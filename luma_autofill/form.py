"""Detect and fill registration form fields on a Luma event page.

Luma's markup is React-generated and changes without notice, so detection is
deliberately generic: find every visible input inside the registration dialog
(or the page, if no dialog is open), work out a human-readable label for it,
and tag it with a ``data-luma-autofill-id`` attribute so Python can address it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from playwright.sync_api import Locator, Page

ID_ATTR = "data-luma-autofill-id"

# Field kinds produced by the extractor.
TEXT = "text"
TEXTAREA = "textarea"
SELECT = "select"
COMBOBOX = "combobox"
RADIO = "radio"
CHECKBOXES = "checkboxes"
CHECKBOX = "checkbox"
CHOICE_KINDS = (SELECT, COMBOBOX, RADIO, CHECKBOXES)


@dataclass
class FormField:
    id: int
    kind: str
    label: str
    input_type: str = ""
    required: bool = False
    options: list[str] = field(default_factory=list)
    current: list[str] = field(default_factory=list)
    # Filled in by the matcher / drafter / user:
    values: list[str] = field(default_factory=list)
    source: str = ""  # prefilled | profile | ai | user | skipped
    flagged: bool = False
    note: str = ""

    @property
    def display_value(self) -> str:
        if self.kind == CHECKBOX:
            return "checked" if self.values and _truthy(self.values[0]) else "unchecked"
        return ", ".join(self.values)

    @property
    def is_empty(self) -> bool:
        if self.kind == CHECKBOX:
            return not (self.values and _truthy(self.values[0]))
        return not any(v.strip() for v in self.values)


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"yes", "true", "1", "checked", "agree", "y", "on"}


_EXTRACT_JS = r"""
(attr) => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const dialogs = [...document.querySelectorAll('[role="dialog"], dialog[open], .lux-modal, .modal')]
    .filter(visible);
  const root = dialogs.length ? dialogs[dialogs.length - 1] : document.body;

  const labelOf = (el) => {
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const t = clean(by.split(/\s+/).map(id => document.getElementById(id)?.innerText || '').join(' '));
      if (t) return t;
    }
    if (el.labels && el.labels.length) {
      const t = clean(el.labels[0].innerText);
      if (t) return t;
    }
    // Walk up a few levels looking for a label-ish element that precedes the input.
    let node = el.parentElement;
    for (let depth = 0; node && node !== root.parentElement && depth < 6; depth++, node = node.parentElement) {
      const cands = node.querySelectorAll('label, legend, [class*="label" i], [class*="question" i], [class*="title" i]');
      for (const c of cands) {
        if (c.contains(el)) continue;
        if (c.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) {
          const t = clean(c.innerText);
          if (t && t.length < 500) return t;
        }
      }
    }
    return clean(el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('name') || '');
  };
  const required = (el, label) =>
    !!(el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$/.test(label));
  const optionLabel = (el) => {
    if (el.labels && el.labels.length) return clean(el.labels[0].innerText);
    const wrap = el.closest('label');
    if (wrap) return clean(wrap.innerText);
    return clean(el.getAttribute('aria-label') || el.value);
  };

  const fields = [];
  let next = 0;
  const seenGroups = new Set();
  const tag = (el) => { const id = next++; el.setAttribute(attr, String(id)); return id; };

  const els = root.querySelectorAll('input, textarea, select, [role="combobox"], [role="radiogroup"], [contenteditable="true"]');
  for (const el of els) {
    if (el.closest('[aria-hidden="true"]')) continue;
    const tagName = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (['hidden', 'submit', 'button', 'file', 'image', 'reset', 'search'].includes(type)) continue;
    if (el.disabled || el.readOnly) continue;

    if (type === 'radio' || type === 'checkbox') {
      // Group by name, or by the closest fieldset/radiogroup container.
      const container = el.closest('fieldset, [role="radiogroup"], [role="group"]');
      const key = el.name ? 'n:' + el.name : (container ? container : el);
      if (seenGroups.has(key)) continue;
      seenGroups.add(key);
      const members = el.name
        ? [...root.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`)]
        : (container ? [...container.querySelectorAll(`input[type="${type}"]`)] : [el]);
      const vis = members.filter(m => visible(m) || visible(m.closest('label') || m));
      if (!vis.length) continue;
      if (type === 'checkbox' && vis.length === 1 && !container) {
        const lbl = optionLabel(el) || labelOf(el);
        fields.push({ id: tag(el), kind: 'checkbox', label: lbl, input_type: type,
                      required: required(el, lbl), options: [], current: el.checked ? ['yes'] : [] });
        continue;
      }
      const groupLabel = container
        ? clean((container.querySelector('legend') || {}).innerText || container.getAttribute('aria-label') || '') || labelOf(container)
        : labelOf(el);
      const id = next++;
      vis.forEach((m, i) => m.setAttribute(attr, `${id}-${i}`));
      fields.push({ id, kind: type === 'radio' ? 'radio' : 'checkboxes', label: groupLabel, input_type: type,
                    required: vis.some(m => m.required) || /\*\s*$/.test(groupLabel),
                    options: vis.map(optionLabel), current: vis.filter(m => m.checked).map(optionLabel) });
      continue;
    }
    if (el.getAttribute('role') === 'radiogroup') continue; // handled through its inputs
    if (!visible(el)) continue;
    if (el.closest('[role="combobox"]') && el.getAttribute('role') !== 'combobox') continue;

    const label = labelOf(el);
    if (tagName === 'select') {
      const opts = [...el.options].filter(o => o.value !== '' && !o.disabled).map(o => clean(o.text));
      const cur = el.selectedIndex >= 0 && el.options[el.selectedIndex].value !== '' ? [clean(el.options[el.selectedIndex].text)] : [];
      fields.push({ id: tag(el), kind: 'select', label, input_type: 'select', required: required(el, label), options: opts, current: cur });
    } else if (el.getAttribute('role') === 'combobox' && tagName !== 'input') {
      fields.push({ id: tag(el), kind: 'combobox', label, input_type: 'combobox', required: required(el, label),
                    options: [], current: clean(el.innerText) ? [clean(el.innerText)] : [] });
    } else if (tagName === 'textarea' || el.isContentEditable) {
      const cur = clean(tagName === 'textarea' ? el.value : el.innerText);
      fields.push({ id: tag(el), kind: 'textarea', label, input_type: 'textarea', required: required(el, label), options: [], current: cur ? [cur] : [] });
    } else {
      const cur = clean(el.value);
      fields.push({ id: tag(el), kind: 'text', label, input_type: type || 'text', required: required(el, label), options: [], current: cur ? [cur] : [] });
    }
  }
  return { inDialog: dialogs.length > 0, fields };
}
"""


def extract_fields(page: Page) -> list[FormField]:
    result = page.evaluate(_EXTRACT_JS, ID_ATTR)
    fields = [FormField(**f) for f in result["fields"]]
    for f in fields:
        f.label = f.label.rstrip("* ").strip()  # required-ness is already captured
    return [f for f in fields if f.label or f.kind in CHOICE_KINDS]


def _loc(page: Page, field_id: int | str) -> Locator:
    return page.locator(f'[{ID_ATTR}="{field_id}"]').first


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def best_option(options: list[str], wanted: str) -> str | None:
    """Return the option that best matches ``wanted`` (exact, then substring)."""
    w = _norm(wanted)
    if not w:
        return None
    for opt in options:
        if _norm(opt) == w:
            return opt
    for opt in options:
        n = _norm(opt)
        if n and (w in n or n in w):
            return opt
    return None


def fill_field(page: Page, f: FormField) -> None:
    """Write ``f.values`` into the page. Raises on failure."""
    if f.source == "skipped":
        return
    if f.kind in (TEXT, TEXTAREA):
        loc = _loc(page, f.id)
        value = f.values[0] if f.values else ""
        if loc.get_attribute("contenteditable") == "true":
            loc.click()
            loc.press("ControlOrMeta+a")
            loc.press_sequentially(value)
        else:
            loc.fill(value)
    elif f.kind == SELECT:
        if f.values:
            _loc(page, f.id).select_option(label=f.values[0])
    elif f.kind == CHECKBOX:
        _loc(page, f.id).set_checked(bool(f.values) and _truthy(f.values[0]), force=True)
    elif f.kind in (RADIO, CHECKBOXES):
        wanted = {_norm(v) for v in f.values}
        for i, opt in enumerate(f.options):
            should = _norm(opt) in wanted
            if f.kind == RADIO and not should:
                continue
            _check_option(page, f"{f.id}-{i}", should)
    elif f.kind == COMBOBOX:
        if not f.values:
            return
        loc = _loc(page, f.id)
        loc.click()
        option = page.get_by_role("option", name=f.values[0], exact=False).first
        option.wait_for(state="visible", timeout=5000)
        option.click()


def _check_option(page: Page, attr_id: str, checked: bool) -> None:
    loc = _loc(page, attr_id)
    if loc.is_checked() == checked:
        return
    # Luma often hides the real input behind a styled label; click the label.
    if loc.is_visible():
        loc.set_checked(checked)
    else:
        loc.evaluate("el => (el.closest('label') || el).click()")

