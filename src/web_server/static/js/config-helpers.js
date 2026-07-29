// Shared helpers for config panels.
import { api, apiDelete, apiPut, esc } from "./api.js";
import { filterSelect, multiFilterSelect } from "./filter-select.js";
import { confirmDialog, toast } from "./ui.js";

// Canonical escaping + write verbs live in api.js; re-exported here so the
// 35 existing panel importers keep working unchanged.
export { esc, esc as escapeHtml, apiPost, apiPut, apiDelete } from "./api.js";

let _configCache = null;
let _channels = null;
let _roles = null;

// ── Unsaved-changes tracker (W-C1) ─────────────────────────────────────
// Contract (app.js consumes the window globals):
//   - guardForm(form) — call once per panel mount on the form/container
//     element; any `input` or `change` event inside it marks the page dirty.
//   - window.__dkDirty()      → boolean: are there unsaved edits?
//   - window.__dkDirtyReset() → clear the flag (app.js calls this after the
//     user confirms discarding, and on every panel mount).
//   - A successful save shown via showStatus(el, true, …) clears the flag.
//   - A beforeunload handler (wired once, below) warns when dirty.
let _dirty = false;

window.__dkDirty = () => _dirty;
window.__dkDirtyReset = () => { _dirty = false; };

window.addEventListener("beforeunload", (e) => {
  if (!_dirty) return;
  e.preventDefault();
  e.returnValue = ""; // legacy browsers need a non-null returnValue
});

/**
 * Track unsaved edits on a config form. Attach once per panel mount to the
 * form (or any container) element; edits inside it set the dirty flag that
 * app.js checks before navigation/guild switches. Returns `form` for
 * chaining. See the contract comment above.
 */
export function guardForm(form) {
  const mark = () => { _dirty = true; };
  form.addEventListener("input", mark);
  form.addEventListener("change", mark);
  return form;
}

// ── Meta loaders (W-C2: failures are remembered, never silently []) ────
// A failed /api/meta/* fetch used to be swallowed to [] — legacy selects
// then rendered "(disabled)" and an unrelated save posted "0" for every
// channel/role field. Now: the failure is recorded (metaLoadFailed()),
// nothing is cached so a remount retries, and the *Select builders below
// fail SAFE by preserving the currently-saved id as a synthetic option.
const _metaFailed = new Set();

/** True when any /api/meta/* load has failed and not since succeeded. */
export function metaLoadFailed() { return _metaFailed.size > 0; }

/**
 * Inline warning banner for panels to prepend when metaLoadFailed().
 * Returns an HTML string ("" when everything loaded fine).
 */
export function renderMetaWarning() {
  if (!metaLoadFailed()) return "";
  return '<div class="meta-warning" role="alert" '
    + 'style="color:var(--red);font-size:13px;margin:0 0 12px;">'
    + "Channel and role lists failed to load. Your saved settings are kept, "
    + "but reload the page before changing channel or role fields.</div>";
}

export async function loadConfig() {
  _configCache = await api("/api/config");
  return _configCache;
}

export function getCachedConfig() { return _configCache; }

export async function loadChannels() {
  if (_channels) return _channels;
  try {
    _channels = await api("/api/meta/channels");
    _metaFailed.delete("channels");
  } catch (_) {
    _metaFailed.add("channels");
    return [];
  }
  return _channels;
}

let _categories = null;
export async function loadCategories() {
  if (_categories) return _categories;
  try {
    _categories = await api("/api/meta/channels?types=category");
    _metaFailed.delete("categories");
  } catch (_) {
    _metaFailed.add("categories");
    return [];
  }
  return _categories;
}

// Fail-safe option HTML for legacy <select> builders when the backing meta
// list failed to load: keep the saved id selected (so a save on an unrelated
// field can't zero it) and surface the failure as a disabled option.
function _failedSelectOptions(kind, selected, noneLabel) {
  let html = "";
  const id = String(selected || "0");
  if (id !== "0") {
    html += `<option value="${esc(id)}" selected>Current setting (id ${esc(id)})</option>`;
  } else {
    html += `<option value="0" selected>${noneLabel}</option>`;
  }
  html += `<option disabled>${kind} failed to load — reload before saving</option>`;
  return html;
}

export function categorySelect(categories, selected, { allowNone = true } = {}) {
  if (_metaFailed.has("categories") && !categories.length) {
    return _failedSelectOptions("Categories", selected, "(none)");
  }
  let html = allowNone ? '<option value="0">(none)</option>' : "";
  for (const c of categories) {
    const sel = c.id === selected ? " selected" : "";
    html += `<option value="${c.id}"${sel}>${esc(c.name)}</option>`;
  }
  return html;
}

