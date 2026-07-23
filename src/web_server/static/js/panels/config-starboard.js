import {
  loadConfig,
  loadChannels,
  apiPut,
  showStatus,
  buildField,
  mountChannelPicker,
  mountChannelMultiPicker,
  guardForm,
  renderMetaWarning,
} from "../config-helpers.js";

let _fieldSeq = 0;

// buildField renders a bare <label>; tie it to its control by id so screen
// readers announce the label and a label tap focuses the field (W-A7).
function field(labelText, control, hint) {
  const div = buildField(labelText, control, hint);
  if (control instanceof HTMLElement && /^(INPUT|SELECT|TEXTAREA)$/.test(control.tagName)) {
    const id = control.id || `sb-field-${++_fieldSeq}`;
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

function buildTextInput(name, value, placeholder) {
  const inp = document.createElement("input");
  inp.type = "text";
  inp.name = name;
  inp.required = true;
  inp.value = value;
  inp.style.maxWidth = "140px";
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
  empty.textContent = "Loading starboard settings…";
  panel.appendChild(empty);
  container.appendChild(panel);
}

export function mount(container) {
  clearChildren(container);
  appendLoading(container);

  (async () => {
    const [config, channels] = await Promise.all([loadConfig(), loadChannels()]);
    const s = config.starboard || {};

    clearChildren(container);
    const panel = document.createElement("div");
    panel.className = "panel";
    container.appendChild(panel);

    const header = document.createElement("header");
    const h2 = document.createElement("h2");
    h2.textContent = "Starboard";
    const sub = document.createElement("div");
    sub.className = "subtitle";
    sub.textContent = "Copy messages your members react to the most into a hall-of-fame channel";
    header.appendChild(h2);
    header.appendChild(sub);
    panel.appendChild(header);

    const warning = renderMetaWarning();
    if (warning) {
      const w = document.createElement("div");
      w.innerHTML = warning;
      panel.appendChild(w.firstElementChild);
    }

    // Master-toggle banner (W-C6): say plainly that nothing below applies.
    const offBanner = document.createElement("div");
    offBanner.className = "field-hint";
    offBanner.setAttribute("role", "status");
    offBanner.style.cssText =
      "border:1px solid var(--rule); border-radius:6px; padding:10px; margin-bottom:14px; line-height:1.5;";
    offBanner.textContent =
      "Starboard is currently off — nothing below takes effect until you check "
      + "\"Run the Starboard\" and save.";
    panel.appendChild(offBanner);

    // ── Settings form ──────────────────────────────────────────────────
    const form = document.createElement("form");
    form.className = "form form-cards";
    panel.appendChild(form);

    const settingsCard = document.createElement("div");
    settingsCard.className = "card";
    const settingsLabel = document.createElement("div");
    settingsLabel.className = "section-label";
    settingsLabel.textContent = "Starboard Settings";
    settingsCard.appendChild(settingsLabel);
    form.appendChild(settingsCard);

    const enabled = toggleField(
      "enabled",
      "Run the Starboard",
      s.enabled !== false,
      "When unchecked, reactions are ignored and nothing new is reposted. Messages already on the starboard stay there.",
    );
    settingsCard.appendChild(enabled.wrap);

    function syncBanner() {
      offBanner.style.display = enabled.box.checked ? "none" : "";
    }
    enabled.box.addEventListener("change", syncBanner);
    syncBanner();

    const chanSlot = document.createElement("span");
    settingsCard.appendChild(field(
      "Starboard Channel",
      chanSlot,
      "Where popular messages are reposted. Choose \"(disabled)\" and nothing is reposted anywhere.",
    ));
    // Snowflakes stay strings; "0" is the saved value meaning "no channel".
    const chanPicker = mountChannelPicker(
      chanSlot, channels, String(s.channel_id || "0"),
      { emptyValue: "0", emptyLabel: "(disabled)", label: "Starboard Channel" },
    );

    settingsCard.appendChild(field(
      "Reactions Needed",
      buildNumberInput("threshold", 1, s.threshold ?? 3),
      "How many people must react before a message is reposted. The author's own reaction doesn't count.",
    ));

    settingsCard.appendChild(field(
      "Reaction Emoji",
      buildTextInput("emoji", s.emoji || "⭐", "⭐"),
      "The emoji members react with to nominate a message. Any other emoji is ignored.",
    ));

    const saveRow = document.createElement("div");
    saveRow.style.cssText = "display:flex; gap:8px; align-items:center;";
    const saveBtn = document.createElement("button");
    saveBtn.type = "submit";
    saveBtn.className = "btn btn-primary";
    saveBtn.textContent = "Save Settings";
    const saveStatus = document.createElement("span");
    saveRow.appendChild(saveBtn);
    saveRow.appendChild(saveStatus);
    form.appendChild(saveRow);

    guardForm(form);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const rawThreshold = String(fd.get("threshold") ?? "").trim();
      const threshold = parseInt(rawThreshold, 10);
      const emoji = String(fd.get("emoji") || "").trim();
      if (rawThreshold === "" || !Number.isFinite(threshold) || threshold < 1) {
        showStatus(saveStatus, false, "Reactions Needed must be a whole number of 1 or more.");
        form.querySelector('[name="threshold"]').focus();
        return;
      }
      if (!emoji) {
        showStatus(saveStatus, false, "Reaction Emoji cannot be empty.");
        form.querySelector('[name="emoji"]').focus();
        return;
      }
      try {
        await apiPut("/api/config/starboard", {
          channel_id: chanPicker.getValue() || "0",
          threshold,
          emoji,
          enabled: enabled.box.checked,
        });
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
    exLabel.textContent = "Channels the Starboard Ignores";
    exCard.appendChild(exLabel);
    exForm.appendChild(exCard);

    const exSlot = document.createElement("span");
    exCard.appendChild(field(
      "Ignored Channels",
      exSlot,
      "Reactions in these channels never put a message on the starboard — useful for private or spoiler rooms. Type to search, then click a channel to add it.",
    ));
    const exPicker = mountChannelMultiPicker(
      exSlot, channels, s.excluded_channels || [],
      { label: "Ignored Channels" },
    );

    const exRow = document.createElement("div");
    exRow.style.cssText = "display:flex; gap:8px; align-items:center;";
    const exBtn = document.createElement("button");
    exBtn.type = "submit";
    exBtn.className = "btn btn-primary";
    exBtn.textContent = "Save Ignored Channels";
    const exStatus = document.createElement("span");
    exRow.appendChild(exBtn);
    exRow.appendChild(exStatus);
    exForm.appendChild(exRow);

    guardForm(exForm);

    exForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        // Same payload as before: a list of snowflake id strings.
        await apiPut("/api/config/starboard", { excluded_channels: exPicker.getValues() });
        showStatus(exStatus, true);
      } catch (err) {
        showStatus(exStatus, false, err.message);
      }
    });
  })();
}
