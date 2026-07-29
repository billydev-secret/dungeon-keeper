/**
 * Cleanup — server-wide bulk deletion and per-channel auto-delete schedules.
 *
 * Merged from the former "Bulk Cleanup" and "Auto-Delete" pages: both are
 * admin-gated, both answer "what is being deleted on this server?", and both
 * loaded the same config + channel list separately. Server-wide comes first
 * because it sets the baseline every per-channel schedule refines.
 *
 * Each section keeps the construction style it shipped with — the bulk half
 * builds DOM nodes, the schedules half renders a template — so the merge is a
 * relocation, not a rewrite.
 */
import {
  loadConfig,
  loadChannels,
  channelName,
  apiPut,
  apiDelete,
  showStatus,
  buildField,
  guardForm,
  mountChannelPicker,
  mountChannelMultiPicker,
  renderMetaWarning,
} from "../config-helpers.js";
import { toast, confirmDialog } from "../ui.js";

// ── Shared helpers ──────────────────────────────────────────────────────────

let _fieldSeq = 0;

// buildField renders a bare <label>; tie it to its control by id so screen
// readers announce the label and a label tap focuses the field (W-A7).
function field(labelText, control, hint) {
  const div = buildField(labelText, control, hint);
  if (control instanceof HTMLElement && /^(INPUT|SELECT|TEXTAREA)$/.test(control.tagName)) {
    const id = control.id || `cl-field-${++_fieldSeq}`;
    control.id = id;
    div.querySelector("label").htmlFor = id;
  }
  return div;
}

// The one toggle idiom: a checkbox row plus a hint that states what changes.
function toggleField(name, labelText, checked, hint) {
  const wrap = document.createElement("div");
  wrap.className = "field";
  const lbl = document.createElement("label");
  lbl.style.cssText = "display:flex; align-items:center; gap:8px; cursor:pointer;";
  const box = document.createElement("input");
  box.type = "checkbox";
  box.name = name;
  box.checked = !!checked;
  lbl.appendChild(box);
  lbl.appendChild(document.createTextNode(labelText));
  wrap.appendChild(lbl);
  const h = document.createElement("div");
  h.className = "field-hint";
  h.textContent = hint;
  wrap.appendChild(h);
  return { wrap, box };
}

function buildNumberInput(name, min, value) {
  const inp = document.createElement("input");
  inp.type = "number";
  inp.name = name;
  inp.required = true;
  inp.min = String(min);
  inp.step = "1";
  inp.value = String(value);
  inp.style.maxWidth = "140px";
  return inp;
}

function clearChildren(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
}

function fmtLastRun(ts) {
  if (!ts) return "Never";
  try {
    return new Date(ts * 1000).toLocaleString();
  } catch (_) {
    return "Unknown";
  }
}

function formatTs(ts) {
  if (!ts || ts <= 0) return "Never";
  return new Date(ts * 1000).toLocaleString();
}

// ── Per-channel schedule helpers ────────────────────────────────────────────

const UNITS = [
  { label: "Minutes", seconds: 60 },
  { label: "Hours", seconds: 3600 },
  { label: "Days", seconds: 86400 },
];

function bestUnit(totalSeconds) {
  if (totalSeconds % 86400 === 0) return { value: totalSeconds / 86400, unit: 86400 };
  if (totalSeconds % 3600 === 0) return { value: totalSeconds / 3600, unit: 3600 };
  return { value: Math.round(totalSeconds / 60), unit: 60 };
}

function unitOptions(selectedSeconds) {
  return UNITS.map(
    (u) => `<option value="${u.seconds}"${u.seconds === selectedSeconds ? " selected" : ""}>${u.label}</option>`
  ).join("");
}

// Read a "value + unit" pair into whole seconds, naming the field when the
// number is blank or out of range (W-C5) instead of posting NaN.
function readDuration(fd, prefix, label, statusEl, form) {
  const raw = String(fd.get(`${prefix}_value`) ?? "").trim();
  const value = parseInt(raw, 10);
  if (raw === "" || !Number.isFinite(value) || value < 1) {
    showStatus(statusEl, false, `${label} must be a whole number of 1 or more.`);
    const el = form.querySelector(`[name=${prefix}_value]`);
    if (el) el.focus();
    return null;
  }
  return value * parseInt(fd.get(`${prefix}_unit`), 10);
}

