import { loadConfig, loadChannels, apiPut, showStatus, buildField } from "../config-helpers.js";

function buildBoolSelect(name, value) {
  const sel = document.createElement("select");
  sel.name = name;
  const optTrue = document.createElement("option");
  optTrue.value = "true";
  optTrue.textContent = "Enabled";
  if (value) optTrue.selected = true;
  const optFalse = document.createElement("option");
  optFalse.value = "false";
  optFalse.textContent = "Disabled";
  if (!value) optFalse.selected = true;
  sel.appendChild(optTrue);
  sel.appendChild(optFalse);
  return sel;
}

function buildNumberInput(name, min, value) {
  const inp = document.createElement("input");
  inp.type = "number";
  inp.name = name;
  inp.min = String(min);
  inp.value = String(value);
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
  empty.textContent = "Loading config…";
  panel.appendChild(empty);
  container.appendChild(panel);
}

function fmtLastRun(ts) {
  if (!ts) return "never";
  try {
    return new Date(ts * 1000).toLocaleString();
  } catch (_) {
    return "unknown";
  }
}

export function mount(container) {
  clearChildren(container);
  appendLoading(container);

  (async () => {
    const [config, channels] = await Promise.all([loadConfig(), loadChannels()]);
    const c = config.bulk_cleanup || {};
    const excludedSet = new Set(c.excluded_channels || []);

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
      "Continuously delete old messages server-wide, with channel exceptions.";
    header.appendChild(h2);
    header.appendChild(sub);
    panel.appendChild(header);

    // ── Warning ────────────────────────────────────────────────────────
    const warn = document.createElement("div");
    warn.className = "field-hint";
    warn.style.cssText =
      "border:1px solid var(--red,#c00);border-radius:6px;padding:10px;margin-bottom:14px;line-height:1.5;";
    warn.innerHTML =
      "<strong>This permanently deletes messages and cannot be undone.</strong> " +
      "Once enabled, a background task deletes every message older than the age " +
      "below across all text channels and threads (pinned messages and excluded " +
      "channels are kept). Discord requires deleting old messages one at a time " +
      "(~1/sec), so the first pass on a busy server can take hours. It re-runs " +
      "about once a day.";
    panel.appendChild(warn);

    // ── Settings form ──────────────────────────────────────────────────
    const form = document.createElement("form");
    form.className = "form";
    panel.appendChild(form);

    form.appendChild(
      buildField(
        "Status",
        buildBoolSelect("enabled", c.enabled === true),
        "When disabled, nothing is deleted.",
      ),
    );

    form.appendChild(
      buildField(
        "Delete messages older than (days)",
        buildNumberInput("age_days", 1, c.age_days ?? 30),
        "Messages older than this are removed. Minimum 1 day; default 30.",
      ),
    );

    const lastRun = document.createElement("div");
    lastRun.className = "field-hint";
    lastRun.textContent = "Last completed sweep: " + fmtLastRun(c.last_run_ts);
    form.appendChild(lastRun);

    const saveRow = document.createElement("div");
    saveRow.style.marginTop = "10px";
    const saveBtn = document.createElement("button");
    saveBtn.type = "submit";
    saveBtn.className = "btn btn-primary";
    saveBtn.textContent = "Save Settings";
    const saveStatus = document.createElement("span");
    saveRow.appendChild(saveBtn);
    saveRow.appendChild(saveStatus);
    form.appendChild(saveRow);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const ageDays = parseInt(fd.get("age_days"), 10);
      if (!Number.isFinite(ageDays) || ageDays < 1) {
        showStatus(saveStatus, false, "Age must be ≥ 1 day");
        return;
      }
      try {
        await apiPut("/api/config/bulk-cleanup", {
          enabled: fd.get("enabled") === "true",
          age_days: ageDays,
        });
        showStatus(saveStatus, true);
      } catch (err) {
        showStatus(saveStatus, false, err.message);
      }
    });

    // ── Excluded channels ──────────────────────────────────────────────
    const exHeader = document.createElement("div");
    exHeader.className = "section-label";
    exHeader.textContent = "Excluded Channels";
    panel.appendChild(exHeader);

    const exForm = document.createElement("form");
    exForm.className = "form";
    panel.appendChild(exForm);

    const exHint = document.createElement("div");
    exHint.className = "field-hint";
    exHint.style.marginBottom = "10px";
    exHint.textContent =
      "Messages in these channels (and their threads) are never deleted.";
    exForm.appendChild(exHint);

    const exBox = document.createElement("div");
    exBox.className = "checkbox-list";
    for (const ch of channels) {
      const lbl = document.createElement("label");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.name = "excluded";
      cb.value = ch.id;
      if (excludedSet.has(ch.id)) cb.checked = true;
      const txt = document.createTextNode(" #" + ch.name);
      lbl.appendChild(cb);
      lbl.appendChild(txt);
      exBox.appendChild(lbl);
    }
    exForm.appendChild(exBox);

    const exRow = document.createElement("div");
    exRow.style.marginTop = "10px";
    const exBtn = document.createElement("button");
    exBtn.type = "submit";
    exBtn.className = "btn btn-primary";
    exBtn.textContent = "Save Excluded Channels";
    const exStatus = document.createElement("span");
    exRow.appendChild(exBtn);
    exRow.appendChild(exStatus);
    exForm.appendChild(exRow);

    exForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const checked = [...exForm.querySelectorAll('input[name="excluded"]:checked')].map(
        (el) => el.value,
      );
      try {
        await apiPut("/api/config/bulk-cleanup", { excluded_channels: checked });
        showStatus(exStatus, true);
      } catch (err) {
        showStatus(exStatus, false, err.message);
      }
    });
  })();
}
