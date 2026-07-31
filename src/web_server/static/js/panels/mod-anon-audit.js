import { api, apiPut } from "../api.js";
import { auditPanel, badge, el, tsColumn } from "../audit-helpers.js";

// Feature slugs mirror bot_modules/services/anon_audit_service.KNOWN_FEATURES.
// Listed statically rather than from the response so the dropdown exists before
// the first fetch resolves; the API also returns the set actually recorded.
const FEATURES = [
  { value: "", label: "All features" },
  { value: "ama", label: "AMA" },
  { value: "ffa", label: "Free For All" },
  { value: "hottakes", label: "Hot Takes" },
  { value: "fantasies", label: "Fantasies" },
  { value: "clapback", label: "Clapback" },
  { value: "wyr", label: "Would You Rather" },
  { value: "compliment", label: "Spin the Compliment" },
];

const RETENTION_OPTIONS = [
  { value: "30", label: "30 days" },
  { value: "90", label: "90 days" },
  { value: "180", label: "180 days" },
  { value: "365", label: "1 year" },
  { value: "0", label: "Keep forever" },
];

// Events that de-anonymise or moderate someone else's anonymous post, rather
// than being an anonymous post themselves. Flagged so they stand out in a list
// that is otherwise mostly routine submissions.
const MOD_EVENTS = new Set([
  "question_approved",
  "question_rejected",
  "voters_revealed",
  "hot_seat_skipped",
]);

function prettyEvent(ev) {
  return String(ev || "").replace(/_/g, " ");
}

function extraSummary(extra) {
  if (!extra || typeof extra !== "object") return "—";
  const parts = Object.entries(extra)
    .filter(([, v]) => v !== null && v !== undefined && v !== "")
    .map(([k, v]) => `${k.replace(/_/g, " ")}: ${v}`);
  return parts.length ? parts.join(", ") : "—";
}

function buildRetentionControl() {
  const sel = el("select");
  for (const opt of RETENTION_OPTIONS) {
    const o = el("option", null, opt.label);
    o.value = opt.value;
    sel.append(o);
  }
  const status = el("span", { style: "color:var(--ink-dim);font-size:12px;" }, "");
  const wrap = el("label", null, "Keep for ", sel, " ", status);

  api("/api/moderation/anon-audit/retention")
    .then((d) => { sel.value = String(d.retention_days); })
    .catch((err) => { status.textContent = err.message; });

  sel.addEventListener("change", async () => {
    status.textContent = "Saving…";
    try {
      const d = await apiPut("/api/moderation/anon-audit/retention", {
        retention_days: Number(sel.value),
      });
      sel.value = String(d.retention_days);
      status.textContent = d.retention_days === 0 ? "Purging disabled" : "Saved";
    } catch (err) {
      status.textContent = err.message;
    }
  });

  return wrap;
}

export function mount(container) {
  const handle = auditPanel(container, {
    title: "Anonymous Features Audit",
    subtitle:
      "Who is behind each anonymous submission across the games suite — and who moderated it",
    empty:
      "Nothing recorded yet. Anonymous submissions in AMA, Free For All, Hot Takes, Fantasies, Clapback, Would You Rather and Spin the Compliment land here.",
    filters: [
      { name: "feature", label: "Feature", options: FEATURES },
    ],
    columns: [
      {
        label: "Feature",
        render: (e) => badge(e.feature, "badge-info"),
      },
      {
        label: "Event",
        render: (e) => (MOD_EVENTS.has(e.event)
          ? badge(prettyEvent(e.event), "badge-warning")
          : prettyEvent(e.event)),
      },
      { label: "Actor", render: (e) => e.actor_name || e.actor_id },
      {
        label: "Subject",
        render: (e) => (e.target_id ? (e.target_name || e.target_id) : "—"),
      },
      {
        label: "Content",
        className: "reason-cell",
        // Joined from the message store, never copied into the audit table.
        // Null here means either the guild keeps no message content or the
        // event produced no message at all (see migration 145).
        render: (e) => {
          if (!e.content) return "—";
          return e.content.length > 120 ? e.content.slice(0, 120) + "…" : e.content;
        },
        title: (e) => e.content || "No stored message content for this entry",
      },
      {
        label: "Details",
        className: "reason-cell",
        render: (e) => extraSummary(e.extra),
      },
      {
        label: "Message",
        render: (e) => (e.message_id && e.channel_id
          ? el("a",
              {
                href: `https://discord.com/channels/@me/${e.channel_id}/${e.message_id}`,
                target: "_blank",
                rel: "noopener noreferrer",
              },
              "jump",
            )
          : "—"),
      },
      tsColumn("created_at"),
    ],
    fetch: async (params) => {
      const data = await api("/api/moderation/anon-audit", params);
      return { rows: data.entries, total: data.total };
    },
  });

  const controls = container.querySelector(".controls");
  if (controls) controls.append(buildRetentionControl());

  const note = el("div",
    { style: "color:var(--ink-dim);font-size:12px;margin-top:10px;" },
    "Content is read from the server's message archive rather than copied here, "
    + "so it appears only while message-content storage is enabled and is blank "
    + "for submissions that never became a message (a rejected screened AMA "
    + "question, a queued Would You Rather prompt).",
  );
  const panel = container.querySelector(".panel");
  if (panel) panel.append(note);

  return handle;
}
