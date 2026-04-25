import { loadConfig, loadChannels, apiPut, showStatus, buildField } from "../config-helpers.js";

function buildSelect(name, channels, selectedId, allowNone) {
  const sel = document.createElement("select");
  sel.name = name;
  if (allowNone !== false) {
    const none = document.createElement("option");
    none.value = "0";
    none.textContent = "(disabled)";
    sel.appendChild(none);
  }
  for (const ch of channels) {
    const opt = document.createElement("option");
    opt.value = ch.id;
    opt.textContent = "#" + ch.name;
    if (ch.id === selectedId) opt.selected = true;
    sel.appendChild(opt);
  }
  return sel;
}

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

function buildTextInput(name, value, placeholder) {
  const inp = document.createElement("input");
  inp.type = "text";
  inp.name = name;
  inp.value = value;
  if (placeholder) inp.placeholder = placeholder;
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

export function mount(container) {
  clearChildren(container);
  appendLoading(container);

  (async () => {
    const [config, channels] = await Promise.all([loadConfig(), loadChannels()]);
    const s = config.starboard || {};
    const excludedSet = new Set(s.excluded_channels || []);

    clearChildren(container);
    const panel = document.createElement("div");
    panel.className = "panel";
    panel.style.overflowY = "auto";
    container.appendChild(panel);

    const header = document.createElement("header");
    const h2 = document.createElement("h2");
    h2.textContent = "Starboard";
    const sub = document.createElement("div");
    sub.className = "subtitle";
    sub.textContent = "Repost popular messages to a starboard channel";
    header.appendChild(h2);
    header.appendChild(sub);
    panel.appendChild(header);

    // ── Settings form ──────────────────────────────────────────────────
    const form = document.createElement("form");
    form.className = "config-form";
    panel.appendChild(form);

    form.appendChild(
      buildField(
        "Status",
        buildBoolSelect("enabled", s.enabled !== false),
        "When disabled, reactions are ignored.",
      ),
    );

    const chanSel = buildSelect("channel_id", channels, s.channel_id || "0");
    form.appendChild(
      buildField(
        "Starboard Channel",
        chanSel,
        "Channel where starred messages are reposted.",
      ),
    );

    form.appendChild(
      buildField(
        "Threshold",
        buildNumberInput("threshold", 1, s.threshold ?? 3),
        "Minimum reactions required to post (excluding self-stars).",
      ),
    );

    form.appendChild(
      buildField(
        "Emoji",
        buildTextInput("emoji", s.emoji || "⭐", "⭐"),
        "Reaction emoji that triggers the starboard.",
      ),
    );

    const saveRow = document.createElement("div");
    const saveBtn = document.createElement("button");
    saveBtn.type = "submit";
    saveBtn.textContent = "Save Settings";
    const saveStatus = document.createElement("span");
    saveRow.appendChild(saveBtn);
    saveRow.appendChild(saveStatus);
    form.appendChild(saveRow);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const threshold = parseInt(fd.get("threshold"), 10);
      const emoji = String(fd.get("emoji") || "").trim();
      if (!Number.isFinite(threshold) || threshold < 1) {
        showStatus(saveStatus, false, "Threshold must be ≥ 1");
        return;
      }
      if (!emoji) {
        showStatus(saveStatus, false, "Emoji cannot be empty");
        return;
      }
      try {
        await apiPut("/api/config/starboard", {
          channel_id: fd.get("channel_id"),
          threshold,
          emoji,
          enabled: fd.get("enabled") === "true",
        });
        showStatus(saveStatus, true);
      } catch (err) {
        showStatus(saveStatus, false, err.message);
      }
    });

    // ── Excluded channels ──────────────────────────────────────────────
    const hr = document.createElement("hr");
    hr.style.cssText = "margin:24px 0; border-color:var(--grid);";
    panel.appendChild(hr);

    const exForm = document.createElement("form");
    exForm.className = "config-form";
    panel.appendChild(exForm);

    const exHeader = document.createElement("h3");
    exHeader.style.cssText = "margin:0 0 6px; font-size:15px;";
    exHeader.textContent = "Excluded Channels";
    exForm.appendChild(exHeader);

    const exHint = document.createElement("div");
    exHint.className = "field-hint";
    exHint.style.marginBottom = "10px";
    exHint.textContent = "Reactions in these channels are ignored by the starboard.";
    exForm.appendChild(exHint);

    const exBox = document.createElement("div");
    exBox.style.cssText = "max-height:300px; overflow-y:auto; background:var(--bg-sidebar); border-radius:4px; padding:8px;";
    for (const ch of channels) {
      const lbl = document.createElement("label");
      lbl.style.cssText = "display:flex; align-items:center; gap:6px; padding:3px 4px; font-size:13px; cursor:pointer;";
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
        await apiPut("/api/config/starboard", { excluded_channels: checked });
        showStatus(exStatus, true);
      } catch (err) {
        showStatus(exStatus, false, err.message);
      }
    });
  })();
}
