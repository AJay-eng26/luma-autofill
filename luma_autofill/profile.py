"""Load and normalise the user's profile.json."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

REQUIRED_KEYS = ("name", "email")
KNOWN_KEYS = (
    "name",
    "email",
    "phone",
    "company",
    "linkedin",
    "x_handle",
    "role",
    "website",
    "github",
    "location",
    "about",
)


class ProfileError(Exception):
    pass


@dataclass
class Profile:
    name: str
    email: str
    phone: str = ""
    company: str = ""
    linkedin: str = ""
    x_handle: str = ""
    role: str = ""
    website: str = ""
    github: str = ""
    location: str = ""
    about: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def first_name(self) -> str:
        return self.name.split()[0] if self.name.strip() else ""

    @property
    def last_name(self) -> str:
        parts = self.name.split()
        return " ".join(parts[1:]) if len(parts) > 1 else ""

    @property
    def x_at_handle(self) -> str:
        handle = self.x_handle.strip()
        for prefix in ("https://", "http://", "www.", "x.com/", "twitter.com/"):
            if handle.lower().startswith(prefix):
                handle = handle[len(prefix):]
        handle = handle.strip("/").lstrip("@")
        return f"@{handle}" if handle else ""

    @property
    def x_url(self) -> str:
        handle = self.x_at_handle.lstrip("@")
        return f"https://x.com/{handle}" if handle else ""

    @property
    def linkedin_url(self) -> str:
        value = self.linkedin.strip()
        if value and not value.lower().startswith("http"):
            value = "https://" + value.lstrip("/")
        return value

    def as_context(self) -> dict[str, str]:
        """Everything the LLM may use when drafting answers."""
        data = {k: getattr(self, k) for k in KNOWN_KEYS if getattr(self, k)}
        data.update({k: v for k, v in self.extra.items() if v})
        return data


def load_profile(path: str | Path) -> Profile:
    path = Path(path)
    if not path.exists():
        raise ProfileError(
            f"{path} not found. Copy profile.example.json to {path.name} and fill in your details."
        )
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ProfileError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProfileError(f"{path} must contain a JSON object.")

    missing = [k for k in REQUIRED_KEYS if not str(raw.get(k, "")).strip()]
    if missing:
        raise ProfileError(f"{path} is missing required keys: {', '.join(missing)}")

    known = {k: str(raw[k]).strip() for k in KNOWN_KEYS if k in raw and raw[k] is not None}
    extra = {k: str(v).strip() for k, v in raw.items() if k not in KNOWN_KEYS and v is not None}
    return Profile(**known, extra=extra)
