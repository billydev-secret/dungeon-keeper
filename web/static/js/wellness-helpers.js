// Shared helpers for wellness SPA panels.

function redirectToWellnessLogin() {
  const url = new URL("/auth/discord", window.location.origin);
  url.searchParams.set("return_to", window.location.href);
  window.location = url.toString();
}

export async function wGet(path) {
  const res = await fetch(path, { credentials: "same-origin" });
  if (res.status === 401) { redirectToWellnessLogin(); return new Promise(() => {}); }
  if (!res.ok) {
    let detail = res.statusText;
    try { const b = await res.json(); if (b.error) detail = b.error; else if (b.detail) detail = b.detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}

async function _mutate(method, path, body) {
  const opts = { method, credentials: "same-origin", headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  if (res.status === 401) { redirectToWellnessLogin(); return new Promise(() => {}); }
  const data = await res.json();
  if (!res.ok || data.ok === false) throw new Error(data.error || data.detail || res.statusText);
  return data;
}

export function wPost(path, body) { return _mutate("POST", path, body); }
export function wPut(path, body) { return _mutate("PUT", path, body); }
export function wDelete(path) { return _mutate("DELETE", path); }

export function esc(s) {
  if (!s) return "";
  const d = document.createElement("div"); d.textContent = s; return d.innerHTML;
}

export function showStatus(el, ok, msg) {
  el.className = `save-status ${ok ? "save-ok" : "save-err"}`;
  el.textContent = msg || (ok ? "Saved" : "Error");
  if (ok) setTimeout(() => { el.textContent = ""; }, 3000);
}
