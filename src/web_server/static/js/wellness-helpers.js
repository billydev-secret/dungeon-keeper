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

// Friendly labels for the raw enum values the API serves. The Discord setup
// wizard has always shown labels like these; the web selects showed bare
// enums ("slow_mode", "ephemeral") until 2026-07-30. "ephemeral" in
// particular needs honest wording: it is an in-channel reply the whole room
// can see for ~30 seconds, not a private notice.
export const ENFORCEMENT_LABELS = {
  gentle: "💛 Gentle — a heads-up, nothing more",
  slow_mode: "🐢 Slow mode — hold me to my caps",
  gradual: "🌱 Gradual — heads-up first, slow mode if I keep going",
  cooldown: "☕ Cooldown (legacy)", // pre-2026-07-30 rows only; not selectable
};

export const NOTIFICATION_LABELS = {
  ephemeral: "In-channel reply (visible to the room ~30s)",
  dm: "DM only (private)",
  both: "In-channel + DM",
};

export function enfLabel(value) { return ENFORCEMENT_LABELS[value] || value; }
export function notifLabel(value) { return NOTIFICATION_LABELS[value] || value; }
