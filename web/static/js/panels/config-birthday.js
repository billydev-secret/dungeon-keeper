import { loadConfig, loadChannels, apiPut, showStatus, buildField } from "../config-helpers.js";

const DEFAULT_MESSAGE = "Happy birthday, {mention}! 🎂";

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
  const sample = "@" + (username || "user");
  previewEl.textContent = String(template || "").replace(/\{mention\}/g, sample);
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
    note.textContent = "The bot announces birthdays once per day at or after 9 AM (server time).";
    panel.appendChild(note);

    const form = document.createElement("form");
    form.className = "form";
    panel.appendChild(form);

    const chanSel = buildSelect("birthday_channel_id", channels, b.birthday_channel_id || "0");
    form.appendChild(
      buildField(
        "Announcement Channel",
        chanSel,
        "Set to (disabled) to turn off announcements.",
      ),
    );

    const messageVal = b.birthday_message || DEFAULT_MESSAGE;
    const ta = buildTextarea("birthday_message", messageVal);
    form.appendChild(
      buildField(
        "Message Template",
        ta,
        "Use {mention} as a placeholder for the user's mention.",
      ),
    );

    const previewWrap = document.createElement("div");
    previewWrap.className = "field";
    const previewLbl = document.createElement("label");
    previewLbl.textContent = "Preview";
    const preview = document.createElement("div");
    preview.style.cssText =
      "padding:10px 12px; background:var(--bg-input); border: 1px solid var(--rule); border-radius: var(--r-sm); font-size:14px; white-space:pre-wrap; color:var(--ink);";
    renderPreview(preview, messageVal, sampleName);
    previewWrap.appendChild(previewLbl);
    previewWrap.appendChild(preview);
    form.appendChild(previewWrap);

    ta.addEventListener("input", () => {
      renderPreview(preview, ta.value, sampleName);
    });

    const saveRow = document.createElement("div");
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
      const fd = new FormData(form);
      const message = String(fd.get("birthday_message") || "").trim();
      if (!message) {
        showStatus(saveStatus, false, "Message cannot be empty");
        return;
      }
      try {
        await apiPut("/api/config/birthday", {
          birthday_channel_id: fd.get("birthday_channel_id"),
          birthday_message: message,
        });
        showStatus(saveStatus, true);
      } catch (err) {
        showStatus(saveStatus, false, err.message);
      }
    });
  })();
}
