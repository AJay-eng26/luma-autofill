"""Match form fields to profile.json values with keyword rules.

Anything a rule can't answer confidently is left for the AI drafter.
"""

from __future__ import annotations

import re

from .form import CHECKBOX, CHOICE_KINDS, FormField, best_option
from .profile import Profile

# Labels longer than this are treated as open questions, not simple fields
# ("Tell us about your company and why you want to attend" is not a "company" field).
MAX_SIMPLE_LABEL = 60

# (key, patterns) — checked in order, first hit wins.
RULES: list[tuple[str, tuple[str, ...]]] = [
    ("linkedin", (r"linked\s*in",)),
    ("x", (r"twitter", r"\bx\s*(\(|handle|username|profile|account|url|link)", r"x\.com", r"^x$")),
    ("github", (r"github",)),
    ("telegram", (r"telegram", r"\btg\b")),
    ("email", (r"e-?mail",)),
    ("phone", (r"phone", r"mobile", r"whats\s*app", r"cell")),
    ("first_name", (r"first\s*name", r"given\s*name")),
    ("last_name", (r"last\s*name", r"surname", r"family\s*name")),
    ("name", (r"^(your\s*)?(full\s*)?name$", r"^what('s| is) your (full )?name\??$")),
    ("company", (r"company", r"organi[sz]ation", r"employer", r"affiliation", r"startup\s*name", r"where do you work")),
    ("role", (r"job\s*title", r"\btitle\b", r"\brole\b", r"position", r"occupation", r"what do you do")),
    ("website", (r"website", r"portfolio", r"personal\s*(site|url)")),
    ("location", (r"\bcity\b", r"location", r"where are you based")),
]

INPUT_TYPE_KEYS = {"email": "email", "tel": "phone"}


def _value_for(key: str, label: str, profile: Profile) -> str:
    wants_url = bool(re.search(r"url|link|profile|https?", label, re.I))
    if key == "x":
        return profile.x_url if wants_url else profile.x_at_handle
    if key == "telegram":
        handle = profile.telegram_at_handle
        return f"https://t.me/{handle.lstrip('@')}" if handle and wants_url else handle
    if key == "linkedin":
        return profile.linkedin_url
    if key == "first_name":
        return profile.first_name
    if key == "last_name":
        return profile.last_name
    return getattr(profile, key, "") or ""


def classify(f: FormField) -> str | None:
    """Return the profile key a field maps to, or None if it needs the drafter."""
    if f.kind == CHECKBOX:
        return None
    if f.input_type in INPUT_TYPE_KEYS:
        return INPUT_TYPE_KEYS[f.input_type]
    label = f.label.strip().rstrip("*").strip()
    if not label or len(label) > MAX_SIMPLE_LABEL:
        return None
    for key, patterns in RULES:
        if any(re.search(p, label, re.I) for p in patterns):
            return key
    return None


def _same(a: str, b: str) -> bool:
    norm = lambda t: re.sub(r"[^a-z0-9]+", "", t.lower())  # noqa: E731
    return norm(a) == norm(b)


def match_fields(fields: list[FormField], profile: Profile) -> list[FormField]:
    """Fill ``values``/``source`` from the profile. Returns the fields still unanswered."""
    unanswered: list[FormField] = []
    for f in fields:
        key = classify(f)
        value = _value_for(key, f.label, profile) if key else ""
        if value and f.kind in CHOICE_KINDS and f.options:
            value = best_option(f.options, value) or ""
        prefilled = list(f.current) if f.current and f.kind != CHECKBOX else []
        if value:
            # profile.json wins over answers Luma remembered from earlier registrations.
            f.values, f.source = [value], "profile"
            if prefilled and not _same(prefilled[0], value):
                f.note = f"replaced Luma's saved answer: {prefilled[0]}"
        elif prefilled:
            f.values, f.source = prefilled, "prefilled"
        else:
            unanswered.append(f)
    return unanswered