export async function loadRoles() {
  if (_roles) return _roles;
  try {
    _roles = await api("/api/meta/roles");
    _metaFailed.delete("roles");
  } catch (_) {
    _metaFailed.add("roles");
    return [];
  }
  return _roles;
}

let _members = null;
let _bots = null;

export async function loadMembers() {
  if (_members) return _members;
  try {
    _members = await api("/api/meta/members");
    _metaFailed.delete("members");
  } catch (_) {
    _metaFailed.add("members");
    return [];
  }
  return _members;
}

export async function loadBots() {
  if (_bots) return _bots;
  try {
    _bots = await api("/api/meta/bots");
    _metaFailed.delete("bots");
  } catch (_) {
    _metaFailed.add("bots");
    return [];
  }
  return _bots;
}

// ── Searchable picker adapters ──────────────────────────────────────────
// Convert the /api/meta/* records into the {id, label} option shape the
// filter-select widgets expect, then mount a picker in place of a slot node.
// Panels render a placeholder (e.g. <span data-picker="welcome_channel_id">)
// in their innerHTML and call one of the mount* helpers below afterwards,
// holding the returned handle to read getValue()/getValues() on save.

export function toChannelOptions(channels) {
  return channels.map((c) => ({ id: String(c.id), label: `#${c.name}` }));
}
export function toRoleOptions(roles) {
  return roles.map((r) => ({ id: String(r.id), label: `@${r.name}` }));
}
export function toCategoryOptions(categories) {
  return categories.map((c) => ({ id: String(c.id), label: c.name }));
}
export function toMemberOptions(members) {
  return members.map((m) => {
    const left = m.left_server ? " (left)" : "";
    const base = m.display_name && m.display_name !== m.name
      ? `${m.display_name} (${m.name})`
      : m.name;
    return { id: String(m.id), label: `${base}${left}` };
  });
}

function _normalizeIds(values) {
  if (Array.isArray(values)) return values.map(String).filter(Boolean);
  return String(values || "").split(",").map((s) => s.trim()).filter(Boolean);
}

// Mount a single-value searchable picker, replacing `slotEl`. `opts` is passed
// through to filterSelect (so callers can supply `filter`, `emptyLabel`, etc.).
// Pass `label` with the visible field label to give the search input an
// accessible name (aria-label) — otherwise every picker announces only its
// placeholder. Applies to every mount* helper below.
export function mountPicker(slotEl, options, value, opts = {}) {
  const fs = filterSelect(opts.placeholder || "Type to filter…", options, opts);
  fs.setValue(value);
  slotEl.replaceWith(fs.el);
  return fs;
}

// Mount a multi-value chip picker, replacing `slotEl`.
export function mountMultiPicker(slotEl, options, values, opts = {}) {
  const fs = multiFilterSelect(opts.placeholder || "Type to filter…", options, opts);
  fs.setValues(_normalizeIds(values));
  slotEl.replaceWith(fs.el);
  return fs;
}

/** Fire `cb` when a filterSelect's value changes.
 *
 *  filterSelect has no change event of its own — selecting closes the popover
 *  and focus leaves afterwards, hence focusout plus a short settle delay.
 *  Note the listener goes on `fs.el`, not the slot element you passed to
 *  mountPicker: that slot was replaced out of the DOM by the mount. */
export function onPickerChange(fs, cb) {
  let last = fs.getValue();
  fs.el.addEventListener("focusout", () => {
    setTimeout(() => {
      const cur = fs.getValue();
      if (cur !== last) { last = cur; cb(); }
    }, 200);
  });
}

// Typed conveniences — build the option list and the right empty sentinel.
// Single-pickers default to emptyValue "0" (the unset id config uses) so
// getValue() returns "0" when cleared, matching the old <select> behavior.
export function mountChannelPicker(slotEl, channels, value, opts = {}) {
  return mountPicker(slotEl, toChannelOptions(channels), value,
    { emptyValue: "0", emptyLabel: "(disabled)", ...opts });
}
export function mountRolePicker(slotEl, roles, value, opts = {}) {
  return mountPicker(slotEl, toRoleOptions(roles), value,
    { emptyValue: "0", emptyLabel: "(none)", ...opts });
}
export function mountCategoryPicker(slotEl, categories, value, opts = {}) {
  return mountPicker(slotEl, toCategoryOptions(categories), value,
    { emptyValue: "0", emptyLabel: "(none)", ...opts });
}
export function mountChannelMultiPicker(slotEl, channels, values, opts = {}) {
  return mountMultiPicker(slotEl, toChannelOptions(channels), values, opts);
}
export function mountRoleMultiPicker(slotEl, roles, values, opts = {}) {
  return mountMultiPicker(slotEl, toRoleOptions(roles), values, opts);
}
export function mountMemberPicker(slotEl, members, value, opts = {}) {
  return mountPicker(slotEl, toMemberOptions(members), value,
    { emptyValue: "0", emptyLabel: "(none)", ...opts });
}
export function mountMemberMultiPicker(slotEl, members, values, opts = {}) {
  return mountMultiPicker(slotEl, toMemberOptions(members), values, opts);
}

