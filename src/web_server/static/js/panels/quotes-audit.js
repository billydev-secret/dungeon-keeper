import { api } from "../api.js";
import { auditPanel, badge, tsColumn } from "../audit-helpers.js";

const THEME_LABELS = {
  golden_meadow: "Golden Meadow",
  midnight:      "Midnight",
  rose:          "Rose",
};

const THEME_BADGE = {
  golden_meadow: "badge-warning",
  midnight:      "badge-info",
  rose:          "badge-danger",
};

export function mount(container) {
  return auditPanel(container, {
    title: "Quotes Audit Log",
    subtitle: "All quote cards posted to channels",
    empty: "No quotes posted yet.",
    filters: [
      {
        name: "theme",
        label: "Theme",
        options: [
          { value: "", label: "All themes" },
          ...Object.entries(THEME_LABELS).map(([value, label]) => ({ value, label })),
        ],
      },
    ],
    columns: [
      { label: "#", render: (e) => String(e.id) },
      { label: "Quoter", render: (e) => e.quoter_name || e.quoter_id },
      { label: "Quoted", render: (e) => e.quoted_user_name || e.quoted_user_id },
      {
        label: "Theme",
        render: (e) => badge(THEME_LABELS[e.theme] || e.theme, THEME_BADGE[e.theme] || ""),
      },
      { label: "Font", render: (e) => e.font },
      tsColumn("ts"),
    ],
    fetch: async (params) => {
      const data = await api("/api/quotes/audit", params);
      return { rows: data.entries, total: data.total };
    },
  });
}