// ── Mount ───────────────────────────────────────────────────────────────────

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading cleanup settings…</div></div>`;

  (async () => {
    const [config, channels] = await Promise.all([loadConfig(), loadChannels()]);

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Cleanup</h2>
          <div class="subtitle">Delete old messages server-wide, or on a per-channel schedule</div>
        </header>
        ${renderMetaWarning()}
        <section data-region="bulk"></section>
        <section data-region="schedules" style="margin-top:32px;"></section>
      </div>`;

    renderBulk(container.querySelector('[data-region="bulk"]'), config.bulk_cleanup || {}, channels);
    renderSchedules(
      container.querySelector('[data-region="schedules"]'),
      config.auto_delete || [],
      channels,
    );
  })();
}

// ── Section 1: server-wide bulk cleanup ─────────────────────────────────────

function renderBulk(region, c, channels) {
  clearChildren(region);

  const label = document.createElement("div");
  label.className = "section-label";
  label.textContent = "Server-Wide Cleanup";
  region.appendChild(label);

  const warn = document.createElement("div");
  warn.className = "field-hint";
  warn.style.cssText =
    "border:1px solid var(--red,#c00);border-radius:6px;padding:10px;margin-bottom:14px;line-height:1.5;";
  warn.innerHTML =
    "<strong>This deletes messages permanently and cannot be undone.</strong> " +
    "Once it is running, a background task deletes every message older than the " +
    "age you set, in every text channel and thread. Pinned messages and the " +
    "channels you list below are left alone. Discord makes the bot delete older " +
    "messages one at a time — about one per second — so the first pass on a busy " +
    "server can take hours. After that it runs again about once a day.";
  region.appendChild(warn);

  const form = document.createElement("form");
  form.className = "form form-cards";
  region.appendChild(form);

  const settingsCard = document.createElement("div");
  settingsCard.className = "card";
  const settingsLabel = document.createElement("div");
  settingsLabel.className = "section-label";
  settingsLabel.textContent = "Cleanup Schedule";
  settingsCard.appendChild(settingsLabel);
  form.appendChild(settingsCard);

  const enabled = toggleField(
    "enabled",
    "Run Bulk Cleanup",
    c.enabled === true,
    "While unchecked nothing is deleted. Checking this and saving starts the first pass within a few minutes.",
  );
  settingsCard.appendChild(enabled.wrap);

  settingsCard.appendChild(
    field(
      "Delete Messages Older Than (days)",
      buildNumberInput("age_days", 1, c.age_days ?? 30),
      "Every message past this age is deleted for good on each pass. Minimum 1 day; 30 is the default.",
    ),
  );

  const lastRun = document.createElement("div");
  lastRun.className = "field-hint";
  lastRun.textContent = "Last completed sweep: " + fmtLastRun(c.last_run_ts);
  settingsCard.appendChild(lastRun);

  const saveRow = document.createElement("div");
  saveRow.style.cssText = "display:flex; gap:8px; align-items:center;";
  const saveBtn = document.createElement("button");
  saveBtn.type = "submit";
  saveBtn.className = "btn btn-primary";
  saveBtn.textContent = "Save Schedule";
  const saveStatus = document.createElement("span");
  saveRow.appendChild(saveBtn);
  saveRow.appendChild(saveStatus);
  form.appendChild(saveRow);

  guardForm(form);

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    const rawAge = String(fd.get("age_days") ?? "").trim();
    const ageDays = parseInt(rawAge, 10);
    if (rawAge === "" || !Number.isFinite(ageDays) || ageDays < 1) {
      showStatus(saveStatus, false, "Delete Messages Older Than must be a whole number of 1 day or more.");
      form.querySelector('[name="age_days"]').focus();
      return;
    }
    // Switching the sweep on is irreversible for everything it deletes.
    if (enabled.box.checked && c.enabled !== true) {
      const ok = await confirmDialog(
        `Start deleting every message older than ${ageDays} day${ageDays === 1 ? "" : "s"} across this server? `
        + "Pinned messages and your protected channels are kept. Everything else is gone for good.",
        { title: "Start Bulk Cleanup", danger: true, confirmLabel: "Start Cleanup" },
      );
      if (!ok) return;
    }
    try {
      await apiPut("/api/config/bulk-cleanup", {
        enabled: enabled.box.checked,
        age_days: ageDays,
      });
      c.enabled = enabled.box.checked;
      showStatus(saveStatus, true);
    } catch (err) {
      showStatus(saveStatus, false, err.message);
    }
  });

  // ── Excluded channels ──────────────────────────────────────────────
  const exForm = document.createElement("form");
  exForm.className = "form form-cards";
  region.appendChild(exForm);

  const exCard = document.createElement("div");
  exCard.className = "card";
  const exLabel = document.createElement("div");
  exLabel.className = "section-label";
  exLabel.textContent = "Protected Channels";
  exCard.appendChild(exLabel);
  exForm.appendChild(exCard);

  const exSlot = document.createElement("span");
  exCard.appendChild(field(
    "Channels to Leave Alone",
    exSlot,
    "Nothing in these channels — or in their threads — is ever deleted by bulk cleanup. Type to search, then click a channel to add it.",
  ));
  const exPicker = mountChannelMultiPicker(
    exSlot, channels, c.excluded_channels || [],
    { label: "Channels to Leave Alone" },
  );

  const exRow = document.createElement("div");
  exRow.style.cssText = "display:flex; gap:8px; align-items:center;";
  const exBtn = document.createElement("button");
  exBtn.type = "submit";
  exBtn.className = "btn btn-primary";
  exBtn.textContent = "Save Protected Channels";
  const exStatus = document.createElement("span");
  exRow.appendChild(exBtn);
  exRow.appendChild(exStatus);
  exForm.appendChild(exRow);

  guardForm(exForm);

  exForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      // Same payload as before: a list of snowflake id strings.
      await apiPut("/api/config/bulk-cleanup", { excluded_channels: exPicker.getValues() });
      showStatus(exStatus, true);
    } catch (err) {
      showStatus(exStatus, false, err.message);
    }
  });
}

