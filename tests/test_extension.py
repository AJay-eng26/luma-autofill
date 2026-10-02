"""Run the browser extension's content script against the mock Luma page."""

import json
import os
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent.parent
FIXTURE = (Path(__file__).parent / "fixtures" / "event.html").as_uri()
PROFILE = {
    "name": "Ada Lovelace", "email": "ada@example.com", "phone": "+1 415 555 0100",
    "company": "Analytical Engines", "linkedin": "linkedin.com/in/ada", "x_handle": "https://x.com/ada",
    "role": "Founder", "location": "APAC",
}

# Stand-in for the extension APIs: storage holds the profile, the "background" returns fixed drafts.
CHROME_STUB = """
window.__draftCalls = [];
window.chrome = {
  storage: { local: { get: async () => ({ profile: %s }) } },
  runtime: { sendMessage: (msg, cb) => {
    window.__draftCalls.push(msg.payload);
    const answers = msg.payload.questions.map((q) => {
      const pick = {
        "How did you hear about this event?": ["Newsletter"],
        "Are you a founder?": ["Yes"],
        "Topics you care about": ["Agents", "Fundraising"],
        "I agree to share my info with the host": ["yes"],
        "Which partners interest you?": ["Beta Capital"],
      }[q.question] || ["I'm building developer tools for AI agents."];
      return { field_id: q.field_id, values: pick, confidence: "medium", needs_input: false, note: "" };
    });
    setTimeout(() => cb({ ok: true, answers }), 10);
  } },
};
""" % json.dumps(PROFILE)

# A Luma-style custom multi-select (role=combobox + role=option list).
COMBOBOX = """
const wrap = document.createElement('div');
wrap.className = 'field';
wrap.innerHTML = '<div class="label">Which partners interest you?</div>' +
  '<div role="combobox" tabindex="0" id="combo" style="padding:8px;border:1px solid #999">Select one or more</div>' +
  '<div id="list" role="listbox" style="display:none">' +
  '<div role="option">Alpha Ventures</div><div role="option">Beta Capital</div></div>';
document.getElementById('form').insertBefore(wrap, document.getElementById('submit'));
window.__comboPicked = [];
document.getElementById('combo').onclick = () => { document.getElementById('list').style.display = 'block'; };
document.querySelectorAll('#list [role=option]').forEach((o) => o.onclick = () => {
  window.__comboPicked.push(o.innerText); document.getElementById('list').style.display = 'none';
});
"""


@pytest.fixture
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM") or None)
        pg = browser.new_page()
        pg.goto(FIXTURE)
        pg.add_init_script(CHROME_STUB)
        pg.reload()
        yield pg
        browser.close()


def inject(page):
    page.add_style_tag(path=str(ROOT / "extension" / "content.css"))
    page.add_script_tag(path=str(ROOT / "extension" / "content.js"))


def test_autofill_button_fills_form_without_submitting(page):
    inject(page)
    page.click("#cta")
    page.evaluate(COMBOBOX)
    page.click("#luma-autofill-btn")
    page.wait_for_selector("#luma-autofill-panel:has-text('Filled')")

    values = page.evaluate("[...document.querySelectorAll('#form input, #form textarea')].map(e => e.type === 'checkbox' || e.type === 'radio' ? e.checked : e.value)")
    assert "ada@example.com" in values and "+1 415 555 0100" in values
    assert "https://linkedin.com/in/ada" in values and "@ada" in values and "Founder" in values
    assert "I'm building developer tools for AI agents." in values
    assert page.eval_on_selector("#heard", "e => e.options[e.selectedIndex].text") == "Newsletter"
    assert page.is_checked("input[name=founder][value=y]")
    assert page.is_checked("input[name=topics][value=a]") and page.is_checked("input[name=topics][value=c]")
    assert not page.is_checked("input[name=topics][value=b]")
    assert page.is_checked("#terms")
    assert page.evaluate("window.__comboPicked") == ["Beta Capital"]
    # Only open questions went to the AI, with the combobox options it read.
    asked = {q["question"]: q for q in page.evaluate("window.__draftCalls")[0]["questions"]}
    assert "Email" not in asked and "Company" not in asked
    assert asked["Which partners interest you?"]["options"] == ["Alpha Ventures", "Beta Capital"]
    # AI answers are highlighted; nothing was submitted.
    assert page.locator("[data-luma-autofill=ai]").count() >= 4
    assert page.evaluate("window.submitted") is None
    assert "host approval" in page.inner_text("#luma-autofill-panel")


