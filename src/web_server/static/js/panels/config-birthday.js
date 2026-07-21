import { loadConfig, loadChannels, apiPut, showStatus, buildField } from "../config-helpers.js";

const DEFAULT_MESSAGE = "Happy birthday, {mention}! 🎂\n{request}";
const SAMPLE_REQUEST = "Ping me with cake reactions!";

function buildSelect(name, channels, selectedId) {
  const sel = document.createElement("select");
  sel.name = name;
  const none = document.createElement("option");
  none.value = "0";
  none.textContent = "(disabled)";
  sel.appendChild(none);
  for (const ch of channels) {
    const opt = document.createElement("option");
    opt.value = ch.id;
    opt.textContent = "#" + ch.name;
    if (ch.id === selectedId) opt.selected = true;
    sel.appendChild(opt);
  }
  return sel;
}

function buildTextarea(name, value) {
  const ta = document.createElement("textarea");
  ta.name = name;
  ta.rows = 3;
  ta.value = value;
  ta.style.cssText = "width:100%; resize:vertical; font-family:inherit;";
  return ta;
}

function buildCheckbox(name, checked) {
  const wrap = document.createElement("label");
  wrap.style.cssText = "display:flex; align-items:center; gap:8px; cursor:pointer;";
  const box = document.createElement("input");
  box.type = "checkbox";
  box.name = name;
  box.checked = !!checked;
  const txt = document.createElement("span");
  txt.textContent = "Pin the announcement in this channel";
  wrap.appendChild(box);
  wrap.appendChild(txt);
  return { wrap, box };
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

function renderPreview(previewEl, template, username) {
  previewEl.textContent = String(template || "")
    .replace(/\{mention\}/g, "@" + (username || "user"))
    .replace(/\{name\}/g, username || "user")
    .replace(/\{request\}/g, SAMPLE_REQUEST)
    .split("\n")
    .map((ln) => ln.replace(/\s+$/, ""))
    .filter((ln) => ln !== "")
    .join("\n")
    .trim();
}

// Build one channel block (dropdown + message + pin + live preview) and return
// the field elements so the caller can read them on submit.
function buildChannelBlock(form, { title, chanName, msgName, pinName }, channels, cfg, sampleName) {
  const heading = document.createElement("h3");
  heading.textContent = title;
  heading.style.cssText = "margin:1.25rem 0 0.5rem; font-size:15px;";
  form.appendChild(heading);

  const chanSel = buildSelect(chanName, channels, cfg.channelId || "0");
  form.appendChild(
    buildField("Channel", chanSel, "Set to (disabled) to skip this channel."),
  );

  const ta = buildTextarea(msgName, cfg.message || DEFAULT_MESSAGE);
  form.appendChild(
    buildField(
      "Message Template",
      ta,
      "Variables: {mention} pings the member · {name} their display name · {request} their special request (blank if none).",
    ),
  );

  const previewWrap = document.createElement("div");
  previewWrap.className = "field";
  const previewLbl = document.createElement("label");
  previewLbl.textContent = "Preview";
  const preview = document.createElement("div");
  preview.style.cssText =
    "padding:10px 12px; background:var(--bg-input); border: 1px solid var(--rule); border-radius: var(--r-sm); font-size:14px; white-space:pre-wrap; color:var(--ink);";
  renderPreview(preview, ta.value, sampleName);
  previewWrap.appendChild(previewLbl);
  previewWrap.appendChild(preview);
  form.appendChild(previewWrap);
  ta.addEventListener("input", () => renderPreview(preview, ta.value, sampleName));

  const { wrap: pinWrap, box: pinBox } = buildCheckbox(pinName, cfg.pin);
  const pinField = document.createElement("div");
  pinField.className = "field";
  pinField.appendChild(pinWrap);
  form.appendChild(pinField);

  return { chanSel, ta, pinBox };
}

export function mount(container) {
  clearChildren(container);
  appendLoading(container);

  (async () => {
    const [config, channels] = await Promise.all([loadConfig(), loadChannels()]);
    const b = config.birthday || {};
    const me = window.__dk_user || {};
    const sampleName = me.username || "user";

    clearChildren(container);
    const panel = document.createElement("div");
    panel.className = "panel";
    container.appendChild(panel);

    const header = document.createElement("header");
    const h2 = document.createElement("h2");
    h2.textContent = "Birthdays";
    const sub = document.createElement("div");
    sub.className = "subtitle";
    sub.textContent = "Daily birthday announcements (members set their own date with /birthday set)";
    header.appendChild(h2);
    header.appendChild(sub);
    panel.appendChild(header);

    const note = document.createElement("p");
    note.style.cssText = "color:var(--ink-dim); margin-bottom:1rem; font-size:13px;";
    note.textContent =
      "The bot announces birthdays once per day (00:00 UTC). You can post to two channels, each with its own message; a pinned message is automatically unpinned on the next day's pass.";
    panel.appendChild(note);

    const form = document.createElement("form");
    form.className = "form";
    panel.appendChild(form);

    const primary = buildChannelBlock(
      form,
      {
        title: "Primary Channel",
        chanName: "birthday_channel_id",
        msgName: "birthday_message",
        pinName: "birthday_pin",
      },
      channels,
      { channelId: b.birthday_channel_id || "0", message: b.birthday_message, pin: b.birthday_pin },
      sampleName,
    );

    const secondary = buildChannelBlock(
      form,
      {
        title: "Second Channel (optional)",
        chanName: "birthday_channel_id_2",
        msgName: "birthday_message_2",
        pinName: "birthday_pin_2",
      },
      channels,
      { channelId: b.birthday_channel_id_2 || "0", message: b.birthday_message_2, pin: b.birthday_pin_2 },
      sampleName,
    );

    const saveRow = document.createElement("div");
    saveRow.style.cssText = "margin-top:1rem;";
    const saveBtn = document.createElement("button");
    saveBtn.type = "submit";
    saveBtn.className = "btn btn-primary";
    saveBtn.textContent = "Save";
    const saveStatus = document.createElement("span");
    saveRow.appendChild(saveBtn);
    saveRow.appendChild(saveStatus);
    form.appendChild(saveRow);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const message = primary.ta.value.trim();
      const message2 = secondary.ta.value.trim();
      if (!message || !message2) {
        showStatus(saveStatus, false, "Message cannot be empty");
        return;
      }
      try {
        await apiPut("/api/config/birthday", {
          birthday_channel_id: primary.chanSel.value,
          birthday_message: message,
          birthday_pin: primary.pinBox.checked,
          birthday_channel_id_2: secondary.chanSel.value,
          birthday_message_2: message2,
          birthday_pin_2: secondary.pinBox.checked,
        });
        showStatus(saveStatus, true);
      } catch (err) {
        showStatus(saveStatus, false, err.message);
      }
    });
  })();
}
