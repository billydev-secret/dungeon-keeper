import { api } from "../api.js";
import { auditPanel, badge, tsColumn } from "../audit-helpers.js";

const STATE_LABELS = {
  pending:  "Pending",
  expired:  "Expired",
  rejected: "Rejected",
  accepted: "Accepted",
};

const STATE_BADGE = {
  pending:  "badge-info",
  expired:  "badge-dim",
  rejected: "badge-danger",
  accepted: "badge-success",
};

function yesNo(val, yesCls, noCls) {
  return badge(val ? "Yes" : "No", val ? yesCls : noCls);
}

export function mount(container) {
  return auditPanel(container, {
    title: "Whisper Audit Log",
    subtitle: "Whisper send history — shows each whisper's current status, not every past change",
    empty: "No whispers found.",
    filters: [
      {
        name: "state",
        label: "State",
        options: [
          { value: "", label: "All" },
          ...Object.entries(STATE_LABELS).map(([value, label]) => ({ value, label })),
        ],
      },
      { name: "reported_only", label: "Reported only", type: "checkbox" },
    ],
    columns: [
      { label: "#", render: (e) => String(e.id) },
      { label: "Sender", render: (e) => e.sender_name || e.sender_id },
      { label: "Target", render: (e) => e.target_name || e.target_id },
      {
        label: "State",
        render: (e) => badge(STATE_LABELS[e.state] || e.state, STATE_BADGE[e.state] || ""),
      },
      { label: "Solved", render: (e) => yesNo(e.solved, "badge-success", "badge-dim") },
      { label: "Exposed", render: (e) => yesNo(e.exposed, "badge-warning", "badge-dim") },
      {
        label: "Reports",
        render: (e) => (e.report_count > 0 ? badge(String(e.report_count), "badge-danger") : "0"),
      },
      tsColumn("created_at"),
    ],
    fetch: async (params) => {
      const data = await api("/api/moderation/whisper-audit", params);
      return { rows: data.entries, total: data.total };
    },
  });
}
