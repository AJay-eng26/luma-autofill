import os
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from luma_autofill import luma
from luma_autofill.cli import EXIT_ABORTED, EXIT_OK, run_registration
from luma_autofill.form import TEXT, FormField
from luma_autofill.luma import Status
from luma_autofill.matcher import classify, match_fields
from luma_autofill.profile import Profile

FIXTURE = (Path(__file__).parent / "fixtures" / "event.html").as_uri()

PROFILE = Profile(
    name="Ada Lovelace",
    email="ada@example.com",
    phone="+1 415 555 0100",
    company="Analytical Engines",
    linkedin="linkedin.com/in/ada",
    x_handle="https://x.com/ada",
    role="Founder",
)


def text_field(label, input_type="text"):
    return FormField(id=0, kind=TEXT, label=label, input_type=input_type)


@pytest.mark.parametrize(
    "label,input_type,key",
    [
        ("Email", "text", "email"),
        ("Work email address", "text", "email"),
        ("Anything", "email", "email"),
        ("Phone Number", "text", "phone"),
        ("LinkedIn Profile URL", "text", "linkedin"),
        ("X (Twitter) handle", "text", "x"),
        ("First Name", "text", "first_name"),
        ("Company *", "text", "company"),
        ("Job Title", "text", "role"),
        ("Why do you want to attend? Tell us about your company and what you're building.", "text", None),
        ("How did you hear about us?", "text", None),
    ],
)
def test_classify(label, input_type, key):
    assert classify(text_field(label, input_type)) == key


def test_match_formats_handles():
    fields = [text_field("X handle"), text_field("Twitter profile URL"), text_field("LinkedIn")]
    assert match_fields(fields, PROFILE) == []
    assert [f.values[0] for f in fields] == ["@ada", "https://x.com/ada", "https://linkedin.com/in/ada"]


def test_prefilled_values_are_kept():
    f = text_field("Name")
    f.current = ["Someone Else"]
    match_fields([f], PROFILE)
    assert f.values == ["Someone Else"] and f.source == "prefilled"


@pytest.mark.parametrize(
    "text,status",
    [
        ("Pending Approval — the host will review", Status.PENDING_APPROVAL),
        ("You're on the waitlist", Status.WAITLISTED),
        ("You’re In! See you there", Status.REGISTERED),
        ("Registration Closed", Status.CLOSED),
        ("An event about things", Status.UNKNOWN),
    ],
)
def test_detect_status(text, status):
    assert luma.detect_status(text) is status


def test_validate_url():
    assert luma.validate_url("lu.ma/abc123") == "https://lu.ma/abc123"
    with pytest.raises(luma.LumaError):
        luma.validate_url("https://example.com/abc")


class FakeDrafter:
    model = "fake"

    def __init__(self):
        self.seen = []

    def draft(self, fields, profile, event_context):
        assert "AI Founders Dinner" in event_context
        answers = {
            "How did you hear about this event?": ["Newsletter"],
            "Are you a founder?": ["Yes"],
            "Topics you care about": ["Agents", "Fundraising"],
            "I agree to share my info with the host": ["yes"],
        }
        for f in fields:
            self.seen.append(f.label)
            f.values = answers.get(f.label, ["I'm building developer tools for AI agents."])
            f.source, f.flagged = "ai", True


@pytest.fixture
def page():
    with sync_playwright() as p:
        # PLAYWRIGHT_CHROMIUM lets CI/sandboxes point at a preinstalled browser.
        browser = p.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM") or None)
        pg = browser.new_page()
        pg.goto(FIXTURE)
        yield pg
        browser.close()


def scripted(*answers):
    it = iter(answers)
    return lambda _prompt: next(it)


def test_end_to_end_submit_after_confirm(page):
    drafter = FakeDrafter()
    code = run_registration(page, PROFILE, drafter, dry_run=False, ask=scripted("s", "y", "submit"))
    assert code == EXIT_OK
    submitted = page.evaluate("window.submitted")
    values = list(submitted.values())
    assert "Ada Lovelace" in values and "ada@example.com" in values and "+1 415 555 0100" in values
    assert "https://linkedin.com/in/ada" in values and "@ada" in values and "Founder" in values
    assert "I'm building developer tools for AI agents." in values
    assert submitted.get("founder:y") and submitted.get("topics:a") and submitted.get("topics:c")
    assert submitted.get("terms:on")
    assert not submitted.get("topics:b")
    # Only questions the profile can't answer went to the AI.
    assert "Email" not in drafter.seen and "Company" not in drafter.seen
    assert "Why do you want to attend? Tell us what you're building." in drafter.seen


def test_dry_run_never_submits(page):
    code = run_registration(page, PROFILE, FakeDrafter(), dry_run=True, ask=scripted())
    assert code == EXIT_OK
    assert page.evaluate("window.submitted") is None


def test_quit_does_not_submit(page):
    code = run_registration(page, PROFILE, FakeDrafter(), dry_run=False, ask=scripted("q"))
    assert code == EXIT_ABORTED
    assert page.evaluate("window.submitted") is None


def test_edit_then_submit(page):
    ask = scripted("e 4", "Babbage & Co", "", "a", "s", "submit")
    code = run_registration(page, PROFILE, FakeDrafter(), dry_run=False, ask=ask)
    assert code == EXIT_OK
    assert "Babbage & Co" in page.evaluate("window.submitted").values()


def test_drafter_request_and_parsing():
    import json

    import anthropic
    import httpx2 as httpx

    from luma_autofill.drafter import Drafter

    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        answers = {"answers": [
            {"field_id": 1, "values": ["Building agent tooling."], "confidence": "medium",
             "needs_input": False, "note": ""},
            {"field_id": 2, "values": ["newsletter"], "confidence": "high", "needs_input": False, "note": ""},
        ]}
        return httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": json.dumps(answers)}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    client = anthropic.Anthropic(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    why = FormField(id=1, kind="textarea", label="Why attend?", required=True)
    heard = FormField(id=2, kind="select", label="How did you hear?", options=["Friend", "Newsletter"])
    Drafter(client=client).draft([why, heard], PROFILE, "Title: Dinner")

    body = captured["body"]
    assert body["model"] == "claude-opus-5-5"
    assert body["output_config"]["effort"] == "medium"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["fallbacks"] == "default"
    assert why.values == ["Building agent tooling."] and why.flagged and "confidence: medium" in why.note
    assert heard.values == ["Newsletter"] and heard.source == "ai"


def test_find_browser_choices(tmp_path):
    from luma_autofill import browser

    assert browser.find_browser("chromium") is None
    exe = tmp_path / "chrome"
    exe.write_text("")
    assert browser.find_browser("auto", str(exe)) == ("custom", str(exe))
    with pytest.raises(SystemExit):
        browser.find_browser("auto", str(tmp_path / "missing"))
