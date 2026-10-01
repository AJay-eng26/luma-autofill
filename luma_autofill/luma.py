"""Luma page-level logic: opening the form, finding submit, reading status."""

from __future__ import annotations

import re
from enum import Enum
from urllib.parse import urlparse

from playwright.sync_api import Locator, Page

LUMA_HOSTS = {"lu.ma", "www.lu.ma", "luma.com", "www.luma.com"}


class Status(str, Enum):
    REGISTERED = "registered"
    PENDING_APPROVAL = "pending_approval"
    WAITLISTED = "waitlisted"
    CLOSED = "closed"
    PAID = "paid_ticket"
    OPEN = "open"
    VERIFYING = "verifying_browser"
    UNKNOWN = "unknown"


STATUS_DESCRIPTIONS = {
    Status.REGISTERED: "You're registered.",
    Status.PENDING_APPROVAL: "Registration submitted — pending host approval.",
    Status.WAITLISTED: "You're on the waitlist.",
    Status.CLOSED: "Registration is closed, sold out, or the event has ended.",
    Status.PAID: "This event requires a paid ticket — not handled automatically. Register manually.",
    Status.OPEN: "Registration is open.",
    Status.VERIFYING: ("Luma is running its 'Verifying Your Browser' check. The tool has let go of Chrome "
                       "and left it open: finish there (tick the checkbox if one appears). Your request is "
                       "sent once the check passes."),
    Status.UNKNOWN: "Could not determine registration status — check the page.",
}

# Checked in order; the first match wins. Patterns run against visible page text.
STATUS_PATTERNS: list[tuple[Status, tuple[str, ...]]] = [
    (Status.PENDING_APPROVAL, (r"pending approval", r"approval pending", r"request (has been )?(sent|submitted)",
                               r"waiting (for|on) (host )?approval", r"registration is pending",
                               r"host will review", r"you('|’)ll (be notified|hear back)")),
    (Status.WAITLISTED, (r"you('|’)re on the waitlist", r"joined the waitlist", r"on the wait ?list")),
    (Status.REGISTERED, (r"you('|’)re (in|going|registered)", r"you are (in|going|registered)",
                         r"registration confirmed", r"see you there")),
    (Status.CLOSED, (r"registration (is )?closed", r"sold out", r"event (is )?full", r"this event has ended",
                     r"event ended", r"no longer accepting")),
]

APPROVAL_PATTERNS = (r"approval required", r"requires? approval", r"request to join",
                     r"subject to (host )?approval", r"host (must|will) approve")

CTA_RE = re.compile(r"^(register|request to join|rsvp|join event|join waitlist|get tickets?|"
                    r"one-click rsvp|apply|sign up|reserve( a)? spot|attend)\b", re.I)
ONE_CLICK_RE = re.compile(r"one[- ]click", re.I)
SUBMIT_RE = re.compile(r"^(register|request to join|submit|rsvp|confirm|join( event| waitlist)?|"
                       r"complete registration|apply|continue|next|send request|sign up)\b", re.I)
PRICE_RE = re.compile(r"[$€£₹]\s?\d")


class LumaError(Exception):
    pass


def validate_url(url: str) -> str:
    parsed = urlparse(url if "://" in url else "https://" + url)
    if parsed.hostname not in LUMA_HOSTS or not parsed.path.strip("/"):
        raise LumaError(f"Not a Luma event URL: {url}")
    return parsed.geturl()


def page_text(page: Page) -> str:
    return page.evaluate("() => document.body ? document.body.innerText : ''")


VERIFYING_RE = re.compile(r"verifying your browser|verify you are human", re.I)


def detect_status(text: str) -> Status:
    if VERIFYING_RE.search(text):
        return Status.VERIFYING
    for status, patterns in STATUS_PATTERNS:
        if any(re.search(p, text, re.I) for p in patterns):
            return status
    return Status.UNKNOWN


def requires_approval(text: str) -> bool:
    return any(re.search(p, text, re.I) for p in APPROVAL_PATTERNS)


def is_logged_out(page: Page) -> bool:
    return page.get_by_role("link", name=re.compile(r"^sign in$", re.I)).count() > 0 or \
        page.get_by_role("button", name=re.compile(r"^sign in$", re.I)).count() > 0


def event_context(page: Page) -> str:
    """Title + visible description, passed to the AI drafter."""
    title = page.title()
    body = page.evaluate(
        """() => {
            const main = document.querySelector('main') || document.body;
            return main.innerText;
        }"""
    )
    return f"URL: {page.url}\nTitle: {title}\n\n{body}"


def _visible_buttons(scope: Page | Locator, pattern: re.Pattern) -> list[Locator]:
    out = []
    for loc in scope.get_by_role("button", name=pattern).all():
        try:
            if loc.is_visible() and loc.is_enabled():
                out.append(loc)
        except Exception:
            continue
    return out


def find_cta(page: Page) -> Locator | None:
    buttons = _visible_buttons(page, CTA_RE)
    return buttons[0] if buttons else None


def cta_label(cta: Locator) -> str:
    return " ".join((cta.inner_text() or "").split())


def is_one_click(label: str) -> bool:
    return bool(ONE_CLICK_RE.search(label))


def is_paid(label: str) -> bool:
    return bool(PRICE_RE.search(label)) or bool(re.search(r"\bget tickets?\b|\bbuy\b|\bpay\b|checkout", label, re.I))


def form_scope(page: Page) -> Page | Locator:
    dialogs = page.locator('[role="dialog"]:visible, dialog[open]')
    return dialogs.last if dialogs.count() else page


def find_submit(page: Page) -> Locator | None:
    """The last enabled submit-like button in the open dialog (or page)."""
    scope = form_scope(page)
    buttons = _visible_buttons(scope, SUBMIT_RE)
    if not buttons:
        submit_inputs = scope.locator('button[type="submit"]:visible, input[type="submit"]:visible').all()
        buttons = [b for b in submit_inputs if b.is_enabled()]
    return buttons[-1] if buttons else None


def wait_for_status(page: Page, before: str, timeout_ms: int = 20000) -> Status:
    """Poll the page after submitting until a status message appears."""
    waited = 0
    while waited < timeout_ms:
        page.wait_for_timeout(1000)
        waited += 1000
        text = page_text(page)
        if text != before:
            status = detect_status(text)
            if status is not Status.UNKNOWN:
                return status
    return detect_status(page_text(page))
