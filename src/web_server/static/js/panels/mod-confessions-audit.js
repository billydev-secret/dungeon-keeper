import { api } from "../api.js";
import { auditPanel, badge, jumpAnchor, tsColumn } from "../audit-helpers.js";

// Rows come from `anon_audit_log`, not the seven-day `confession_threads`
// table, so this panel is the durable moderation record rather than a rolling
// week. Its retention window is the guild-wide anon-audit one, changed on the
// Anonymous Features panel — deliberately not duplicated here, because two
// controls writing one value is how they end up disagreeing on screen.
export function mount(container) {
  return auditPanel(container, {
    title: "Confessions Audit Log",
    subtitle: "Every confession and anonymous reply, with the real author behind it",
    empty: "No confessions have been submitted yet. When members post one, this log ties it back to the real author.",
    filters: [],
    columns: [
      {
        label: "Kind",
        render: (e) => (e.kind === "reply"
          ? badge("Reply", "badge-dim")
          : badge("Confession", "badge-info")),
      },
      { label: "Author", render: (e) => e.author_name || e.author_id },
      {
        label: "Content",
        className: "reason-cell",
        // Not sliced: the cell clamps to three lines and expands in place, so
        // a 120-char cut here only limited what "More" could ever reveal — the
        // CSS was already cutting it at roughly 37 characters on screen.
        render: (e) => e.content || "—",
        title: (e) => e.content || "—",
      },
      {
        label: "Message",
        render: (e) => jumpAnchor(e.channel_id, e.message_id, `#${e.message_id || "—"}`),
      },
      {
        label: "In reply to",
        render: (e) => {
          if (e.kind !== "reply") return "—";
          // Name the member replied to when we still have them; otherwise
          // point at the confession the reply hangs off.
          if (e.replied_to_name || e.replied_to_id) {
            return e.replied_to_name || e.replied_to_id;
          }
          // Link the thread, not the root message. A reply's channel_id is the
          // thread it was posted into, while the root confession lives in the
          // parent channel — addressing one through the other resolves to
          // nothing. A thread spawned from a message shares that message's id,
          // so root_message_id *is* the thread id on both destination shapes.
          return jumpAnchor(e.root_message_id, null, "confession");
        },
      },
      tsColumn("created_at"),
    ],
    fetch: async (params) => {
      const data = await api("/api/moderation/confessions-audit", params);
      return { rows: data.entries, total: data.total };
    },
  });
}
