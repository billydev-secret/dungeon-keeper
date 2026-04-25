// Shared helpers for config panels.
import { api } from "./api.js";

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

export async function loadRoles() {
  if (_roles) return _roles;
  try { _roles = await api("/api/meta/roles"); } catch (_) { _roles = []; }
  return _roles;
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
    html += `<option value="${ch.id}"${sel}>#${ch.name}</option>`;
  }
  return html;
}

export function roleSelect(roles, selected, { allowNone = true } = {}) {
  let html = allowNone ? '<option value="0">(none)</option>' : "";
  for (const r of roles) {
    const sel = r.id === selected ? " selected" : "";
    html += `<option value="${r.id}"${sel}>@${r.name}</option>`;
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
    html += `<option value="${ch.id}"${sel}>#${ch.name}</option>`;
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
    html += `<option value="${r.id}"${sel}>@${r.name}</option>`;
  }
  return html;
}

export function multiIdList(ids, nameMap) {
  if (!ids || !ids.length) return "<em>none</em>";
  return ids.map((id) => nameMap[id] || id).join(", ");
}

export async function saveSection(section, body) {
  return api(`/api/config/${section}`, null, { method: "PUT", body });
}

// Patched api() that supports PUT with JSON body
export async function apiPut(path, body) {
  const res = await fetch(path, {
    method: "PUT",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { const b = await res.json(); if (b.detail) detail = b.detail; } catch (_) {}
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json();
}

export async function apiDelete(path) {
  const res = await fetch(path, {
    method: "DELETE",
    credentials: "same-origin",
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { const b = await res.json(); if (b.detail) detail = b.detail; } catch (_) {}
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json();
}

export function showStatus(el, ok, msg) {
  el.className = `save-status ${ok ? "save-ok" : "save-err"}`;
  el.textContent = msg || (ok ? "Saved" : "Error");
  if (ok) setTimeout(() => { el.textContent = ""; }, 3000);
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
