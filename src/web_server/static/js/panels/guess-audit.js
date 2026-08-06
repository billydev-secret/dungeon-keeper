import { api } from "../api.js";
import { memberNames } from "../config-helpers.js";
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
  // The guess audit endpoint returns raw actor snowflakes only; resolve display
  // names via the same /api/meta/members lookup other panels use.
  return auditPanel(container, {
    title: "Guess Who Audit",
    subtitle: "Recent submit, delete, solve, and guess-cap events from Guess Who",
    empty: "No Guess Who activity yet. Submissions, deletions, and solves appear here once members start a round.",
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
      const data = await api("/api/guess/audit", params);
      // Rows first, then names for exactly the actors on this page. The member
      // list is a bounded page now, and an audit trail is precisely where the
      // long-departed turn up — memberNames() looks up whoever the page missed
      // instead of leaving a raw snowflake in the Actor column. Both halves are
      // memoized in config-helpers, so paging costs no extra request.
      const lookup = await memberNames(data.events.map((e) => e.actor_id));
      const rows = data.events.map((e) => ({
        ...e,
        actor_name: lookup(e.actor_id),
      }));
      return { rows }; // endpoint returns no total
    },
  });
}
