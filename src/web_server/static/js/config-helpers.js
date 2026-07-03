// Shared helpers for config panels.
import { api, apiPut, esc } from "./api.js";
import { filterSelect, multiFilterSelect } from "./filter-select.js";

// Canonical escaping + write verbs live in api.js; re-exported here so the
// 35 existing panel importers keep working unchanged.
export { esc, esc as escapeHtml, apiPut, apiDelete } from "./api.js";

let _configCache = null;
let _channels = null;
let _roles = null;

export async function loadConfig() {
  _configCache = await api("/api/config");
  return _configCache;
}

export function getCachedConfig() { return _configCache; }

export async function loadChannels() {
  if (_channels) return _channels;
  try { _channels = await api("/api/meta/channels"); } catch (_) { _channels = []; }
  return _channels;
}

let _categories = null;
export async function loadCategories() {
  if (_categories) return _categories;
  try { _categories = await api("/api/meta/channels?types=category"); } catch (_) { _categories = []; }
  return _categories;
}

export function categorySelect(categories, selected, { allowNone = true } = {}) {
  let html = allowNone ? '<option value="0">(none)</option>' : "";
  for (const c of categories) {
    const sel = c.id === selected ? " selected" : "";
    html += `<option value="${c.id}"${sel}>${esc(c.name)}</option>`;
  }
  return html;
}

export async function loadRoles() {
  if (_roles) return _roles;
  try { _roles = await api("/api/meta/roles"); } catch (_) { _roles = []; }
  return _roles;
}

let _members = null;
export async function loadMembers() {
  if (_members) return _members;
  try { _members = await api("/api/meta/members"); } catch (_) { _members = []; }
  return _members;
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

// Typed conveniences — build the option list and the right empty sentinel.
// Single-pickers default to emptyValue "0" (the unset id config uses) so
// getValue() returns "0" when cleared, matching the old <select> behaviour.
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
  let html = allowNone ? '<option value="0">(disabled)</option>' : "";
  for (const ch of channels) {
    const sel = ch.id === selected ? " selected" : "";
    html += `<option value="${ch.id}"${sel}>#${esc(ch.name)}</option>`;
  }
  return html;
}

export function roleSelect(roles, selected, { allowNone = true } = {}) {
  let html = allowNone ? '<option value="0">(none)</option>' : "";
  for (const r of roles) {
    const sel = r.id === selected ? " selected" : "";
    html += `<option value="${r.id}"${sel}>@${esc(r.name)}</option>`;
  }
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
  el.className = `save-status ${ok ? "save-ok" : "save-err"}`;
  el.textContent = msg || (ok ? "Saved" : "Error");
  // Errors linger longer than successes, but both clear — a stale "Error"
  // next to a button outlives its usefulness once the user moves on.
  clearTimeout(el._statusTimer);
  el._statusTimer = setTimeout(() => { el.textContent = ""; }, ok ? 3000 : 8000);
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
