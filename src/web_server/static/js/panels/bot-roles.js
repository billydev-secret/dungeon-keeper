import {
  esc, mountAsync, apiPost, loadRoles, mountRolePicker,
} from "../config-helpers.js";
import { api } from "../api.js";
import { confirmDialog, toast } from "../ui.js";

// Bot-Managed Roles. The one page that can show every role Dungeon Keeper
// makes for itself — the five ping dials, the two it now makes only when you
// offer them, and the nine that features used to make on their own with
// nothing anywhere listing them.
//
// This is an audit surface, not a form: the job is "what has this bot done to
// my role list, is any of it broken, what do I do about it". So it opens with
// a sentence rather than a row of big-number tiles (the counts are 16 and 1 —
// numbers a person reads faster in prose), it groups by whether the bot HANDS
// THE ROLE OUT (which is the only thing that decides whether its position in
// your role list matters), and every card carries a sentence saying what
// happens next rather than leaving a badge to imply it.
//
// No table anywhere, so nothing needs an overflow-x wrapper; the action row
// wraps on a phone instead of scrolling.

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

const GROUPS = [
  {
    id: "handed_out",
    label: "Roles I hand out",
    blurb: "I add and remove these myself, so each one has to sit below "
      + "Dungeon Keeper in Server Settings → Roles.",
  },
  {
    id: "pointed_at",
    label: "Roles I only point at",
    blurb: "Holding one of these grants nothing from me. I mention them, or "
      + "name them in a room's permissions — I never hand them out, so where "
      + "they sit in your role list doesn't matter.",
  },
];

function roleCard(role) {
  const badge = BADGE[role.state] || { text: role.state, cls: "badge-dim" };
  const notes = (role.notes || [])
    .map((n) => `<div class="field-hint">${esc(n)}</div>`)
    .join("");
  const members = role.role_id
    ? `<div class="field-hint">@${esc(role.current_name || role.name)} · id ${esc(role.role_id)}</div>`
    : "";
  const actions = [];
  if (role.can_create) {
    actions.push(`<button type="button" class="btn btn-sm" data-act="create"
      data-key="${esc(role.key)}">Make it now</button>`);
  }
  if (role.can_adopt) {
    actions.push(`<button type="button" class="btn btn-sm btn-ghost" data-act="adopt-open"
      data-key="${esc(role.key)}">Use a different role</button>`);
  }
  if (role.can_stop && role.role_id) {
    actions.push(`<button type="button" class="btn btn-sm btn-ghost" data-act="stop"
      data-key="${esc(role.key)}">Stop managing</button>`);
  }
  if (role.state === "offer_first" || (role.state === "deleted" && !role.can_create)) {
    actions.push(`<a class="btn btn-sm" href="#/onboarding">Offer it in onboarding</a>`);
  }
  if (role.panel) {
    actions.push(`<a class="btn btn-sm btn-ghost" href="#/${esc(role.panel)}">Open ${esc(role.panel_label || "settings")}</a>`);
  }

  return `
    <div class="card" style="margin-bottom:var(--s-3)" data-card="${esc(role.key)}">
      <div style="display:flex; flex-wrap:wrap; gap:var(--s-2); align-items:baseline">
        <strong>${esc(role.emoji)} @${esc(role.name)}</strong>
        <span class="badge ${badge.cls}">${esc(badge.text)}</span>
      </div>
      <div style="margin-top:var(--s-1)">${esc(role.headline)}</div>
      ${members}
      ${notes}
      <div style="display:flex; flex-wrap:wrap; gap:var(--s-2); margin-top:var(--s-3)">
        ${actions.join("")}
      </div>
      <div data-adopt hidden style="margin-top:var(--s-3); display:flex;
           flex-wrap:wrap; gap:var(--s-2); align-items:center">
        <span data-picker></span>
        <button type="button" class="btn btn-sm btn-primary" data-act="adopt"
                data-key="${esc(role.key)}">Point it here</button>
      </div>
    </div>`;
}

