// Calls the Claude API for questions the profile can't answer.
// Runs in the extension's service worker so the API key never touches the Luma page.

const SYSTEM_PROMPT = `You fill out event registration forms on behalf of the user described in the profile.

Write answers in the first person, as the user. Keep them short and natural: one or two sentences
for free-text questions unless the question clearly asks for more. Use only facts present in the
profile or event description. Never invent employers, achievements, numbers, wallet addresses, names
of people, or links. If the profile does not contain what a question asks for, leave values empty and
set needs_input to true with a short note saying what the user should supply.

For multiple-choice questions, values must be copied exactly from the provided options. For a single
checkbox (consent, terms, opt-ins), answer "yes" or "no"; answer "yes" to required terms/consent
checkboxes and "no" to optional marketing opt-ins unless the profile says otherwise. Return an empty
values list to leave an optional field blank.`;

const ANSWER_SCHEMA = {
  type: "object",
  properties: {
    answers: {
      type: "array",
      items: {
        type: "object",
        properties: {
          field_id: { type: "integer" },
          values: { type: "array", items: { type: "string" } },
          confidence: { type: "string", enum: ["high", "medium", "low"] },
          needs_input: { type: "boolean" },
          note: { type: "string" },
        },
        required: ["field_id", "values", "confidence", "needs_input", "note"],
        additionalProperties: false,
      },
    },
  },
  required: ["answers"],
  additionalProperties: false,
};

async function draft({ questions, profile, event }) {
  const { apiKey, model } = await chrome.storage.local.get(["apiKey", "model"]);
  if (!apiKey) throw new Error("No Anthropic API key saved. Open the extension settings to add one.");

  const userMsg =
    `<profile>\n${JSON.stringify(profile, null, 2)}\n</profile>\n\n` +
    `<event>\n${event.slice(0, 6000)}\n</event>\n\n` +
    `<questions>\n${JSON.stringify(questions, null, 2)}\n</questions>\n\n` +
    "Answer every question by field_id.";

  const res = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-api-key": apiKey,
      "anthropic-version": "2023-06-01",
      // Server-side refusal fallback: if the model declines, the API retries on another model.
      "anthropic-beta": "server-side-fallback-2026-07-01",
      "anthropic-dangerous-direct-browser-access": "true",
    },
    body: JSON.stringify({
      model: model || "claude-opus-5-5",
      max_tokens: 16000,
      system: SYSTEM_PROMPT,
      messages: [{ role: "user", content: userMsg }],
      output_config: { effort: "medium", format: { type: "json_schema", schema: ANSWER_SCHEMA } },
      fallbacks: "default",
    }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = body?.error?.message || res.statusText;
    if (res.status === 401) throw new Error(`API key rejected (${msg}). Check it in the extension settings.`);
    throw new Error(`Claude API error ${res.status}: ${msg}`);
  }
  if (body.stop_reason === "refusal") throw new Error("Claude declined to draft these answers.");
  const text = (body.content || []).filter((b) => b.type === "text").map((b) => b.text).pop();
  if (!text) throw new Error("Claude returned no answer.");
  return JSON.parse(text).answers;
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type !== "draft") return false;
  draft(msg.payload)
    .then((answers) => sendResponse({ ok: true, answers }))
    .catch((err) => sendResponse({ ok: false, error: String(err.message || err) }));
  return true; // keep the channel open for the async response
});

chrome.action.onClicked.addListener(() => chrome.runtime.openOptionsPage());
