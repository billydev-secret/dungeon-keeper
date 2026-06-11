import { api } from "../api.js";
import { auditPanel, badge, tsColumn } from "../audit-helpers.js";

const ACTION_LABELS = {
  submit:        "Submit",
  delete:        "Delete",
  solve:         "Solve",
  guess_cap_hit: "Guess Cap Hit",
};

const ACTION_BADGE = {
  submit: "badge-info",
  delete: "badge-danger",
  solve: "badge-success",
  guess_cap_hit: "badge-warning",
};

function fmtDetails(raw) {
  if (!raw) return "—";
  try {
    const parsed = typeof raw === "string" ? JSON.parse(raw) : raw;
    if (!parsed || typeof parsed !== "object" || Object.keys(parsed).length === 0) return "—";
    return Object.entries(parsed)
      .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`)
      .join(", ");
  } catch (_) {
    return String(raw);
  }
}

export function mount(container) {
  // The guess audit endpoint returns raw actor snowflakes only; resolve
  // display names via the same /api/meta/members lookup other panels use.
  let memberNames = null; // Map<id, display name>, loaded once per mount

  return auditPanel(container, {
    title: "Guess Who Audit",
    subtitle: "Recent submit, delete, solve, and guess-cap events for the Guess game",
    empty: "No audit events yet.",
    filters: [
      {
        name: "action",
        label: "Action",
        options: [
          { value: "", label: "All" },
          ...Object.entries(ACTION_LABELS).map(([value, label]) => ({ value, label })),
        ],
      },
    ],
    columns: [
      {
        label: "Action",
        render: (e) => badge(ACTION_LABELS[e.action] || e.action, ACTION_BADGE[e.action] || ""),
      },
      { label: "Round", render: (e) => (e.round_id != null ? `#${e.round_id}` : "—") },
      {
        label: "Actor",
        className: "user-cell",
        render: (e) => e.actor_name || e.actor_id,
      },
      { label: "Details", className: "reason-cell", render: (e) => fmtDetails(e.details) },
      tsColumn("ts"),
    ],
    fetch: async (params) => {
      if (!memberNames) {
        try {
          const members = await api("/api/meta/members");
          memberNames = new Map(members.map((m) => [m.id, m.display_name || m.name]));
        } catch (_) {
          memberNames = new Map(); // fall back to raw IDs
        }
      }
      const data = await api("/api/guess/audit", params);
      const rows = data.events.map((e) => ({
        ...e,
        actor_name: memberNames.get(e.actor_id) || null,
      }));
      return { rows }; // endpoint returns no total
    },
  });
}
