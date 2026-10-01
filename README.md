# luma-autofill

A Python CLI agent that fills Luma (lu.ma / luma.com) event registration forms for you.

- Opens the event in Chromium (Playwright) with a **persistent, logged-in browser profile**.
- Detects every form field and fills it from **`profile.json`** (name, email, phone, company, LinkedIn, X handle, role, …).
- For free-text or unknown questions, **drafts an answer with Claude** (Anthropic API) and **flags it for your review**.
- **Stops before submit**, prints a summary of every field, lets you edit any of them, and submits only after you type `submit`.
- Detects **approval-required** events and reports the final status: registered, pending approval, waitlisted, closed, or paid ticket.
- `--dry-run` fills the form and shows the summary, but never submits.

## Recommended: the browser extension

Luma protects its submit button with a Cloudflare "Verifying Your Browser" check that fails whenever an automation tool is attached to the browser. The `extension/` folder fixes this by running **inside your own Brave or Chrome** with nothing attached. It fills the form, and **you click submit yourself**, so Cloudflare sees a normal visitor.

### Install (about 3 min)

1. Download this repository (Code → Download ZIP) and unzip it.
2. In Brave, open `brave://extensions` (in Chrome, `chrome://extensions`).
3. Turn on **Developer mode** (top right).
4. Click **Load unpacked** and choose the `extension` folder.
5. Click the **puzzle-piece icon → Luma Autofill**. The settings page opens. Click **Import profile.json** or type your details, paste your Anthropic API key, then **Save**.

### Use

1. Open a Luma event and click **Register** / **Request to Join** so the form appears.
2. Click the purple **✨ Autofill** button (bottom right).
3. Check the highlights. Green came from your profile, 🟨 yellow is an AI draft to check, and 🟥 red is required but still empty.
4. Click Luma's submit button yourself.

Your details and API key are kept in the browser's local extension storage. Claude is called from the extension's background worker, never from the Luma page.

## Command-line tool (Playwright)

The CLI below does the same filling from a terminal. It works for filling and reviewing, but Luma's Cloudflare check can block the final submit while Playwright is attached. When that happens, the tool disconnects and leaves the browser open for you (exit code 4).

### Setup (about 5 min)

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

3. **Log in to Luma once** — your installed Chrome opens on the Luma sign-in page with nothing attached to it. Sign in, then **close that browser window** to save the session:

   ```bash
   python -m luma_autofill login
   ```

   The session is saved in `./browser_profile/<browser>/` (gitignored) and reused on every run.

   Why not let Playwright launch the browser? Luma's "Verifying Your Browser" check stalls on automation-launched browsers. So the tool starts your real **Chrome**, or **Edge** or **Brave** if Chrome isn't installed, as a normal process. For `fill`, it then connects to that browser over a local DevTools port. To pick a browser, put `--browser chrome|msedge|brave|chromium` or `--browser-path <exe>` before the subcommand, e.g. `python -m luma_autofill --browser msedge login`.

### Usage

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
| `--browser` | `auto` | `chrome`, `msedge`, `brave`, or `chromium` (bundled); `auto` tries them in that order (put before the subcommand). |
| `--browser-path` | — | Use a specific Chromium-based browser executable (put before the subcommand). |
| `--browser-profile` | `./browser_profile` | Folder for saved browser sessions (put before the subcommand). |

### Exit codes

`0` done or dry run · `1` error · `2` you quit without submitting · `3` registration closed or requires a paid ticket · `4` Luma started its "Verifying Your Browser" check after submit; the tool disconnected and left the browser open for you to finish.

### How it works

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
