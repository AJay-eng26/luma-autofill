"""Command-line entry point: ``python -m luma_autofill``."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Callable

from playwright.sync_api import Error as PlaywrightError, Locator, Page, sync_playwright

from . import browser, luma
from .drafter import DEFAULT_MODEL, Drafter
from .form import FormField, extract_fields, fill_field
from .luma import STATUS_DESCRIPTIONS, Status
from .matcher import match_fields
from .profile import Profile, ProfileError, load_profile
from .review import print_summary, review_loop

SIGNIN_URL = "https://luma.com/signin"
MAX_STEPS = 4

EXIT_OK, EXIT_ERROR, EXIT_ABORTED, EXIT_UNAVAILABLE, EXIT_HANDOFF = 0, 1, 2, 3, 4
DEBUG_DIR = Path("debug")


def log(msg: str) -> None:
    print(f"› {msg}", flush=True)


def click_button(page: Page, button: Locator) -> None:
    """Click, falling back to a DOM click when an overlay intercepts the pointer.

    Luma sometimes layers a transparent overlay over the page, which makes Playwright's
    real mouse click retry until it times out.
    """
    try:
        button.click(timeout=5000)
    except PlaywrightError:
        if extract_fields(page):
            return  # The registration form is already open on top of the button.
        log("Normal click was blocked by an overlay; clicking the button directly.")
        button.evaluate("el => el.click()")


def save_debug(page: Page | None) -> None:
    """Save a screenshot and the page HTML so a failure can be diagnosed later."""
    if page is None or page.is_closed():
        return
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        page.screenshot(path=str(DEBUG_DIR / "last_error.png"), full_page=True)
        (DEBUG_DIR / "last_error.html").write_text(page.content(), encoding="utf-8")
        log(f"Saved a screenshot of the page to {DEBUG_DIR / 'last_error.png'}")
    except Exception:  # noqa: BLE001 - debugging aid only
        pass


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
    if status is Status.VERIFYING:
        return EXIT_HANDOFF
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
            click_button(page, cta)
            return report(luma.wait_for_status(page, before), approval)
        click_button(page, cta)

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
        click_button(page, submit)
        status = luma.wait_for_status(page, before, timeout_ms=10000)
        if status is not Status.UNKNOWN:
            return report(status, approval)
        # No status yet: either a multi-step form or a slow confirmation. Loop to check.

    return report(luma.detect_status(luma.page_text(page)), approval)


def _browser_setup(args: argparse.Namespace) -> tuple[str | None, Path]:
    found = browser.find_browser(args.browser, args.browser_path)
    name = found[0] if found else "chromium"
    log(f"Using browser: {name}")
    # Each browser keeps its own saved session; profile formats differ between them.
    return (found[1] if found else None), Path(args.browser_profile) / name


def cmd_login(args: argparse.Namespace) -> int:
    exe, profile_dir = _browser_setup(args)
    if exe is None:
        # Bundled Chromium only; Luma's browser check may stall here.
        with sync_playwright() as p, browser.connected_browser(p, None, profile_dir, headless=False) as ctx:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(SIGNIN_URL)
            input("Sign in to Luma in the browser window, then press Enter here to save the session… ")
    else:
        proc = browser.open_plain(exe, profile_dir, SIGNIN_URL)
        print("Sign in to Luma in the browser window that just opened.")
        print("When you can see your Luma home page, CLOSE that browser window to save the session.")
        # Poll instead of a blocking wait() so Ctrl+C still works on Windows.
        while proc.poll() is None:
            time.sleep(0.5)
    log(f"Session saved to {profile_dir}")
    return EXIT_OK


def cmd_fill(args: argparse.Namespace) -> int:
    try:
        url = luma.validate_url(args.url)
        profile = load_profile(args.profile)
    except (luma.LumaError, ProfileError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    drafter = None if args.no_ai else Drafter(model=args.model)
    exe, profile_dir = _browser_setup(args)
    with sync_playwright() as p, browser.connected_browser(p, exe, profile_dir, args.headless) as session:
        ctx = session.context
        # A fresh tab: the browser's own start tab can be replaced or closed on first run.
        page = ctx.new_page()
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
        except PlaywrightError as exc:
            first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
            if page.is_closed():
                log("The browser tab was closed before the tool finished. Nothing was submitted by the tool.")
            else:
                log(f"Browser step failed: {first_line}")
                save_debug(page)
            code = EXIT_ERROR
        if code in (EXIT_HANDOFF, EXIT_ERROR) and not args.dry_run:
            # Let the user finish (or check) in the real browser instead of closing it on them.
            session.keep_open = True
            log("Leaving Chrome open so you can finish there.")
        elif args.keep_open and not args.headless:
            input("Press Enter to close the browser… ")
    return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="luma_autofill", description="Fill Luma event registration forms.")
    parser.add_argument("--browser-profile", default="browser_profile",
                        help="Folder for the saved browser sessions (default: ./browser_profile)")
    parser.add_argument("--browser", choices=browser.BROWSER_CHOICES, default="auto",
                        help="auto tries installed Chrome, then Edge, then Brave, then bundled Chromium.")
    parser.add_argument("--browser-path", help="Path to a Chromium-based browser executable to use instead.")
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
