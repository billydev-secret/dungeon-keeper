import { api } from "../api.js";
import { auditPanel, el, tsColumn } from "../audit-helpers.js";

export function mount(container) {
  return auditPanel(container, {
    title: "Confessions Audit Log",
    subtitle: "Every confession submitted, with the real author behind it",
    empty: "No confessions have been submitted yet. When members post one, this log ties it back to the real author.",
    filters: [],
    columns: [
      { label: "Author", render: (e) => e.author_name || e.author_id },
      {
        label: "Content",
        className: "reason-cell",
        render: (e) => {
          const text = e.content || "—";
          return text.length > 120 ? text.slice(0, 120) + "…" : text;
        },
        title: (e) => e.content || "—",
      },
      {
        label: "Message",
        render: (e) => el("a",
          {
            href: `https://discord.com/channels/@me/${e.channel_id}/${e.message_id}`,
            target: "_blank",
            rel: "noopener noreferrer",
          },
          `#${e.message_id}`,
        ),
      },
      {
        label: "Thread",
        render: (e) => (e.thread_id && e.thread_id !== "0"
          ? el("a",
              {
                href: `https://discord.com/channels/@me/${e.thread_id}`,
                target: "_blank",
                rel: "noopener noreferrer",
              },
              `#${e.thread_id}`,
            )
          : "—"),
      },
      tsColumn("created_at"),
    ],
    fetch: async (params) => {
      const data = await api("/api/moderation/confessions-audit", params);
      return { rows: data.entries, total: data.total };
    },
  });
}