def test_opens_the_form_itself(page):
    inject(page)
    page.click("#luma-autofill-btn")  # form not open yet: Autofill clicks "Request to Join" first
    page.wait_for_selector("#luma-autofill-panel:has-text('Filled')")
    assert page.locator("#modal").is_visible()
    assert page.input_value("#email") == "ada@example.com"
    assert page.evaluate("window.submitted") is None


def test_open_form_survives_click_on_autofill(page):
    # Luma-style dismissal: any pointer press or focus outside the open form closes it.
    page.evaluate("""() => {
        const modal = document.getElementById('modal');
        const dismiss = (e) => { if (modal.style.display !== 'none' && !modal.contains(e.target)) modal.style.display = 'none'; };
        document.addEventListener('pointerdown', dismiss);
        document.addEventListener('mousedown', dismiss);
        document.addEventListener('focusin', dismiss);
    }""")
    inject(page)
    page.click("#cta")
    page.click("#luma-autofill-btn")
    page.wait_for_selector("#luma-autofill-panel:has-text('Filled')")
    assert page.locator("#modal").is_visible()
    assert page.input_value("#email") == "ada@example.com"


def test_alt_a_shortcut(page):
    inject(page)
    page.keyboard.press("Alt+a")
    page.wait_for_selector("#luma-autofill-panel:has-text('Filled')")
    assert page.input_value("#email") == "ada@example.com"


def test_no_form_on_page(page):
    page.evaluate("document.getElementById('cta').remove(); document.getElementById('modal').remove()")
    inject(page)
    page.click("#luma-autofill-btn")
    page.wait_for_selector("#luma-autofill-panel")
    assert "No form found" in page.inner_text("#luma-autofill-panel")


REACT_FIXTURE = (Path(__file__).parent / "fixtures" / "react_event.html").as_uri()

REACT_STUB = CHROME_STUB.replace(
    '"Which partners interest you?": ["Beta Capital"],',
    '"Which partners interest you?": ["Beta Capital"],\n        "Telegram Contact": ["@ada_tg"],\n        "What\'s your familiarity with Calisthenics?": ["Intermediate"],',
)


@pytest.fixture
def react_page():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM") or None)
        pg = browser.new_page()
        pg.add_init_script(REACT_STUB)
        pg.goto(REACT_FIXTURE)
        yield pg
        browser.close()


def test_values_stick_in_react_controlled_form(react_page):
    page = react_page
    inject(page)
    page.click("#luma-autofill-btn")  # opens the React modal itself, then fills
    page.wait_for_selector("#luma-autofill-panel:has-text('Filled')")
    page.wait_for_timeout(300)
    page.click("#submit")  # the test submits, to read React's own state
    state = page.evaluate("window.submitted")
    assert state["name"] == "Ada Lovelace"
    assert state["company"] == "Analytical Engines"
    assert state["telegram"] == "@ada_tg"
    assert state["x"] == "@ada"
    assert state["fam"] == "Intermediate"
    # The covered inline copy of the form behind the modal stays untouched.
    assert page.input_value("#behind-name") == "" and page.input_value("#behind-company") == ""


def test_reports_fields_that_did_not_stick(react_page):
    page = react_page
    page.evaluate("chrome.storage.local.get = async () => ({ profile: { name: 'Ada Lovelace', email: 'a@x.com', linkedin: 'linkedin.com/in/ada' } })")
    inject(page)
    page.click("#luma-autofill-btn")
    page.wait_for_selector("#luma-autofill-panel:has-text('Filled')")
    text = page.inner_text("#luma-autofill-panel")
    assert "couldn't be filled automatically" in text
    assert page.locator(".panel [data-luma-autofill=missing]").count() >= 1
    assert "Please enter: https://linkedin.com/in/ada" in page.inner_text(".panel")


def test_name_rule_only_matches_own_name(page):
    inject(page)
    cls = lambda label: page.evaluate("(l) => window.__lumaAutofill.classify({kind: 'text', inputType: 'text', label: l})", label)
    assert cls("Name") == "name"
    assert cls("Full name") == "name"
    assert cls("Name of your primary contact at OKX") is None


def test_telegram_from_profile(page):
    inject(page)
    val = page.evaluate("""() => {
        const A = window.__lumaAutofill;
        const f = {kind: 'text', inputType: 'text', label: 'Telegram Contact'};
        return [A.classify(f), A.profileValue('telegram', f.label, {telegram: 't.me/ada_tg'})];
    }""")
    assert val == ["telegram", "@ada_tg"]
    x = page.evaluate("window.__lumaAutofill.profileValue('x', 'X handle', {x_handle: 'x.com/ada'})")
    assert x == "@ada"
