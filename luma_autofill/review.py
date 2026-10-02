"""Terminal summary of filled fields and the confirm/edit loop."""

from __future__ import annotations

import textwrap
from typing import Callable

from .form import CHECKBOX, CHOICE_KINDS, FormField

SOURCE_TAGS = {"prefilled": "prefilled", "profile": "profile", "ai": "AI DRAFT", "user": "edited", "skipped": "skipped"}


def print_summary(fields: list[FormField]) -> None:
    print("\n" + "=" * 72)
    print("FORM SUMMARY")
    print("=" * 72)
    for n, f in enumerate(fields, 1):
        flag = "  ⚠ REVIEW" if f.flagged else ""
        req = " *" if f.required else ""
        print(f"\n[{n}] {f.label}{req}  ({SOURCE_TAGS.get(f.source, 'empty')}){flag}")
        if f.options:
            print(textwrap.fill("options: " + " | ".join(f.options), 70,
                                initial_indent="    ", subsequent_indent="      "))
        value = f.display_value or "(empty)"
        for line in value.splitlines() or [value]:
            print(textwrap.fill(line, 70, initial_indent="    > ", subsequent_indent="      "))
        if f.note:
            print(f"    note: {f.note}")
    print("\n" + "=" * 72)
    flagged = sum(f.flagged for f in fields)
    missing = missing_required(fields)
    if flagged:
        print(f"{flagged} AI-drafted answer(s) flagged for review.")
    if missing:
        print("Required but empty: " + ", ".join(f"[{fields.index(f) + 1}]" for f in missing))


def missing_required(fields: list[FormField]) -> list[FormField]:
    return [f for f in fields if f.required and f.is_empty]


def prompt_new_value(f: FormField, ask: Callable[[str], str] = input) -> list[str]:
    if f.kind == CHECKBOX:
        return ["yes" if ask("Check this box? [y/n]: ").strip().lower().startswith("y") else "no"]
    if f.kind in CHOICE_KINDS and f.options:
        for i, opt in enumerate(f.options, 1):
            print(f"  {i}. {opt}")
        multi = f.kind == "checkboxes"
        raw = ask("Choose number(s), comma-separated: " if multi else "Choose a number: ").strip()
        picks = []
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit() and 1 <= int(part) <= len(f.options):
                picks.append(f.options[int(part) - 1])
        return picks if multi else picks[:1]
    print("Enter new value. Finish with an empty line (blank first line clears the field).")
    lines = []
    while True:
        line = ask("  ")
        if not line:
            break
        lines.append(line)
    return ["\n".join(lines)] if lines else []


def review_loop(
    fields: list[FormField],
    refill: Callable[[FormField], None],
    ask: Callable[[str], str] = input,
) -> bool:
    """Show the summary and let the user edit until they submit (True) or quit (False)."""
    while True:
        print_summary(fields)
        choice = ask("\n[s]ubmit  [e]dit <n>  [a]pprove all flagged  [q]uit without submitting: ").strip().lower()
        if choice in ("q", "quit"):
            return False
        if choice in ("a", "approve"):
            for f in fields:
                f.flagged = False
            continue
        if choice.startswith("e"):
            arg = choice[1:].strip() or ask("Field number: ").strip()
            if not arg.isdigit() or not 1 <= int(arg) <= len(fields):
                print("Invalid field number.")
                continue
            f = fields[int(arg) - 1]
            f.values = prompt_new_value(f, ask)
            f.source, f.flagged, f.note = "user", False, ""
            try:
                refill(f)
            except Exception as exc:  # noqa: BLE001 - surface any Playwright failure
                print(f"Could not write that value into the page: {exc}")
            continue
        if choice in ("s", "submit"):
            missing = missing_required(fields)
            if missing:
                print("Cannot submit: required fields are empty — edit them first.")
                continue
            flagged = [f for f in fields if f.flagged]
            if flagged:
                ok = ask(f"{len(flagged)} AI answer(s) still flagged. Submit anyway? [y/N]: ").strip().lower()
                if not ok.startswith("y"):
                    continue
            confirm = ask("Type 'submit' to register for this event: ").strip().lower()
            if confirm == "submit":
                return True
            print("Not submitted.")
