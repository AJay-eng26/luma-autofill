"""Command-line entry point: ``python -m luma_autofill``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

from playwright.sync_api import BrowserContext, Error as PlaywrightError, Page, Playwright, sync_playwright

from . import luma
from .drafter import DEFAULT_MODEL, Drafter
from .form import FormField, extract_fields, fill_field
from .luma import STATUS_DESCRIPTIONS, Status
from .matcher import match_fields
from .profile import Profile, ProfileError, load_profile
from .review import print_summary, review_loop

SIGNIN_URL = "https://luma.com/signin"
MAX_STEPS = 4

EXIT_OK, EXIT_ERROR, EXIT_ABORTED, EXIT_UNAVAILABLE = 0, 1, 2, 3


def log(msg: str) -> None:
    print(f"› {msg}", flush=True)


def wait_for_fields(page: Page, timeout_ms: int = 10000) -> list[FormField]:
    waited = 0
    while True:
        fields = extract_fields(page)
        if fields or waited >= timeout_ms:
            return fields
        page.wait_for_timeout(500)
        waited += 500


def prepare_fields(page: Page, fields: list[FormField], profile: Profile, drafter: Drafter | None) -> None:
    unanswered = match_fields(fields, profile)
    if unanswered:
        if drafter:
            log(f"Drafting {len(unanswered)} answer(s) with {drafter.model}…")
            try:
                drafter.draft(unanswered, profile, luma.event_context(page))
            except Exception as exc:  # noqa: BLE001 - keep going; user fills by hand
                log(f"AI drafting failed ({exc.__class__.__name__}: {exc}). Fill these in manually.")
                for f in unanswered:
                    f.flagged, f.note = True, "AI drafting failed; please fill in."
        else:
            for f in unanswered:
                f.flagged, f.note = True, "No profile match (AI disabled); please fill in."
    for f in fields:
        if f.source in ("prefilled", "") or (f.is_empty and not f.required):
            continue
        try:
            fill_field(page, f)
        except Exception as exc:  # noqa: BLE001
            f.flagged = True
            f.note = (f.note + "; " if f.note else "") + f"could not fill automatically: {exc.__class__.__name__}"


def report(status: Status, approval: bool) -> int:
    if status is Status.UNKNOWN and approval:
        msg = "Submitted. This event requires host approval — check your email / the event page for the decision."
    else:
        msg = STATUS_DESCRIPTIONS[status]
        if approval and status is Status.REGISTERED:
            msg += " (Event is approval-based — host may still review.)"
    print(f"\nSTATUS: {status.value.upper()} — {msg}")
    if status in (Status.CLOSED, Status.PAID):
        return EXIT_UNAVAILABLE
    return EXIT_OK


def run_registration(
    page: Page,
    profile: Profile,
    drafter: Drafter | None,
    dry_run: bool,
    ask: Callable[[str], str] = input,
) -> int:
    """Drive one already-loaded event page through registration."""
    if luma.is_logged_out(page):
        log("Looks like you're not signed in to Luma. Run `python -m luma_autofill login` first "
            "(continuing — Luma will ask for name/email).")

    text = luma.page_text(page)
    approval = luma.requires_approval(text)
    if approval:
        log("This event requires host approval.")

    cta = luma.find_cta(page)
    if cta is None:
        status = luma.detect_status(text)
        if status is not Status.UNKNOWN or not extract_fields(page):
            log("No registration button found.")
            return report(status, approval)
    else:
        label = luma.cta_label(cta)
        log(f"Registration button: “{label}”")
        if luma.is_paid(label):
            return report(Status.PAID, approval)
        if luma.is_one_click(label):
            print(f"\n“{label}” registers immediately using your Luma account (no form).")
            print(f"  name: {profile.name}\n  email: {profile.email}")
            if dry_run:
                log("Dry run: stopping before one-click registration.")
                return EXIT_OK
            if ask("Type 'submit' to register: ").strip().lower() != "submit":
                log("Aborted. Nothing was submitted.")
                return EXIT_ABORTED
            before = luma.page_text(page)
            cta.click()
            return report(luma.wait_for_status(page, before), approval)
        cta.click()

    seen_labels: set[tuple[str, ...]] = set()
    for step in range(1, MAX_STEPS + 1):
        fields = wait_for_fields(page)
        if not fields:
            if step == 1:
                log("Opened registration but found no form fields.")
                break
            return report(luma.detect_status(luma.page_text(page)), approval)
        key = tuple(f.label for f in fields)
        if key in seen_labels:
            log("The same form is still showing after submit — Luma probably rejected a value. Check the browser.")
            return EXIT_ERROR
        seen_labels.add(key)

        log(f"Step {step}: found {len(fields)} field(s).")
        prepare_fields(page, fields, profile, drafter)

        if dry_run:
            print_summary(fields)
            log("Dry run: stopping before submit. Nothing was submitted.")
            return EXIT_OK

        if not review_loop(fields, lambda f: fill_field(page, f), ask):
            log("Aborted. Nothing was submitted.")
            return EXIT_ABORTED

        submit = luma.find_submit(page)
        if submit is None:
            log("Could not find the submit button. Submit manually in the browser.")
            return EXIT_ERROR
        log(f"Clicking “{luma.cta_label(submit)}”…")
        before = luma.page_text(page)
        submit.click()
        status = luma.wait_for_status(page, before, timeout_ms=10000)
        if status is not Status.UNKNOWN:
            return report(status, approval)
        # No status yet: either a multi-step form or a slow confirmation. Loop to check.

    return report(luma.detect_status(luma.page_text(page)), approval)


BROWSER_CHOICES = ("auto", "chrome", "msedge", "chromium")


def launch_browser(p: Playwright, args: argparse.Namespace, headless: bool) -> BrowserContext:
    """Open a persistent profile in a real Chrome/Edge install when available.

    Luma's bot check can stall on Playwright's bundled Chromium, so ``auto`` tries the
    user's installed Chrome, then Edge, and only then falls back to bundled Chromium.
    Each browser gets its own profile folder because their profile formats differ.
    """
    channels = ("chrome", "msedge", "chromium") if args.browser == "auto" else (args.browser,)
    last_error: Exception | None = None
    for channel in channels:
        try:
            ctx = p.chromium.launch_persistent_context(
                str(Path(args.browser_profile) / channel),
                channel=None if channel == "chromium" else channel,
                headless=headless,
                ignore_default_args=["--enable-automation"],
                args=["--disable-blink-features=AutomationControlled"],
                no_viewport=True,
            )
        except PlaywrightError as exc:
            last_error = exc
            continue
        log(f"Using browser: {channel}")
        return ctx
    raise SystemExit(f"Could not start a browser ({', '.join(channels)}): {last_error}")


def cmd_login(args: argparse.Namespace) -> int:
    with sync_playwright() as p:
        ctx = launch_browser(p, args, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(SIGNIN_URL)
        input("Sign in to Luma in the browser window, then press Enter here to save the session… ")
        ctx.close()
    log(f"Session saved to {args.browser_profile}")
    return EXIT_OK


def cmd_fill(args: argparse.Namespace) -> int:
    try:
        url = luma.validate_url(args.url)
        profile = load_profile(args.profile)
    except (luma.LumaError, ProfileError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    drafter = None if args.no_ai else Drafter(model=args.model)
    with sync_playwright() as p:
        ctx = launch_browser(p, args, headless=args.headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            log(f"Opening {url}")
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001 - networkidle can time out on busy pages; carry on
            pass
        try:
            code = run_registration(page, profile, drafter, args.dry_run)
        except KeyboardInterrupt:
            log("Interrupted. Nothing further was submitted.")
            code = EXIT_ABORTED
        if args.keep_open and not args.headless:
            input("Press Enter to close the browser… ")
        ctx.close()
    return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="luma_autofill", description="Fill Luma event registration forms.")
    parser.add_argument("--browser-profile", default="browser_profile",
                        help="Folder for the saved browser sessions (default: ./browser_profile)")
    parser.add_argument("--browser", choices=BROWSER_CHOICES, default="auto",
                        help="Browser to drive: auto tries installed Chrome, then Edge, then bundled Chromium.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="Open a browser to sign in to Luma once; the session is reused.")

    fill = sub.add_parser("fill", help="Fill the registration form for an event URL.")
    fill.add_argument("url", help="Luma event URL, e.g. https://lu.ma/abc123")
    fill.add_argument("--profile", default="profile.json", help="Path to your profile.json")
    fill.add_argument("--dry-run", action="store_true", help="Fill and show the summary, never submit.")
    fill.add_argument("--no-ai", action="store_true", help="Don't call the Anthropic API; flag unknown fields.")
    fill.add_argument("--model", default=DEFAULT_MODEL, help=f"Claude model for drafting (default: {DEFAULT_MODEL})")
    fill.add_argument("--headless", action="store_true", help="Run the browser without a window.")
    fill.add_argument("--keep-open", action="store_true", help="Leave the browser open until you press Enter.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.browser_profile = str(Path(args.browser_profile).expanduser())
    return cmd_login(args) if args.command == "login" else cmd_fill(args)
