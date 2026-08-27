import { api, apiPut } from "../api.js";
import { confirmDialog } from "../ui.js";
import { auditPanel, badge, el, jumpAnchor, tsColumn } from "../audit-helpers.js";

const RETENTION_OPTIONS = [
  { value: "30", label: "30 days" },
  { value: "90", label: "90 days" },
  { value: "180", label: "180 days" },
  { value: "365", label: "1 year" },
  { value: "0", label: "Keep forever" },
];

// Labels come from the server's canonical GAME_NAMES, so this panel names each
// game the same way the rest of the dashboard does. Filled in on first load and
// then left alone; re-running would clobber the mod's current selection.
function fillFeatureOptions(sel, features) {
  if (!sel || !features || sel.dataset.filled) return;
  sel.dataset.filled = "1";
  for (const f of features) {
    const o = el("option", null, f.label);
    o.value = f.value;
    sel.append(o);
  }
}

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
    .then((d) => { sel.value = String(d.retention_days); current = d.retention_days; })
    .catch((err) => { status.textContent = err.message; });

  // The value the control held before this change, so a shortening can be
  // recognised and rolled back if the admin declines.
  let current = null;

  sel.addEventListener("change", async () => {
    const next = Number(sel.value);
    // 0 disables purging entirely, so picking it is always a lengthening.
    // Everything else is a shortening if the window shrinks, or if it was
    // previously "keep forever".
    const shortening = next !== 0 && current !== null
      && (current === 0 || next < current);
    if (shortening) {
      const ok = await confirmDialog(
        "The next scheduled cleanup will permanently delete every entry older "
        + "than that. This cannot be undone.",
        {
          title: `Shorten the anonymous report log to ${next} days?`,
          confirmLabel: "Shorten",
          danger: true,
        },
      );
      if (!ok) { sel.value = String(current); return; }
    }
    status.textContent = "Saving…";
    try {
      const d = await apiPut("/api/moderation/anon-audit/retention", {
        retention_days: Number(sel.value),
      });
      sel.value = String(d.retention_days);
      current = d.retention_days;
      status.textContent = d.retention_days === 0 ? "Purging disabled" : "Saved";
    } catch (err) {
      status.textContent = err.message;
    }
  });

  return wrap;
}

export function mount(container) {
  // Stays synchronous: app.js does not await mount(), so returning a promise
  // would leave it holding a Promise instead of the panel handle and skip
  // unmount on navigation. The feature dropdown is filled in below once the
  // first response lands.
  //
  // Declared up front rather than after auditPanel(): that call kicks off the
  // first fetch synchronously, and the fetch callback closes over this.
  let featureSel = null;

  const handle = auditPanel(container, {
    title: "Anonymous Features Audit",
    subtitle:
      "Who is behind each anonymous submission across the games suite — and who moderated it",
    empty:
      "Nothing recorded yet. Anonymous submissions in AMA, Free For All, Hot Takes, Fantasies, Clapback, Would You Rather and Spin the Compliment land here.",
    filters: [
      { name: "feature", label: "Feature", options: [{ value: "", label: "All features" }] },
    ],
    columns: [
      {
        label: "Feature",
        render: (e) => badge(e.feature_label || e.feature, "badge-info"),
      },
      {
        label: "Event",
        // is_mod_action is derived server-side from the service's MOD_EVENTS,
        // so renaming an event can't silently stop flagging it here.
        render: (e) => (e.is_mod_action
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
        render: (e) => e.content || "—",
        title: (e) => e.content || "No stored message content for this entry",
      },
      {
        label: "Details",
        className: "reason-cell",
        render: (e) => extraSummary(e.extra),
      },
      {
        label: "Message",
        render: (e) => (e.message_id ? jumpAnchor(e.channel_id, e.message_id, "jump") : "—"),
      },
      tsColumn("created_at"),
    ],
    fetch: async (params) => {
      const data = await api("/api/moderation/anon-audit", params);
      fillFeatureOptions(featureSel, data.features);
      return { rows: data.entries, total: data.total };
    },
  });

  // Resolved before the retention control is appended, so it is unambiguously
  // the feature filter rather than a positional guess among several selects.
  const controls = container.querySelector(".controls");
  featureSel = controls ? controls.querySelector("select") : null;
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
