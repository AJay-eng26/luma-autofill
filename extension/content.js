// Luma Autofill content script: adds an "Autofill" button to Luma pages.
// It fills the open registration form from your profile, asks Claude to draft
// answers to open questions, and highlights everything for review.
// It never clicks submit: you do that yourself after checking the answers.
(() => {
  if (window.__lumaAutofillLoaded) return;
  window.__lumaAutofillLoaded = true;

  const MAX_SIMPLE_LABEL = 60;
  const RULES = [
    ["linkedin", [/linked\s*in/i]],
    ["x", [/twitter/i, /\bx\s*(\(|handle|username|profile|account|url|link)/i, /x\.com/i, /^x$/i]],
    ["github", [/github/i]],
    ["email", [/e-?mail/i]],
    ["phone", [/phone/i, /mobile/i, /whats\s*app/i, /\bcell\b/i]],
    ["first_name", [/first\s*name/i, /given\s*name/i]],
    ["last_name", [/last\s*name/i, /surname/i, /family\s*name/i]],
    ["name", [/^(full\s*)?name\b/i, /your\s*(full\s*)?name/i]],
    ["company", [/company/i, /organi[sz]ation/i, /employer/i, /affiliation/i, /startup\s*name/i, /where do you work/i]],
    ["role", [/job\s*title/i, /\btitle\b/i, /\brole\b/i, /position/i, /occupation/i, /what do you do/i]],
    ["website", [/website/i, /portfolio/i, /personal\s*(site|url)/i]],
    ["location", [/\bcity\b/i, /location/i, /where are you (primarily )?based/i]],
  ];
  const TYPE_KEYS = { email: "email", tel: "phone" };
  const clean = (t) => (t || "").replace(/\s+/g, " ").trim();
  const norm = (t) => (t || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const truthy = (v) => ["yes", "true", "1", "checked", "agree", "y", "on"].includes(norm(v));

  // ---------- profile helpers ----------
  function profileValue(key, label, p) {
    const wantsUrl = /url|link|profile|https?/i.test(label);
    const handle = (p.x_handle || "").replace(/^https?:\/\/(www\.)?(x|twitter)\.com\//i, "").replace(/^@/, "").replace(/\/$/, "");
    switch (key) {
      case "x": return handle ? (wantsUrl ? `https://x.com/${handle}` : `@${handle}`) : "";
      case "linkedin": {
        const v = (p.linkedin || "").trim();
        return v && !/^https?:/i.test(v) ? `https://${v.replace(/^\/+/, "")}` : v;
      }
      case "first_name": return (p.name || "").trim().split(/\s+/)[0] || "";
      case "last_name": return (p.name || "").trim().split(/\s+/).slice(1).join(" ");
      default: return (p[key] || "").trim();
    }
  }

  function classify(f) {
    if (f.kind === "checkbox") return null;
    if (TYPE_KEYS[f.inputType]) return TYPE_KEYS[f.inputType];
    const label = f.label.replace(/\*\s*$/, "").trim();
    if (!label || label.length > MAX_SIMPLE_LABEL) return null;
    for (const [key, patterns] of RULES) if (patterns.some((re) => re.test(label))) return key;
    return null;
  }

  function bestOption(options, wanted) {
    const w = norm(wanted);
    if (!w) return null;
    return options.find((o) => norm(o) === w) || options.find((o) => norm(o) && (norm(o).includes(w) || w.includes(norm(o)))) || null;
  }

  // ---------- field detection ----------
  const visible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none";
  };

  function formRoot() {
    const dialogs = [...document.querySelectorAll('[role="dialog"], dialog[open], .lux-modal, .modal')].filter(visible);
    return dialogs.length ? dialogs[dialogs.length - 1] : document.body;
  }

  function labelOf(el, root) {
    const by = el.getAttribute("aria-labelledby");
    if (by) {
      const t = clean(by.split(/\s+/).map((id) => document.getElementById(id)?.innerText || "").join(" "));
      if (t) return t;
    }
    if (el.labels && el.labels.length && clean(el.labels[0].innerText)) return clean(el.labels[0].innerText);
    let node = el.parentElement;
    for (let depth = 0; node && node !== root.parentElement && depth < 6; depth++, node = node.parentElement) {
      for (const c of node.querySelectorAll('label, legend, [class*="label" i], [class*="question" i], [class*="title" i]')) {
        if (c.contains(el)) continue;
        if (c.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) {
          const t = clean(c.innerText);
          if (t && t.length < 500) return t;
        }
      }
    }
    return clean(el.getAttribute("aria-label") || el.getAttribute("placeholder") || el.getAttribute("name") || "");
  }

  const optionLabel = (el) =>
    clean((el.labels && el.labels[0]?.innerText) || el.closest("label")?.innerText || el.getAttribute("aria-label") || el.value);

  function extractFields() {
    const root = formRoot();
    const fields = [];
    const seen = new Set();
    let id = 0;
    for (const el of root.querySelectorAll('input, textarea, select, [role="combobox"], [contenteditable="true"]')) {
      if (el.closest('[aria-hidden="true"], #luma-autofill-panel')) continue;
      const tag = el.tagName.toLowerCase();
      const type = (el.getAttribute("type") || "").toLowerCase();
      if (["hidden", "submit", "button", "file", "image", "reset", "search"].includes(type)) continue;
      if (el.disabled || el.readOnly) continue;

      if (type === "radio" || type === "checkbox") {
        const container = el.closest('fieldset, [role="radiogroup"], [role="group"]');
        const key = el.name ? `n:${el.name}` : container || el;
        if (seen.has(key)) continue;
        seen.add(key);
        const members = el.name
          ? [...root.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`)]
          : container ? [...container.querySelectorAll(`input[type="${type}"]`)] : [el];
        const vis = members.filter((m) => visible(m) || visible(m.closest("label") || m));
        if (!vis.length) continue;
        if (type === "checkbox" && vis.length === 1 && !container) {
          const label = optionLabel(el) || labelOf(el, root);
          fields.push({ id: id++, kind: "checkbox", label, inputType: type, required: el.required || /\*\s*$/.test(label), options: [], current: el.checked ? ["yes"] : [], el, anchor: el.closest("label") || el });
          continue;
        }
        const groupLabel = container
          ? clean(container.querySelector("legend")?.innerText || container.getAttribute("aria-label") || "") || labelOf(container, root)
          : labelOf(el, root);
        fields.push({
          id: id++, kind: type === "radio" ? "radio" : "checkboxes", label: groupLabel, inputType: type,
          required: vis.some((m) => m.required) || /\*\s*$/.test(groupLabel),
          options: vis.map(optionLabel), current: vis.filter((m) => m.checked).map(optionLabel),
          members: vis, el: vis[0], anchor: container || vis[0].closest("label") || vis[0],
        });
        continue;
      }
      if (!visible(el)) continue;
      if (el.closest('[role="combobox"]') && el.getAttribute("role") !== "combobox") continue;

      const label = labelOf(el, root);
      const required = !!(el.required || el.getAttribute("aria-required") === "true" || /\*\s*$/.test(label));
      if (tag === "select") {
        const opts = [...el.options].filter((o) => o.value !== "" && !o.disabled).map((o) => clean(o.text));
        const cur = el.selectedIndex >= 0 && el.options[el.selectedIndex].value !== "" ? [clean(el.options[el.selectedIndex].text)] : [];
        fields.push({ id: id++, kind: "select", label, inputType: "select", required, options: opts, current: cur, el, anchor: el });
      } else if (el.getAttribute("role") === "combobox") {
        fields.push({ id: id++, kind: "combobox", label, inputType: "combobox", required, options: [], current: [], el, anchor: el });
      } else if (tag === "textarea" || el.isContentEditable) {
        const cur = clean(tag === "textarea" ? el.value : el.innerText);
        fields.push({ id: id++, kind: "textarea", label, inputType: "textarea", required, options: [], current: cur ? [cur] : [], el, anchor: el });
      } else {
        const cur = clean(el.value);
        fields.push({ id: id++, kind: "text", label, inputType: type || "text", required, options: [], current: cur ? [cur] : [], el, anchor: el });
      }
    }
    for (const f of fields) f.label = f.label.replace(/[*\s]+$/, "");
    return fields.filter((f) => f.label || f.options.length);
  }

  // Read the options of a custom dropdown by opening it briefly.
  async function readComboOptions(f) {
    f.el.click();
    await sleep(300);
    f.options = [...document.querySelectorAll('[role="option"]')].filter(visible).map((o) => clean(o.innerText)).filter(Boolean);
    document.activeElement?.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await sleep(150);
  }

  // ---------- filling (React-safe) ----------
  function setNativeValue(el, value) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function setChecked(input, checked) {
    if (input.checked === checked) return;
    (visible(input) ? input : input.closest("label") || input).click();
  }

  async function fill(f) {
    const values = f.values || [];
    if (f.kind === "text" || f.kind === "textarea") {
      const v = values[0] || "";
      if (f.el.isContentEditable) { f.el.focus(); document.execCommand("selectAll"); document.execCommand("insertText", false, v); }
      else setNativeValue(f.el, v);
    } else if (f.kind === "select") {
      const opt = [...f.el.options].find((o) => clean(o.text) === values[0]);
      if (opt) { f.el.value = opt.value; f.el.dispatchEvent(new Event("change", { bubbles: true })); }
    } else if (f.kind === "checkbox") {
      setChecked(f.el, values.length > 0 && truthy(values[0]));
    } else if (f.kind === "radio" || f.kind === "checkboxes") {
      const wanted = new Set(values.map(norm));
      f.members.forEach((m, i) => {
        const should = wanted.has(norm(f.options[i]));
        if (f.kind === "radio" && !should) return;
        setChecked(m, should);
      });
    } else if (f.kind === "combobox") {
      for (const v of values) {
        f.el.click();
        await sleep(300);
        const opt = [...document.querySelectorAll('[role="option"]')].filter(visible).find((o) => norm(o.innerText) === norm(v))
          || [...document.querySelectorAll('[role="option"]')].filter(visible).find((o) => norm(o.innerText).includes(norm(v)));
        if (opt) opt.click();
        await sleep(200);
      }
      document.activeElement?.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    }
  }

  // ---------- highlighting ----------
  function clearMarks() {
    document.querySelectorAll(".luma-autofill-note").forEach((n) => n.remove());
    document.querySelectorAll(".luma-autofill-profile, .luma-autofill-ai, .luma-autofill-missing")
      .forEach((n) => n.classList.remove("luma-autofill-profile", "luma-autofill-ai", "luma-autofill-missing"));
  }

  function mark(f, cls, note) {
    const target = f.kind === "radio" || f.kind === "checkboxes" ? f.anchor : f.el;
    target.classList.add(cls);
    if (!note) return;
    const n = document.createElement("span");
    n.className = "luma-autofill-note" + (cls === "luma-autofill-missing" ? " missing" : "");
    n.textContent = note;
    f.anchor.insertAdjacentElement("afterend", n);
  }

  // ---------- panel ----------
  function panel(html) {
    let p = document.getElementById("luma-autofill-panel");
    if (!p) {
      p = document.createElement("div");
      p.id = "luma-autofill-panel";
      document.body.appendChild(p);
    }
    p.innerHTML = '<span class="close" title="Close">✕</span>' + html;
  }
  const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  function draftViaBackground(payload) {
    return new Promise((resolve) => chrome.runtime.sendMessage({ type: "draft", payload }, resolve));
  }

  // ---------- opening the form ----------
  const CTA_RE = /^(register|request to join|rsvp|join event|join waitlist|apply|sign up|reserve( a)? spot|attend)\b/i;

  const dialogOpen = () =>
    [...document.querySelectorAll('[role="dialog"], dialog[open], .lux-modal, .modal')].some(visible);

  async function openForm() {
    if (dialogOpen()) return true;
    const cta = [...document.querySelectorAll("button, a[role=button]")]
      .filter((b) => visible(b) && !b.closest("#luma-autofill-panel") && b.id !== "luma-autofill-btn")
      .find((b) => CTA_RE.test(clean(b.innerText)) && !/one[- ]click/i.test(b.innerText));
    if (!cta) return false;
    cta.click();
    for (let i = 0; i < 30 && !dialogOpen(); i++) await sleep(200);
    await sleep(400); // let the form render its questions
    return dialogOpen();
  }

  // ---------- main ----------
  async function run() {
    const { profile } = await chrome.storage.local.get("profile");
    if (!profile || !profile.name) {
      panel("No profile saved yet. Click the extension icon (puzzle piece → Luma Autofill) to add your details.");
      return;
    }
    await openForm();
    const fields = extractFields();
    if (!fields.length) {
      panel("No form found. Click Luma's <b>Register</b> / <b>Request to Join</b> button first, then click Autofill.");
      return;
    }
    clearMarks();
    for (const f of fields) if (f.kind === "combobox") await readComboOptions(f);

    const unanswered = [];
    for (const f of fields) {
      const key = classify(f);
      let value = key ? profileValue(key, f.label, profile) : "";
      if (value && f.options.length) value = bestOption(f.options, value) || "";
      if (value) { f.values = [value]; f.source = "profile"; }
      else if (f.current.length && f.kind !== "checkbox") { f.values = f.current; f.source = "prefilled"; }
      else unanswered.push(f);
    }

    let aiError = "";
    if (unanswered.length) {
      panel(`Asking Claude about ${unanswered.length} question(s)…`);
      const main = document.querySelector("main") || document.body;
      const res = await draftViaBackground({
        profile,
        event: `URL: ${location.href}\nTitle: ${document.title}\n\n${main.innerText}`,
        questions: unanswered.map((f) => ({
          field_id: f.id, question: f.label, type: f.kind === "checkbox" ? "single checkbox" : f.kind,
          required: f.required, ...(f.options.length ? { options: f.options } : {}),
        })),
      });
      if (!res || !res.ok) aiError = res?.error || "No response from the extension.";
      const byId = new Map((res?.answers || []).map((a) => [a.field_id, a]));
      for (const f of unanswered) {
        const a = byId.get(f.id);
        f.source = "ai";
        let values = (a?.values || []).map((v) => v.trim()).filter(Boolean);
        if (f.options.length && f.kind !== "checkbox") values = values.map((v) => bestOption(f.options, v)).filter(Boolean);
        f.values = values;
        f.note = a ? [a.needs_input ? "Needs your input." : "", a.note, a.confidence !== "high" ? `(confidence: ${a.confidence})` : ""].filter(Boolean).join(" ") : "AI couldn't answer this. Please fill it in.";
      }
    }

    let filled = 0, ai = 0, missing = 0;
    for (const f of fields) {
      if (f.source !== "prefilled" && f.values?.length) {
        try { await fill(f); filled++; } catch (e) { f.note = `Couldn't fill automatically: ${e.message}`; }
      }
      const empty = f.kind === "checkbox" ? !(f.values?.length && truthy(f.values[0])) : !(f.values || []).some((v) => v.trim());
      if (f.required && empty) { missing++; mark(f, "luma-autofill-missing", f.note || "Required: please fill this in."); }
      else if (f.source === "ai") { ai++; mark(f, "luma-autofill-ai", "AI draft — please check. " + (f.note || "")); }
      else if (f.source === "profile") mark(f, "luma-autofill-profile");
    }

    const approval = /approval required|requires? approval|request to join|subject to (host )?approval/i.test(document.body.innerText);
    panel(
      `Filled <b>${filled}</b> field(s) from your profile and AI.<br>` +
      (ai ? `🟨 <b>${ai}</b> AI answer(s) highlighted in yellow: check them.<br>` : "") +
      (missing ? `🟥 <b>${missing}</b> required field(s) still empty (red).<br>` : "") +
      (aiError ? `⚠️ AI drafting failed: ${esc(aiError)}<br>` : "") +
      (approval ? "ℹ️ This event needs host approval.<br>" : "") +
      "<br>When everything looks right, click Luma's submit button yourself."
    );
  }

  const btn = document.createElement("button");
  btn.id = "luma-autofill-btn";
  btn.textContent = "✨ Autofill";
  btn.title = "Fill this Luma form (shortcut: Alt+A)";
  let busy = false;
  async function trigger() {
    if (busy) return;
    busy = true;
    btn.disabled = true;
    btn.textContent = "Filling…";
    try { await run(); }
    catch (e) { panel(`Something went wrong: ${esc(String(e.message || e))}`); }
    finally { busy = false; btn.disabled = false; btn.textContent = "✨ Autofill"; }
  }
  document.body.appendChild(btn);

  // Luma closes its form on any pointer press or focus outside it. Swallow those events
  // for our own button/panel (window capture runs before the page's listeners) and keep
  // focus where it is, so the open form survives a click on Autofill.
  const ours = (t) => t instanceof Node && (btn.contains(t) || document.getElementById("luma-autofill-panel")?.contains(t));
  for (const type of ["pointerdown", "mousedown", "touchstart", "pointerup", "mouseup", "click", "focusin"]) {
    window.addEventListener(type, (e) => {
      if (!ours(e.target)) return;
      e.stopImmediatePropagation();
      if (type === "pointerdown" || type === "mousedown") e.preventDefault(); // don't move focus
      if (type === "click" && btn.contains(e.target)) trigger();
      if (type === "click" && e.target.classList?.contains("close")) e.target.closest("#luma-autofill-panel")?.remove();
    }, true);
  }
  window.addEventListener("keydown", (e) => {
    if (e.altKey && !e.ctrlKey && !e.metaKey && e.key.toLowerCase() === "a") { e.preventDefault(); trigger(); }
  }, true);

  window.__lumaAutofill = { run, trigger, extractFields, classify, profileValue }; // for tests
})();