export function mount(container) {
  mountAsync(container, async (root) => {
    // Both fetches up front and the rejection is allowed to reach mountAsync,
    // so a failure renders the error WITH a working Try again rather than a
    // dead spinner.
    const [data, roles] = await Promise.all([api("/api/bot-roles"), loadRoles()]);
    // One delegated listener for the life of the panel: render() runs again
    // after every write, and binding inside it would stack a fresh handler on
    // the container each time (three "Make it now" requests on the third
    // click).
    pickers.clear();
    wire(root, roles);
    render(root, data);
  }, { errorMsg: "Couldn't read this server's bot-managed roles." });
}

function render(root, data) {
  const all = data.roles || [];
  const groups = GROUPS.map((g) => {
    const items = all.filter((r) => r.group === g.id);
    if (!items.length) return "";
    return `
      <div class="section-label">${esc(g.label)}</div>
      <p class="field-hint">${esc(g.blurb)}</p>
      ${items.map(roleCard).join("")}`;
  }).join("");

  root.innerHTML = `
    <div class="panel">
      <header><h2>Bot-Managed Roles</h2></header>
      <p class="field-hint">
        Roles Dungeon Keeper makes for itself, and how each one is doing.
        Everything else in your role list is yours — nothing here touches it.
      </p>
      <p style="font-size:var(--t-4); line-height:1.4; margin:var(--s-4) 0">
        ${esc(data.summary || "")}
      </p>
      ${data.can_manage_roles ? "" : `<p class="error">The bot doesn't have
        <strong>Manage Roles</strong>, so it can't make or change any of these.
        Grant it in Server Settings → Roles → Dungeon Keeper.</p>`}
      ${groups}
      <p class="field-hint" style="margin-top:var(--s-5)">
        “Stop managing” only stops me pointing at a role — I never delete one,
        and anybody holding it keeps it.
      </p>
    </div>`;
}

// The pickers a "Use a different role" row has mounted, keyed by dial. A
// filterSelect is a widget, not a <select>, so its value comes from the handle.
const pickers = new Map();

function wire(root, roles) {
  root.addEventListener("click", async (ev) => {
    const btn = ev.target.closest("button[data-act]");
    if (!btn) return;
    const key = btn.dataset.key;
    const card = root.querySelector(`[data-card="${CSS.escape(key)}"]`);

    if (btn.dataset.act === "adopt-open") {
      const wrap = card.querySelector("[data-adopt]");
      wrap.hidden = !wrap.hidden;
      if (!wrap.hidden && !wrap.dataset.mounted) {
        pickers.set(key, mountRolePicker(
          wrap.querySelector("[data-picker]"), roles, "0", { label: "Role" },
        ));
        wrap.dataset.mounted = "1";
      }
      return;
    }

    if (btn.dataset.act === "create") {
      btn.disabled = true;
      try {
        const res = await apiPost("/api/bot-roles/create", { key });
        toast(`Made @${res.name}.`);
        await reload(root);
      } catch (err) {
        toast(err.message || String(err), "error");
        btn.disabled = false;
      }
      return;
    }

    if (btn.dataset.act === "adopt") {
      const roleId = pickers.get(key)?.getValue() || "0";
      if (!roleId || roleId === "0") {
        toast("Pick a role first.", "error");
        return;
      }
      try {
        const res = await apiPost("/api/bot-roles/adopt", { key, role_id: roleId });
        toast(`Now using @${res.name}.`);
        await reload(root);
      } catch (err) {
        toast(err.message || String(err), "error");
      }
      return;
    }

    if (btn.dataset.act === "stop") {
      const ok = await confirmDialog(
        "I'll stop using this role and won't make another. The role stays in "
        + "your server and everybody holding it keeps it.",
        { title: "Stop managing this role?", confirmLabel: "Stop managing" },
      );
      if (!ok) return;
      try {
        await apiPost("/api/bot-roles/stop", { key });
        toast("Stopped managing it.");
        await reload(root);
      } catch (err) {
        toast(err.message || String(err), "error");
      }
    }
  });
}

async function reload(root) {
  pickers.clear();
  render(root, await api("/api/bot-roles"));
}
