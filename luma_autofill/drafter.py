"""Draft answers for unknown / free-text questions with the Anthropic API."""

from __future__ import annotations

import json
import os
from typing import Literal

import anthropic
from pydantic import BaseModel

from .form import CHECKBOX, CHOICE_KINDS, FormField, best_option
from .profile import Profile

DEFAULT_MODEL = os.environ.get("LUMA_AUTOFILL_MODEL", "claude-opus-5-5")

SYSTEM_PROMPT = """You fill out event registration forms on behalf of the user described in the profile.

Write answers in the first person, as the user. Keep them short and natural: one or two sentences
for free-text questions unless the question clearly asks for more. Use only facts present in the
profile or event description. Never invent employers, achievements, numbers, or links. If the profile
does not contain what a question asks for, give your best honest short answer and set needs_input to
true with a note saying what the user should check or supply.

For multiple-choice questions, values must be copied exactly from the provided options. For a single
checkbox (consent, terms, opt-ins), answer "yes" or "no"; answer "yes" to required terms/consent
checkboxes and "no" to optional marketing opt-ins unless the profile says otherwise. Return an empty
values list to leave an optional field blank."""


class DraftedAnswer(BaseModel):
    field_id: int
    values: list[str]
    confidence: Literal["high", "medium", "low"]
    needs_input: bool
    note: str


class DraftResponse(BaseModel):
    answers: list[DraftedAnswer]


class Drafter:
    def __init__(self, model: str = DEFAULT_MODEL, client: anthropic.Anthropic | None = None):
        self.model = model
        self.client = client or anthropic.Anthropic()

    def draft(self, fields: list[FormField], profile: Profile, event_context: str) -> None:
        """Set ``values``/``source``/``flagged``/``note`` on each field in place."""
        if not fields:
            return
        questions = [
            {
                "field_id": f.id,
                "question": f.label,
                "type": "single checkbox" if f.kind == CHECKBOX else f.kind,
                "required": f.required,
                **({"options": f.options} if f.options else {}),
            }
            for f in fields
        ]
        user_msg = (
            f"<profile>\n{json.dumps(profile.as_context(), indent=2)}\n</profile>\n\n"
            f"<event>\n{event_context[:6000]}\n</event>\n\n"
            f"<questions>\n{json.dumps(questions, indent=2)}\n</questions>\n\n"
            "Answer every question by field_id."
        )
        response = self.client.beta.messages.parse(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            output_format=DraftResponse,
            output_config={"effort": "medium"},
            # Server-side fallback: if the primary model declines, the API retries on another model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal" or response.parsed_output is None:
            for f in fields:
                f.source, f.flagged, f.note = "ai", True, "AI could not draft an answer; please fill in."
            return

        by_id = {a.field_id: a for a in response.parsed_output.answers}
        for f in fields:
            answer = by_id.get(f.id)
            f.source, f.flagged = "ai", True
            if answer is None:
                f.note = "AI skipped this question; please fill in."
                continue
            values = [v.strip() for v in answer.values if v.strip()]
            if f.kind in CHOICE_KINDS and f.options:
                values = [m for v in values if (m := best_option(f.options, v))]
            f.values = values
            notes = [answer.note.strip()] if answer.note.strip() else []
            if answer.needs_input:
                notes.insert(0, "needs your input")
            if answer.confidence != "high":
                notes.append(f"confidence: {answer.confidence}")
            f.note = "; ".join(notes)
