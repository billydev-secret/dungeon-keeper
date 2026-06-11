import { api } from "../api.js";
import { auditPanel, badge, tsColumn } from "../audit-helpers.js";

const ACTION_LABELS = {
  request_asked:        "Request Asked",
  request_accepted:     "Request Accepted",
  request_denied:       "Request Denied",
  request_expired:      "Request Expired",
  relationship_revoked: "Revoked",
};

const ACTION_BADGE = {
  request_asked:        "badge-info",
  request_accepted:     "badge-success",
  request_denied:       "badge-danger",
  request_expired:      "badge-dim",
  relationship_revoked: "badge-warning",
};

const TYPE_LABELS = { dm: "DM", friend: "Friend Request" };
const TYPE_BADGE  = { dm: "badge-info", friend: "badge-warning" };

function parseType(notes) {
  if (!notes) return null;
  const m = notes.match(/^type=(\w+)/);
  return m ? m[1] : null;
}

function restNotes(e) {
  const reqType = parseType(e.notes);
  return reqType
    ? (e.notes.replace(/^type=\w+[,;]?\s*/, "") || "—")
    : (e.notes || "—");
}

export function mount(container) {
  return auditPanel(container, {
    title: "DM Audit Log",
    subtitle: "DM permission request and relationship history",
    empty: "No DM audit entries found.",
    filters: [
      {
        name: "action",
        label: "Action",
        options: [
          { value: "", label: "All Actions" },
          ...Object.entries(ACTION_LABELS).map(([value, label]) => ({ value, label })),
        ],
      },
      {
        name: "type",
        label: "Type",
        options: [
          { value: "", label: "All Types" },
          { value: "dm", label: "DM" },
          { value: "friend", label: "Friend Request" },
        ],
      },
    ],
    columns: [
      {
        label: "Action",
        render: (e) => {
          const frag = document.createDocumentFragment();
          frag.append(badge(ACTION_LABELS[e.action] || e.action, ACTION_BADGE[e.action] || ""));
          const reqType = parseType(e.notes);
          if (reqType) {
            frag.append(
              " ",
              badge(TYPE_LABELS[reqType] || reqType, TYPE_BADGE[reqType] || "badge-dim"),
            );
          }
          return frag;
        },
      },
      { label: "Actor", render: (e) => e.actor_name || e.actor_id || "—" },
      { label: "User A", render: (e) => e.user_a_name || e.user_a_id || "—" },
      { label: "User B", render: (e) => e.user_b_name || e.user_b_id || "—" },
      {
        label: "Notes",
        className: "reason-cell",
        render: restNotes,
        title: restNotes,
      },
      tsColumn("timestamp"),
    ],
    fetch: async (params) => {
      const data = await api("/api/moderation/dm-audit", params);
      return { rows: data.entries, total: data.total };
    },
  });
}