// ── Section 2: per-channel auto-delete schedules ────────────────────────────

function renderSchedules(region, rules, channels) {
  let seq = 0;
  function ruleRow(r) {
    const age = bestUnit(r.max_age_seconds);
    const interval = bestUnit(r.interval_seconds);
    const chName = channelName(channels, r.channel_id);
    const uid = `ad-${++seq}`;
    return `
      <form class="form card" style="margin-bottom:16px;" data-channel="${r.channel_id}">
        <div class="section-label">${chName}</div>
        <div class="field-row">
          <div class="field">
            <label for="${uid}-age">Delete Messages Older Than</label>
            <div style="display:flex; gap:4px; flex-wrap:wrap;">
              <input type="number" name="age_value" id="${uid}-age" required min="1" step="1" value="${age.value}" style="max-width:90px;" />
              <select name="age_unit" aria-label="Delete age unit">${unitOptions(age.unit)}</select>
            </div>
            <div class="field-hint">Anything in this channel older than this is deleted permanently — there is no undo.</div>
          </div>
          <div class="field">
            <label for="${uid}-interval">Check This Channel Every</label>
            <div style="display:flex; gap:4px; flex-wrap:wrap;">
              <input type="number" name="interval_value" id="${uid}-interval" required min="1" step="1" value="${interval.value}" style="max-width:90px;" />
              <select name="interval_unit" aria-label="Check interval unit">${unitOptions(interval.unit)}</select>
            </div>
            <div class="field-hint">How often the cleanup sweep runs. A shorter interval deletes closer to the age limit.</div>
          </div>
        </div>
        <div class="field">
          <label style="display:flex; gap:6px; align-items:center;">
            <input type="checkbox" name="media_only"${r.media_only ? " checked" : ""} />
            Only Delete Messages With Attachments
          </label>
          <div class="field-hint">When checked, images, videos, and files are removed but plain text messages are left alone.</div>
        </div>
        <div class="field-hint">Last sweep: ${formatTs(r.last_run_ts)}</div>
        <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
          <button type="submit" class="btn btn-primary">Save</button>
          <button type="button" class="btn btn-danger" data-remove="${r.channel_id}">Remove</button>
          <span data-status></span>
        </div>
      </form>`;
  }

  region.innerHTML = `
    <div class="section-label">Per-Channel Schedules</div>
    <div class="field-hint" style="margin-bottom:14px;">
      Independent of the server-wide sweep above: each schedule cleans one channel
      on its own age and interval, whether or not bulk cleanup is running.
    </div>
    <div data-rules>${rules.length
      ? rules.map(ruleRow).join("")
      : '<div class="empty">No channels are being cleaned up yet. Add your first schedule below.</div>'}</div>

    <div class="section-label">Add a Schedule</div>
    <form class="form card" data-add-form>
      <div class="field">
        <label>Channel</label>
        <span data-picker="channel_id"></span>
        <div class="field-hint">The channel to clean up. One schedule per channel — adding a channel that already has a schedule replaces it.</div>
      </div>
      <div class="field-row">
        <div class="field">
          <label for="ad-new-age">Delete Messages Older Than</label>
          <div style="display:flex; gap:4px; flex-wrap:wrap;">
            <input type="number" name="age_value" id="ad-new-age" required min="1" step="1" value="30" style="max-width:90px;" />
            <select name="age_unit" aria-label="Delete age unit">${unitOptions(86400)}</select>
          </div>
          <div class="field-hint">Anything older than this is deleted permanently — there is no undo.</div>
        </div>
        <div class="field">
          <label for="ad-new-interval">Check This Channel Every</label>
          <div style="display:flex; gap:4px; flex-wrap:wrap;">
            <input type="number" name="interval_value" id="ad-new-interval" required min="1" step="1" value="1" style="max-width:90px;" />
            <select name="interval_unit" aria-label="Check interval unit">${unitOptions(86400)}</select>
          </div>
          <div class="field-hint">How often the cleanup sweep runs.</div>
        </div>
      </div>
      <div class="field">
        <label style="display:flex; gap:6px; align-items:center;">
          <input type="checkbox" name="media_only" />
          Only Delete Messages With Attachments
        </label>
        <div class="field-hint">When checked, images, videos, and files are removed but plain text messages are left alone.</div>
      </div>
      <div style="display:flex; gap:8px; align-items:center;">
        <button type="submit" class="btn btn-primary">Add Schedule</button>
        <span data-add-status></span>
      </div>
    </form>`;

  // Re-render just this section after an add/remove; the bulk half above keeps
  // its own state and must not be torn down.
  async function refresh() {
    const fresh = await loadConfig();
    renderSchedules(region, fresh.auto_delete || [], channels);
  }

  // Save handlers for existing rules
  for (const r of rules) {
    const form = region.querySelector(`[data-channel="${r.channel_id}"]`);
    const status = form.querySelector("[data-status]");
    guardForm(form);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const maxAge = readDuration(fd, "age", "Delete Messages Older Than", status, form);
      if (maxAge === null) return;
      const interval = readDuration(fd, "interval", "Check This Channel Every", status, form);
      if (interval === null) return;
      try {
        await apiPut(`/api/config/auto-delete/${r.channel_id}`, {
          max_age_seconds: maxAge,
          interval_seconds: interval,
          media_only: form.querySelector("[name=media_only]").checked,
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  }

  // Remove handlers
  region.querySelectorAll("[data-remove]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const name = channelName(channels, btn.dataset.remove);
      const ok = await confirmDialog(
        `Stop auto-deleting messages in ${name}? Messages already deleted cannot be recovered.`,
        { title: "Remove Schedule", danger: true, confirmLabel: "Remove" },
      );
      if (!ok) return;
      try {
        await apiDelete(`/api/config/auto-delete/${btn.dataset.remove}`);
        await refresh();
      } catch (err) {
        toast(err.message, "error");
      }
    });
  });

  // Add handler
  const addForm = region.querySelector("[data-add-form]");
  const addStatus = region.querySelector("[data-add-status]");
  const addPicker = mountChannelPicker(
    addForm.querySelector('[data-picker="channel_id"]'),
    channels,
    "0",
    { emptyLabel: "(pick a channel)", label: "Channel" },
  );
  guardForm(addForm);
  addForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(addForm);
    // Snowflakes stay strings; "0" is the unset sentinel the picker returns.
    const channelId = addPicker.getValue() || "0";
    if (channelId === "0") {
      showStatus(addStatus, false, "Pick a channel first.");
      return;
    }
    const maxAge = readDuration(fd, "age", "Delete Messages Older Than", addStatus, addForm);
    if (maxAge === null) return;
    const interval = readDuration(fd, "interval", "Check This Channel Every", addStatus, addForm);
    if (interval === null) return;
    try {
      await apiPut(`/api/config/auto-delete/${channelId}`, {
        max_age_seconds: maxAge,
        interval_seconds: interval,
        media_only: addForm.querySelector("[name=media_only]").checked,
      });
      await refresh();
    } catch (err) {
      showStatus(addStatus, false, err.message);
    }
  });
}
