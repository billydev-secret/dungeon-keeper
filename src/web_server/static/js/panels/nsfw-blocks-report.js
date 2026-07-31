import { api } from "../api.js";
import { auditPanel, badge, tsColumn } from "../audit-helpers.js";

const SURFACE_LABELS = {
  sfw: "SFW channel",
  spoiler: "Spoiler channel",
};

const ACTION_LABELS = {
  removed: "Removed",
  logged: "Log only",
};

const ACTION_BADGE = {
  removed: "badge-danger",
  logged: "badge-warning",
};

// An unreadable image is not a low score — the bot deleted it without ever
// seeing it, which is a different thing to review and is called out as such.
function fmtScore(score) {
  if (score == null) return "unreadable";
  return score.toFixed(2);
}

export function mount(container) {
  return auditPanel(container, {
    title: "Blocked Images",
    subtitle:
      "Every image the bot removed for being explicit, and how sure it was",
    empty:
      "No images have been blocked. Removals appear here as they happen, from both spoiler channels and SFW-channel prevention.",
    filters: [
      {
        name: "surface",
        label: "Gate",
        options: [
          { value: "", label: "All gates" },
          ...Object.entries(SURFACE_LABELS).map(([value, label]) => ({
            value,
            label,
          })),
        ],
      },
      {
        name: "days",
        label: "Window",
        options: [
          { value: "30", label: "30 days" },
          { value: "7", label: "7 days" },
          { value: "90", label: "90 days" },
          { value: "365", label: "1 year" },
        ],
      },
    ],
    columns: [
      {
        label: "Action",
        render: (e) =>
          badge(ACTION_LABELS[e.action] || e.action, ACTION_BADGE[e.action] || ""),
      },
      {
        label: "Member",
        className: "user-cell",
        render: (e) => e.author_name || e.author_id,
      },
      {
        label: "Channel",
        render: (e) => (e.channel_name ? `#${e.channel_name}` : e.channel_id),
      },
      { label: "File", className: "reason-cell", render: (e) => e.filename },
      { label: "Score", render: (e) => fmtScore(e.score) },
      {
        label: "Gate",
        render: (e) => SURFACE_LABELS[e.surface] || e.surface,
      },
      tsColumn("created_at"),
    ],
    fetch: async (params) => {
      const data = await api("/api/moderation/nsfw-blocks", params);
      return { rows: data.entries, total: data.total };
    },
  });
}