export function channelName(channels, id) {
  if (!id || id === "0") return "(disabled)";
  const ch = channels.find((c) => c.id === id);
  return ch ? `#${ch.name}` : id;
}

export function roleName(roles, id) {
  if (!id || id === "0") return "(none)";
  const r = roles.find((x) => x.id === id);
  return r ? `@${r.name}` : id;
}

export function channelSelect(channels, selected, { allowNone = true } = {}) {
  if (_metaFailed.has("channels") && !channels.length) {
    return _failedSelectOptions("Channels", selected, "(disabled)");
  }
  let html = allowNone ? '<option value="0">(disabled)</option>' : "";
  for (const ch of channels) {
    const sel = ch.id === selected ? " selected" : "";
    html += `<option value="${ch.id}"${sel}>#${esc(ch.name)}</option>`;
  }
  return html;
}

export function roleSelect(roles, selected, { allowNone = true } = {}) {
  if (_metaFailed.has("roles") && !roles.length) {
    return _failedSelectOptions("Roles", selected, "(none)");
  }
  let html = allowNone ? '<option value="0">(none)</option>' : "";
  for (const r of roles) {
    const sel = r.id === selected ? " selected" : "";
    html += `<option value="${r.id}"${sel}>@${esc(r.name)}</option>`;
  }
  return html;
}

// Fail-safe options for the legacy multi-selects: every saved id stays
// selected so a save on an unrelated field can't drop the list.
function _failedMultiOptions(kind, selectedIds) {
  let html = "";
  for (const id of selectedIds) {
    html += `<option value="${esc(id)}" selected>Current setting (id ${esc(id)})</option>`;
  }
  html += `<option disabled>${kind} failed to load — reload before saving</option>`;
  return html;
}

export function channelSelectMulti(channels, selected) {
  const selectedIds = new Set(
    (Array.isArray(selected)
      ? selected
      : String(selected || "").split(","))
      .map((s) => String(s).trim())
      .filter(Boolean),
  );
  if (_metaFailed.has("channels") && !channels.length) {
    return _failedMultiOptions("Channels", selectedIds);
  }
  let html = "";
  for (const ch of channels) {
    const sel = selectedIds.has(ch.id) ? " selected" : "";
    html += `<option value="${ch.id}"${sel}>#${esc(ch.name)}</option>`;
  }
  return html;
}

export function roleSelectMulti(roles, selected) {
  const selectedIds = new Set(
    (Array.isArray(selected)
      ? selected
      : String(selected || "").split(","))
      .map((s) => String(s).trim())
      .filter(Boolean),
  );
  if (_metaFailed.has("roles") && !roles.length) {
    return _failedMultiOptions("Roles", selectedIds);
  }
  let html = "";
  for (const r of roles) {
    const sel = selectedIds.has(r.id) ? " selected" : "";
    html += `<option value="${r.id}"${sel}>@${esc(r.name)}</option>`;
  }
  return html;
}

export function multiIdList(ids, nameMap) {
  if (!ids || !ids.length) return "<em>none</em>";
  return ids.map((id) => esc(nameMap[id] || id)).join(", ");
}

export async function saveSection(section, body) {
  return apiPut(`/api/config/${section}`, body);
}

export function showStatus(el, ok, msg) {
  if (ok) _dirty = false; // a successful save clears the unsaved-edits flag
  el.className = `save-status ${ok ? "save-ok" : "save-err"}`;
  el.textContent = msg || (ok ? "Saved" : "Error");
  // Errors linger longer than successes, but both clear — a stale "Error"
  // next to a button outlives its usefulness once the user moves on.
  clearTimeout(el._statusTimer);
  el._statusTimer = setTimeout(() => { el.textContent = ""; }, ok ? 3000 : 8000);
}

