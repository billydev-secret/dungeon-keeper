// Shared helpers for wellness SPA panels.
//
// Wellness pages are member-facing: an expired session redirects to Discord
// OAuth with a return_to (not the staff /login page), and mutation endpoints
// signal soft failure via {ok: false, error: "..."} on a 200.
import { request } from "./api.js";

function redirectToWellnessLogin() {
  const url = new URL("/auth/discord", window.location.origin);
  url.searchParams.set("return_to", window.location.href);
  window.location = url.toString();
}

export function wGet(path) {
  return request("GET", path, { on401: redirectToWellnessLogin });
}

async function _mutate(method, path, body) {
  const data = await request(method, path, { body, on401: redirectToWellnessLogin });
  if (data.ok === false) throw new Error(data.error || data.detail || "Request failed");
  return data;
}

export function wPost(path, body) { return _mutate("POST", path, body); }
export function wPut(path, body) { return _mutate("PUT", path, body); }
export function wDelete(path) { return _mutate("DELETE", path); }

export { esc } from "./api.js";
export { showStatus } from "./config-helpers.js";
