import { api } from "../api.js";
import { auditPanel, badge, tsColumn } from "../audit-helpers.js";

// These keys must match what the bot writes into audit_log verbatim — an
// unknown action falls through to rendering its raw key, and its filter option
// matches nothing at all. Six of them used to be short forms nobody wrote
// (`jail` for `jail_create`, `warn` for `warning_issue`, `pull` for
// `channel_pull`), so the Jail, Unjail, Warning, Warning Revoke, Pull and
// Remove filters each returned zero rows over a log that had plenty.
const ACTION_LABELS = {
  jail_create:     "Jail",
  jail_release:    "Unjail",
  ticket_open:     "Ticket Open",
  ticket_close:    "Ticket Close",
  ticket_reopen:   "Ticket Reopen",
  ticket_delete:   "Ticket Delete",
  ticket_dismiss:  "Ticket Dismiss",
  ticket_claim:    "Ticket Claim",
  ticket_escalate: "Ticket Escalate",
  warning_issue:   "Warning",
  warning_revoke:  "Warning Revoke",
  channel_pull:    "Pull to Channel",
  channel_remove:  "Remove from Channel",
};

const ACTION_COLORS = {
  jail_create: "badge-danger",
  jail_release: "badge-success",
  warning_issue: "badge-warning",
  ticket_open: "badge-info",
  ticket_close: "badge-dim",
};

export function mount(container) {
  return auditPanel(container, {
    title: "Moderation Audit Log",
    subtitle: "Every jail, warning, and ticket action your moderators have taken",
    empty: "No moderation actions match these filters. Jails, warnings, and ticket activity land here as moderators use them.",
    filters: [
      {
        name: "action",
        label: "Action",
        options: [
          { value: "", label: "All actions" },
          ...Object.entries(ACTION_LABELS).map(([value, label]) => ({ value, label })),
        ],
      },
    ],
    columns: [
      {
        label: "Action",
        render: (e) => badge(ACTION_LABELS[e.action] || e.action, ACTION_COLORS[e.action] || ""),
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
      return { rows: data.entries, total: data.total };
    },
  });
}