// A "these members are exempt" chip list backed by PUT/DELETE
// `${endpoint}/${user_id}`. Config-prune grew the same widget by hand first;
// this is the shared form so a fix to the chips, the confirm flow, or the
// failed-write handling lands once. `picker` is an optional member picker whose
// suggestions are kept clear of already-exempt members.
//
// Returns { add(id, name), ids() } — `add` resolves to whether the write
// actually landed, so a caller removing a row from its own list can hold off
// when it didn't.
export function mountExemptionList(listEl, opts) {
  const { endpoint, picker, emptyText, confirmTitle, confirmLabel, confirmText } = opts;
  let items = (opts.items || []).slice();
  // Held as a live Set rather than rebuilt per candidate: the picker's filter
  // runs once per member on every keystroke.
  const excluded = new Set(items.map((e) => String(e.id)));

  function refresh() {
    if (!items.length) {
      listEl.innerHTML = `<div class="empty" style="padding:8px 0;">${esc(emptyText)}</div>`;
    } else {
      listEl.innerHTML = `<div class="exempt-chips">${items
        .map(
          (e) =>
            `<span class="exempt-chip"><span>${esc(e.name)}</span><button type="button" data-remove-exempt="${esc(e.id)}" aria-label="Stop exempting ${esc(e.name)}" title="Stop exempting ${esc(e.name)}">×</button></span>`
        )
        .join("")}</div>`;
      listEl.querySelectorAll("[data-remove-exempt]").forEach((btn) => {
        btn.addEventListener("click", () => remove(btn.dataset.removeExempt));
      });
    }
    if (picker) picker.setFilter((o) => !excluded.has(String(o.id)));
  }

  async function remove(id) {
    const entry = items.find((e) => String(e.id) === String(id));
    const ok = await confirmDialog(confirmText(entry ? entry.name : "this member"), {
      title: confirmTitle,
      danger: true,
      confirmLabel,
    });
    if (!ok) return;
    try {
      await apiDelete(`${endpoint}/${id}`);
    } catch (err) {
      toast(err.message, "error");
      return;
    }
    items = items.filter((e) => String(e.id) !== String(id));
    excluded.delete(String(id));
    refresh();
  }

  async function add(id, name) {
    try {
      // Member ids stay strings on the wire.
      await apiPut(`${endpoint}/${id}`, {});
    } catch (err) {
      toast(err.message, "error");
      return false;
    }
    items.push({ id: String(id), name });
    items.sort((a, b) => a.name.localeCompare(b.name));
    excluded.add(String(id));
    refresh();
    return true;
  }

  refresh();
  return { add, ids: () => excluded };
}

// Cancel a pending showStatus blanking and drop its styling. Callers that write
// their own text into a status element afterwards need this — otherwise the
// timer armed by an earlier error wipes the new message mid-read, and the
// save-err class leaves it red. Owned here so showStatus's timer field stays
// private to this module.
export function clearStatus(el) {
  clearTimeout(el._statusTimer);
  el._statusTimer = null;
  el.className = "";
  el.textContent = "";
}

export function buildField(labelText, control, hint) {
  const div = document.createElement("div");
  div.className = "field";
  const lbl = document.createElement("label");
  lbl.textContent = labelText;
  div.appendChild(lbl);
  div.appendChild(control);
  if (hint) {
    const h = document.createElement("div");
    h.className = "field-hint";
    h.textContent = hint;
    div.appendChild(h);
  }
  return div;
}

// ── Labelled form controls ─────────────────────────────────────────────
// buildField renders a bare <label>; `field` additionally pairs it with its
// control by id so screen readers announce the label and a label tap focuses
// the input (W-A7). Every panel wants that, so it lives here rather than
// being re-derived per panel.
//
// Note these are not yet the only copies: config-birthday, config-bulk-cleanup,
// config-confessions, config-dms and config-starboard each still carry a
// byte-identical private `field` (config-bios calls its copy `labeledField`),
// and the same three panels duplicate a `toggleField`/`buildNumberInput` pair
// that `checkbox`/`numInput` here subsume. Converting them is a mechanical
// import swap and good follow-up work; prefer importing from here in new code.
let _fieldSeq = 0;

export function field(labelText, control, hint) {
  const div = buildField(labelText, control, hint);
  if (control instanceof HTMLElement && /^(INPUT|SELECT|TEXTAREA)$/.test(control.tagName)) {
    const id = control.id || `dk-field-${++_fieldSeq}`;
    control.id = id;
    div.querySelector("label").htmlFor = id;
  }
  return div;
}

export function numInput(name, value, min, step = "1", max = null) {
  const inp = document.createElement("input");
  inp.type = "number";
  inp.name = name;
  inp.required = true;
  inp.min = String(min);
  if (max != null) inp.max = String(max);
  inp.step = step;
  inp.value = String(value);
  inp.style.maxWidth = "160px";
  return inp;
}

export function checkbox(name, checked, labelText) {
  const label = document.createElement("label");
  label.style.cssText = "display:flex; gap:6px; align-items:center;";
  const inp = document.createElement("input");
  inp.type = "checkbox";
  inp.name = name;
  inp.checked = !!checked;
  label.append(inp, document.createTextNode(" " + labelText));
  return label;
}

/** A titled `.card` section appended to `parent` (usually the panel's form). */
export function sectionCard(parent, title) {
  const el = document.createElement("div");
  el.className = "card";
  const lbl = document.createElement("div");
  lbl.className = "section-label";
  lbl.textContent = title;
  el.appendChild(lbl);
  parent.appendChild(el);
  return el;
}
