import {
  loadConfig,
  loadChannels,
  apiPut,
  showStatus,
  field,
  mountChannelPicker,
  guardForm,
  lockUnlessAdmin,
  renderMetaWarning,
  mountAsync,
} from "../config-helpers.js";

const DEFAULT_MESSAGE = "Happy birthday, {mention}! 🎂\n{request}";
const SAMPLE_REQUEST = "Ping me with cake reactions!";

function buildTextarea(name, value) {
  const ta = document.createElement("textarea");
  ta.name = name;
  ta.rows = 3;
  ta.required = true;
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
  txt.textContent = "Pin the Announcement in This Channel";
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
  empty.textContent = "Loading birthday settings…";
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

// Build one channel card (picker + message + pin + live preview) and return the
// handles so the caller can read them on submit.
function buildChannelBlock(form, { title, chanName, msgName, pinName, chanHint }, channels, cfg, sampleName) {
  const card = document.createElement("div");
  card.className = "card";
  form.appendChild(card);

  const heading = document.createElement("div");
  heading.className = "section-label";
  heading.textContent = title;
  card.appendChild(heading);

  const chanSlot = document.createElement("span");
  card.appendChild(field("Announcement Channel", chanSlot, chanHint));
  // Snowflakes stay strings; "0" is the saved value meaning "don't post here".
  const chanPicker = mountChannelPicker(
    chanSlot, channels, String(cfg.channelId || "0"),
    { emptyValue: "0", emptyLabel: "(disabled)", label: `${title} — announcement channel` },
  );

  const ta = buildTextarea(msgName, cfg.message || DEFAULT_MESSAGE);
  card.appendChild(
    field(
      "Message",
      ta,
      "Placeholders: {mention} pings the birthday member, {name} prints their "
      + "display name without pinging, and {request} inserts the note they "
      + "saved with /birthday set (a blank line if they saved none).",
    ),
  );

  const previewWrap = document.createElement("div");
  previewWrap.className = "field";
  const previewLbl = document.createElement("div");
  previewLbl.className = "field-label";
  previewLbl.textContent = "Preview";
  previewLbl.style.cssText = "font-weight:600; margin-bottom:4px;";
  const preview = document.createElement("div");
  preview.setAttribute("aria-live", "polite");
  preview.style.cssText =
    "padding:10px 12px; background:var(--bg-input); border: 1px solid var(--rule); border-radius: var(--r-sm); font-size:14px; white-space:pre-wrap; color:var(--ink);";
  renderPreview(preview, ta.value, sampleName);
  previewWrap.appendChild(previewLbl);
  previewWrap.appendChild(preview);
  const previewHint = document.createElement("div");
  previewHint.className = "field-hint";
  previewHint.textContent = "How the message reads with your own name filled in.";
  previewWrap.appendChild(previewHint);
  card.appendChild(previewWrap);
  ta.addEventListener("input", () => renderPreview(preview, ta.value, sampleName));

  const { wrap: pinWrap, box: pinBox } = buildCheckbox(pinName, cfg.pin);
  const pinField = document.createElement("div");
  pinField.className = "field";
  pinField.appendChild(pinWrap);
  const pinHint = document.createElement("div");
  pinHint.className = "field-hint";
  pinHint.textContent =
    "Pins today's announcement so nobody misses it. The bot unpins it again on tomorrow's pass.";
  pinField.appendChild(pinHint);
  card.appendChild(pinField);

  return { chanPicker, ta, pinBox, title };
}

/**
 * Birthdays — the announcement settings (Config → Members, id
 * config-birthday, adminOnly). The calendar lives on Reports → Member Lists
 * → Birthday Calendar, cross-linked via `related:`. lockUnlessAdmin stays as
 * defense in depth; writes are refused server-side regardless.
 */
export function mount(outer) {
  outer.innerHTML = `
    <div class="panel">
      <header>
        <h2>Birthdays</h2>
        <div class="subtitle">How the bot announces birthdays</div>
      </header>
      <section data-region="settings"></section>
    </div>
  `;
  return mountSettings(outer.querySelector('[data-region="settings"]'));
}

export function mountSettings(container) {
  clearChildren(container);
  appendLoading(container);

  return mountAsync(container, async () => {
    const [config, channels] = await Promise.all([loadConfig(), loadChannels()]);
    const b = config.birthday || {};
    const me = window.__dk_user || {};
    const sampleName = me.username || "user";

    clearChildren(container);
    const panel = document.createElement("div");
    container.appendChild(panel);

    const header = document.createElement("div");
    header.className = "section-label";
    header.textContent = "Settings";
    panel.appendChild(header);

    const sub = document.createElement("div");
    sub.className = "field-hint";
    sub.style.marginBottom = "12px";
    sub.textContent = "Daily birthday announcements — members add their own date with /birthday set";
    panel.appendChild(sub);

    const warning = renderMetaWarning();
    if (warning) {
      const w = document.createElement("div");
      w.innerHTML = warning;
      panel.appendChild(w.firstElementChild);
    }

    const note = document.createElement("p");
    note.style.cssText = "color:var(--ink-dim); margin-bottom:1rem; font-size:13px;";
    note.textContent =
      "Birthdays are announced once a day, at the hour you pick below in the server's own "
      + "time zone. You can post to two channels, each with its own wording — handy when one "
      + "is for the whole server and one is for a smaller room. See who has a birthday coming "
      + "up on the Birthday Calendar page.";
    panel.appendChild(note);

    const form = document.createElement("form");
    form.className = "form form-cards";
    panel.appendChild(form);

    const timingCard = document.createElement("div");
    timingCard.className = "card";
    form.appendChild(timingCard);
    const timingHeading = document.createElement("div");
    timingHeading.className = "section-label";
    timingHeading.textContent = "Timing";
    timingCard.appendChild(timingHeading);

    const hourSelect = document.createElement("select");
    hourSelect.name = "birthday_announce_hour";
    const savedHour = Number.isInteger(b.birthday_announce_hour) ? b.birthday_announce_hour : 9;
    for (let h = 0; h < 24; h += 1) {
      const opt = document.createElement("option");
      opt.value = String(h);
      opt.textContent = String(h).padStart(2, "0") + ":00";
      if (h === savedHour) opt.selected = true;
      hourSelect.appendChild(opt);
    }
    timingCard.appendChild(
      field(
        "Announcement Time",
        hourSelect,
        "The hour announcements go out, in the server's own time zone (the offset set "
        + "on the Global page). Defaults to 09:00.",
      ),
    );

    const primary = buildChannelBlock(
      form,
      {
        title: "Main Channel",
        chanName: "birthday_channel_id",
        msgName: "birthday_message",
        pinName: "birthday_pin",
        chanHint: "Where birthday announcements are posted. Choose \"(disabled)\" to post nothing here.",
      },
      channels,
      { channelId: b.birthday_channel_id || "0", message: b.birthday_message, pin: b.birthday_pin },
      sampleName,
    );

    const secondary = buildChannelBlock(
      form,
      {
        title: "Second Channel (Optional)",
        chanName: "birthday_channel_id_2",
        msgName: "birthday_message_2",
        pinName: "birthday_pin_2",
        chanHint: "An optional second channel that gets its own announcement. Choose \"(disabled)\" to post in one channel only.",
      },
      channels,
      { channelId: b.birthday_channel_id_2 || "0", message: b.birthday_message_2, pin: b.birthday_pin_2 },
      sampleName,
    );

    const saveRow = document.createElement("div");
    saveRow.style.cssText = "display:flex; gap:8px; align-items:center; margin-top:1rem;";
    const saveBtn = document.createElement("button");
    saveBtn.type = "submit";
    saveBtn.className = "btn btn-primary";
    saveBtn.textContent = "Save";
    const saveStatus = document.createElement("span");
    saveRow.appendChild(saveBtn);
    saveRow.appendChild(saveStatus);
    form.appendChild(saveRow);

    // Lock after the channel pickers mount — each builds its own inputs, so
    // locking earlier would leave those live.
    if (lockUnlessAdmin(container)) return;

    guardForm(form);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const message = primary.ta.value.trim();
      const message2 = secondary.ta.value.trim();
      for (const [text, block] of [[message, primary], [message2, secondary]]) {
        if (!text) {
          showStatus(saveStatus, false, `The ${block.title} message cannot be empty.`);
          block.ta.focus();
          return;
        }
      }
      try {
        await apiPut("/api/config/birthday", {
          birthday_channel_id: primary.chanPicker.getValue() || "0",
          birthday_message: message,
          birthday_pin: primary.pinBox.checked,
          birthday_channel_id_2: secondary.chanPicker.getValue() || "0",
          birthday_message_2: message2,
          birthday_pin_2: secondary.pinBox.checked,
          birthday_announce_hour: Number(hourSelect.value),
        });
        showStatus(saveStatus, true);
      } catch (err) {
        showStatus(saveStatus, false, err.message);
      }
    });
  }, { errorMsg: "Couldn’t load the birthday settings." });
}
