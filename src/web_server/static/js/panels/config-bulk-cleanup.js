import {
  loadConfig,
  loadChannels,
  apiPut,
  showStatus,
  buildField,
  mountChannelMultiPicker,
  guardForm,
  renderMetaWarning,
} from "../config-helpers.js";
import { confirmDialog } from "../ui.js";

let _fieldSeq = 0;

// buildField renders a bare <label>; tie it to its control by id so screen
// readers announce the label and a label tap focuses the field (W-A7).
function field(labelText, control, hint) {
  const div = buildField(labelText, control, hint);
  if (control instanceof HTMLElement && /^(INPUT|SELECT|TEXTAREA)$/.test(control.tagName)) {
    const id = control.id || `bc-field-${++_fieldSeq}`;
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

function appendLoading(container) {
  const panel = document.createElement("div");
  panel.className = "panel";
  const empty = document.createElement("div");
  empty.className = "empty";
  empty.textContent = "Loading bulk cleanup settings…";
  panel.appendChild(empty);
  container.appendChild(panel);
}

function fmtLastRun(ts) {
  if (!ts) return "Never";
  try {
    return new Date(ts * 1000).toLocaleString();
  } catch (_) {
    return "Unknown";
  }
}

export function mount(container) {
  clearChildren(container);
  appendLoading(container);

  (async () => {
    const [config, channels] = await Promise.all([loadConfig(), loadChannels()]);
    const c = config.bulk_cleanup || {};

    clearChildren(container);
    const panel = document.createElement("div");
    panel.className = "panel";
    container.appendChild(panel);

    const header = document.createElement("header");
    const h2 = document.createElement("h2");
    h2.textContent = "Bulk Cleanup";
    const sub = document.createElement("div");
    sub.className = "subtitle";
    sub.textContent =
      "Keep deleting old messages across the whole server, apart from the channels you protect";
    header.appendChild(h2);
    header.appendChild(sub);
    panel.appendChild(header);

    const warning = renderMetaWarning();
    if (warning) {
      const w = document.createElement("div");
      w.innerHTML = warning;
      panel.appendChild(w.firstElementChild);
    }

    // ── Warning ────────────────────────────────────────────────────────
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
    panel.appendChild(warn);

    // ── Settings form ──────────────────────────────────────────────────
    const form = document.createElement("form");
    form.className = "form form-cards";
    panel.appendChild(form);

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
    panel.appendChild(exForm);

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
  })();
}
