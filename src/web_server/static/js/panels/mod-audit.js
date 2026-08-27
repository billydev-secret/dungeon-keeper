import { api } from "../api.js";
import { auditPanel, badge, tsColumn } from "../audit-helpers.js";

// Curated names for the actions whose key does not read well on its own.
// Everything else falls through to prettyAction below, which turns
// `survivor_tasks_run` into "Survivor Tasks Run" — so a new action the bot
// starts writing shows up readable without anyone editing this file. The keys
// must match what the bot writes verbatim; six of them once did not, and each
// of those filters matched zero rows over a log that had plenty.
const ACTION_LABELS = {
  jail_create: "Jail",
  jail_release: "Unjail",
  jail_member_left: "Left While Jailed",
  warning_issue: "Warning",
  warning_revoke: "Warning Revoke",
  channel_pull: "Pull to Channel",
  channel_remove: "Remove from Channel",
  inactive_apply: "Marked Inactive",
  inactive_reactivate: "Reactivated",
  onboarding_roles_added: "Onboarding Roles Added",
};

const ACTION_COLORS = {
  jail_create: "badge-danger",
  jail_release: "badge-success",
  warning_issue: "badge-warning",
  ticket_open: "badge-info",
  ticket_close: "badge-dim",
};

/** `role_menu.elevated_override` → "Role Menu Elevated Override". */
function prettyAction(key) {
  return String(key)
    .split(/[_.]/)
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

function actionLabel(key) {
  return ACTION_LABELS[key] || prettyAction(key);
}

// The filter is filled from the vocabulary the server reports, once, on the
// first load — a hand-kept list here is what drifted from the bot in the first
// place, and it could only ever name actions somebody remembered to add.
function fillActionOptions(container, actions) {
  const sel = container.querySelector('.controls select[name="action"]');
  if (!sel || !actions || !actions.length || sel.dataset.filled) return;
  sel.dataset.filled = "1";
  for (const a of actions) {
    const o = document.createElement("option");
    o.value = a;
    o.textContent = actionLabel(a);
    sel.append(o);
  }
}

export function mount(container) {
  return auditPanel(container, {
    title: "Moderation Audit Log",
    subtitle: "Jails, warnings, tickets and other actions taken on members",
    empty: "No moderation actions match these filters. Jails, warnings, and ticket activity land here as moderators use them.",
    filters: [
      {
        name: "action",
        label: "Action",
        options: [{ value: "", label: "All actions" }],
      },
    ],
    columns: [
      {
        label: "Action",
        render: (e) => badge(actionLabel(e.action), ACTION_COLORS[e.action] || ""),
      },
      { label: "Actor", render: (e) => e.actor_name || e.actor_id },
      {
        label: "Target",
        className: "user-cell",
        render: (e) => e.target_name || e.target_id || "—",
      },
      {
        label: "Details",
        className: "reason-cell",
        render: (e) => (e.extra && e.extra.reason) || "—",
        title: (e) => (e.extra && e.extra.reason) || null,
      },
      tsColumn("created_at"),
    ],
    fetch: async (params) => {
      const data = await api("/api/moderation/audit", params);
      fillActionOptions(container, data.actions);
      return { rows: data.entries, total: data.total };
    },
  });
}
