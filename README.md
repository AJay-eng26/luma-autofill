# luma-autofill

A Python CLI agent that fills Luma (lu.ma / luma.com) event registration forms for you.

- Opens the event in Chromium (Playwright) with a **persistent, logged-in browser profile**.
- Detects every form field and fills it from **`profile.json`** (name, email, phone, company, LinkedIn, X handle, role, …).
- For free-text or unknown questions, **drafts an answer with Claude** (Anthropic API) and **flags it for your review**.
- **Stops before submit**, prints a summary of every field, lets you edit any of them, and submits only after you type `submit`.
- Detects **approval-required** events and reports the final status: registered, pending approval, waitlisted, closed, or paid ticket.
- `--dry-run` fills the form and shows the summary, but never submits.

## Setup (about 5 min)

Requires Python 3.10+.

```bash
git clone https://github.com/ajay-eng26/luma-autofill.git
cd luma-autofill
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

1. **Your details** — copy the example and edit it:

   ```bash
   cp profile.example.json profile.json
   ```

   `name` and `email` are required. Optional keys: `phone`, `company`, `linkedin`, `x_handle`, `role`, `website`, `github`, `location`, and `about` (a few sentences about you — the AI uses it to answer open questions). Any extra keys you add are also given to the AI as context. `profile.json` is gitignored.

2. **Anthropic API key** — used only for questions your profile can't answer:

   ```bash
   export ANTHROPIC_API_KEY=sk-ant-...
   ```

   Skip this and pass `--no-ai` if you'd rather type those answers yourself.

3. **Log in to Luma once** — a browser window opens; sign in, then press Enter in the terminal:

   ```bash
   python -m luma_autofill login
   ```

   The session is saved in `./browser_profile/` (gitignored) and reused on every run.

## Usage

```bash
# Preview: fill the form, print the summary, never submit
python -m luma_autofill fill https://lu.ma/your-event --dry-run

# Real run: fill, review, then submit only after you confirm
python -m luma_autofill fill https://lu.ma/your-event
```

At the review prompt:

| Key | Action |
|---|---|
| `s` | Submit. Blocked if a required field is empty; asks again if AI answers are still flagged; then asks you to type `submit`. |
| `e 7` | Edit field 7. The new value is written into the page right away. |
| `a` | Mark all AI-drafted answers as reviewed. |
| `q` | Quit without submitting. |

Example summary:

```
[3] LinkedIn Profile URL  (profile)
    > https://www.linkedin.com/in/adalovelace

[9] Why do you want to attend? Tell us what you're building. *  (AI DRAFT)  ⚠ REVIEW
    > I'm building developer tools for AI agents and want to meet other founders in the space.
    note: confidence: medium
```

### Options

| Flag | Default | Description |
|---|---|---|
| `--dry-run` | off | Fill and summarise; never submit. |
| `--no-ai` | off | Don't call the Anthropic API; unknown fields are flagged for you to fill. |
| `--model` | `claude-opus-5-5` | Claude model for drafting. Also settable with `LUMA_AUTOFILL_MODEL`. |
| `--profile` | `profile.json` | Path to your details. |
| `--headless` | off | Run without a browser window. |
| `--keep-open` | off | Leave the browser open at the end until you press Enter. |
| `--browser-profile` | `./browser_profile` | Persistent browser profile directory (put before the subcommand). |

### Exit codes

`0` done or dry run · `1` error · `2` you quit without submitting · `3` registration closed or requires a paid ticket.

## How it works

1. **Open** the event page and read the status. If there's no register button, it reports whether you're already registered, pending, waitlisted, or the event is closed.
2. **Approval check** — text like "Approval Required" or a "Request to Join" button marks the event as approval-based. The status report says so.
3. **Click the register button** to open the form. Paid tickets are never handled; the tool stops and tells you. A "One-click RSVP" button registers immediately, so it gets the same `submit` confirmation as a form.
4. **Detect fields** — every visible input, textarea, select, radio group, checkbox group and combobox in the registration dialog, each with its label, options, and required flag.
5. **Match** — keyword rules map short labels to profile keys (e.g. "LinkedIn Profile URL" → `linkedin`, "X handle" → `@handle`, "Twitter URL" → `https://x.com/handle`). Values Luma prefilled from your account are kept.
6. **Draft** — remaining questions go to Claude in one call with your profile and the event description, returned as structured JSON. Every AI answer is flagged `⚠ REVIEW`. Choice answers must match one of the listed options. The request turns on Anthropic's server-side refusal fallback (`fallbacks: "default"`).
7. **Review and submit** — after you confirm, it clicks submit and reads the result. Multi-step forms go through review again for each step.

## Limitations

- Luma changes its markup often. Detection is generic (labels, ARIA roles), but a redesign can still break a selector. Use `--dry-run` first on events that matter, and check the browser window.
- The status shown after submit is read from the page text. Treat Luma's confirmation email as the final word.
- File uploads, CAPTCHAs and payments are not automated.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

The tests run the whole flow against a local mock of a Luma approval-required event (`tests/fixtures/event.html`) with a fake drafter, so they need no network or API key. To use an existing Chromium binary, set `PLAYWRIGHT_CHROMIUM=/path/to/chrome`.
