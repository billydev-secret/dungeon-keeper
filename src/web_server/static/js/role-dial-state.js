import { esc } from "./config-helpers.js";
import { api } from "./api.js";

// One line of truth under a role picker.
//
// The defect this fixes: a picker showing "(none)" renders identically for
// three completely different situations — the admin never touched it, the
// admin deliberately switched it off, and an unrelated whole-form save wrote a
// "0" over it (every config panel sends `picker.getValue() || "0"` for EVERY
// field on EVERY save). An admin looking at that picker cannot tell which, and
// on the dials the bot provisions from, the difference decides whether a role
// gets made at all.
//
// So each dial that can make a role now says, in `.field-hint` register,
// which of those it is in and what happens next — sourced from the same
// endpoint the Bot-Managed Roles page reads, so the two can never disagree.
//
// **No cache.** A guild-scoped cache here would survive a guild switch and
// show one server's roles inside another, which is exactly what
// `resetMetaCaches()` exists to prevent; each panel pays one small request on
// mount instead.

const BADGE = {
  in_use: { text: "In use", cls: "badge-success" },
  out_of_reach: { text: "Out of reach", cls: "badge-warning" },
  deleted: { text: "Deleted", cls: "badge-danger" },
  inherited: { text: "Inherited", cls: "badge-info" },
  turned_off: { text: "Off", cls: "badge-dim" },
  not_made: { text: "Not made yet", cls: "badge-dim" },
  adoptable: { text: "Already there", cls: "badge-info" },
  offer_first: { text: "Offer it first", cls: "badge-dim" },
};

function lineHtml(card) {
  const badge = BADGE[card.state] || { text: card.state, cls: "badge-dim" };
  const extra = card.state === "offer_first"
    ? ' <a href="#/onboarding">Offer it in onboarding</a>'
    : "";
  return `
    <span class="badge ${badge.cls}">${esc(badge.text)}</span>
    ${esc(card.headline)}
    <a href="#/bot-roles">Bot-Managed Roles</a>${extra}`;
}

/**
 * Fill every `[data-role-state="<key>"]` element under `root` with that dial's
 * live state.
 *
 * Deliberately best-effort and non-blocking: the dial itself still works if
 * this fetch fails, so a failure leaves the placeholder empty rather than
 * taking the whole panel's mount down with it. Await the returned promise only
 * if you want to sequence something after it.
 */
export async function mountRoleDialStates(root) {
  const slots = [...root.querySelectorAll("[data-role-state]")];
  if (!slots.length) return;
  const keys = [...new Set(slots.map((el) => el.dataset.roleState))];
  let data;
  try {
    data = await api(`/api/bot-roles/state?keys=${encodeURIComponent(keys.join(","))}`);
  } catch (_err) {
    // The picker above still saves; a missing explanatory line is a smaller
    // failure than a panel that won't load.
    return;
  }
  const byKey = new Map((data.roles || []).map((c) => [c.key, c]));
  for (const el of slots) {
    const card = byKey.get(el.dataset.roleState);
    if (!card || !el.isConnected) continue;
    el.classList.add("field-hint");
    el.innerHTML = lineHtml(card);
  }
}
