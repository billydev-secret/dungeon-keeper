import {
  loadConfig,
  loadChannels,
  apiPut,
  apiDelete,
  showStatus,
  field,
  mountChannelPicker,
  channelName,
  guardForm,
  lockUnlessAdmin,
  renderMetaWarning,
  mountAsync,
  trackCard,
  clearCardDirty,
  hasDirtySibling,
} from "../config-helpers.js";
import { toast, confirmDialog } from "../ui.js";

const DEFAULT_MESSAGE = "Happy birthday, {mention}! 🎂\n{request}";
const SAMPLE_REQUEST = "Ping me with cake reactions!";

function buildTextarea(value) {
  const ta = document.createElement("textarea");
  ta.name = "message";
  ta.rows = 3;
  ta.required = true;
  ta.value = value;
  ta.style.cssText = "width:100%; resize:vertical; font-family:inherit;";
  return ta;
}

function buildPinCheckbox(checked) {
  const wrap = document.createElement("label");
  wrap.style.cssText = "display:flex; align-items:center; gap:8px; cursor:pointer;";
  const box = document.createElement("input");
  box.type = "checkbox";
  box.name = "pin";
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

// Message textarea + live preview + pin checkbox, shared by every channel
// card (existing or the add-a-channel form) so the three stay identical.
function buildMessageAndPinFields(card, { message, pin, sampleName }) {
  const ta = buildTextarea(message);
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

  const { wrap: pinWrap, box: pinBox } = buildPinCheckbox(pin);
  const pinField = document.createElement("div");
  pinField.className = "field";
  pinField.appendChild(pinWrap);
  const pinHint = document.createElement("div");
  pinHint.className = "field-hint";
  pinHint.textContent =
    "Pins today's announcement so nobody misses it. The bot unpins it again on tomorrow's pass.";
  pinField.appendChild(pinHint);
  card.appendChild(pinField);

  return { ta, pinBox };
}

// One already-configured channel: name + message + preview + pin + Save/Remove.
// Each card is its own <form>, saved and removed independently — the pattern
// config-needle.js (Auto-Thread) uses for the same "any number of channels"
// idiom.
function buildChannelCard(list, ch, channels, sampleName) {
  const card = document.createElement("form");
  card.className = "form card";
  // The cards live inside a plain wrapper div, not directly in .panel, so the
  // adjacent-sibling CSS rule that spaces top-level cards apart doesn't reach
  // them — same reason config-needle.js's channelCard() sets this inline.
  card.style.marginBottom = "16px";
  card.dataset.birthdayChannel = ch.channel_id;
  list.appendChild(card);

  const heading = document.createElement("div");
  heading.className = "section-label";
  heading.textContent = channelName(channels, ch.channel_id);
  card.appendChild(heading);

  const { ta, pinBox } = buildMessageAndPinFields(card, {
    message: ch.message, pin: ch.pin, sampleName,
  });

  const row = document.createElement("div");
  row.style.cssText = "display:flex; gap:8px; align-items:center; margin-top:8px; flex-wrap:wrap;";
  const saveBtn = document.createElement("button");
  saveBtn.type = "submit";
  saveBtn.className = "btn btn-primary";
  saveBtn.textContent = "Save";
  const removeBtn = document.createElement("button");
  removeBtn.type = "button";
  removeBtn.className = "btn btn-danger";
  removeBtn.textContent = "Remove Channel";
  const status = document.createElement("span");
  row.appendChild(saveBtn);
  row.appendChild(removeBtn);
  row.appendChild(status);
  card.appendChild(row);

  return { card, ta, pinBox, status, removeBtn, channelId: ch.channel_id };
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
    render(container, config.birthday || {}, channels);
  }, { errorMsg: "Couldn’t load the birthday settings." });
}

function render(container, birthday, channels) {
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
    + "time zone. Announce in any number of channels, each with its own wording — handy "
    + "when one is for the whole server and another is for a smaller room. See who has a "
    + "birthday coming up on the Birthday Calendar page.";
  panel.appendChild(note);

  // ── Timing ──────────────────────────────────────────────────────────
  const timingForm = document.createElement("form");
  timingForm.className = "form card";
  panel.appendChild(timingForm);
  const timingHeading = document.createElement("div");
  timingHeading.className = "section-label";
  timingHeading.textContent = "Timing";
  timingForm.appendChild(timingHeading);

  const hourSelect = document.createElement("select");
  hourSelect.name = "birthday_announce_hour";
  const savedHour = Number.isInteger(birthday.birthday_announce_hour)
    ? birthday.birthday_announce_hour : 9;
  for (let h = 0; h < 24; h += 1) {
    const opt = document.createElement("option");
    opt.value = String(h);
    opt.textContent = String(h).padStart(2, "0") + ":00";
    if (h === savedHour) opt.selected = true;
    hourSelect.appendChild(opt);
  }
  timingForm.appendChild(
    field(
      "Announcement Time",
      hourSelect,
      "The hour announcements go out, in the server's own time zone (the offset set "
      + "on the Global page). Defaults to 09:00.",
    ),
  );

  const timingRow = document.createElement("div");
  timingRow.style.cssText = "display:flex; gap:8px; align-items:center; margin-top:8px;";
  const timingSaveBtn = document.createElement("button");
  timingSaveBtn.type = "submit";
  timingSaveBtn.className = "btn btn-primary";
  timingSaveBtn.textContent = "Save";
  const timingStatus = document.createElement("span");
  timingRow.appendChild(timingSaveBtn);
  timingRow.appendChild(timingStatus);
  timingForm.appendChild(timingRow);

  // ── Existing channels ───────────────────────────────────────────────
  const channelsHeading = document.createElement("div");
  channelsHeading.className = "section-label";
  channelsHeading.style.marginTop = "24px";
  channelsHeading.textContent = "Announcement Channels";
  panel.appendChild(channelsHeading);

  const list = document.createElement("div");
  panel.appendChild(list);

  const me = window.__dk_user || {};
  const sampleName = me.username || "user";

  const cfgs = birthday.channels || [];
  const cards = cfgs.map((ch) => buildChannelCard(list, ch, channels, sampleName));
  if (!cfgs.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "No channels are set up yet. Add your first one below.";
    list.appendChild(empty);
  }

  // ── Add a channel ───────────────────────────────────────────────────
  const addHeading = document.createElement("div");
  addHeading.className = "section-label";
  addHeading.style.marginTop = "16px";
  addHeading.textContent = "Add a Channel";
  panel.appendChild(addHeading);

  const addForm = document.createElement("form");
  addForm.className = "form card";
  panel.appendChild(addForm);

  const chanSlot = document.createElement("span");
  addForm.appendChild(
    field("Channel", chanSlot, "Birthdays are announced here too, alongside every channel already listed above."),
  );
  const picker = mountChannelPicker(
    chanSlot, channels, "0",
    { emptyValue: "0", emptyLabel: "(pick a channel)", label: "Channel" },
  );

  const { ta: addTa, pinBox: addPinBox } = buildMessageAndPinFields(addForm, {
    message: DEFAULT_MESSAGE, pin: false, sampleName,
  });

  const addRow = document.createElement("div");
  addRow.style.cssText = "display:flex; gap:8px; align-items:center; margin-top:8px;";
  const addBtn = document.createElement("button");
  addBtn.type = "submit";
  addBtn.className = "btn btn-primary";
  addBtn.textContent = "Add Channel";
  const addStatus = document.createElement("span");
  addRow.appendChild(addBtn);
  addRow.appendChild(addStatus);
  addForm.appendChild(addRow);

  // Lock after every card and picker mounts — each builds its own inputs, so
  // locking earlier would leave those live.
  if (lockUnlessAdmin(container)) return;

  wireTiming(timingForm, timingStatus, hourSelect);
  wireExistingCards(cards);
  wireRemove(cards, list, container, channels);
  wireAdd(addForm, addStatus, picker, addTa, addPinBox, list, container, channels);
}

// ── Wire: timing form ────────────────────────────────────────────────

function wireTiming(form, status, hourSelect) {
  guardForm(form);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await apiPut("/api/config/birthday/settings", {
        birthday_announce_hour: Number(hourSelect.value),
      });
      showStatus(status, true);
    } catch (err) {
      showStatus(status, false, err.message);
    }
  });
}

// ── Wire: save existing channel cards ────────────────────────────────

function wireExistingCards(cards) {
  for (const c of cards) {
    guardForm(c.card);
    trackCard(c.card);
    c.card.addEventListener("submit", async (e) => {
      e.preventDefault();
      const message = c.ta.value.trim();
      if (!message) {
        showStatus(c.status, false, "Message cannot be empty.");
        c.ta.focus();
        return;
      }
      try {
        await apiPut(`/api/config/birthday/${c.channelId}`, {
          message, pin: c.pinBox.checked,
        });
        showStatus(c.status, true);
        clearCardDirty(c.card);
      } catch (err) {
        showStatus(c.status, false, err.message);
      }
    });
  }
}

// ── Wire: remove buttons ─────────────────────────────────────────────

function wireRemove(cards, list, container, channels) {
  for (const c of cards) {
    c.removeBtn.addEventListener("click", async () => {
      const name = channelName(channels, String(c.channelId));
      const ok = await confirmDialog(
        `Stop announcing birthdays in ${name}?`,
        { title: "Remove Channel", danger: true, confirmLabel: "Remove Channel" },
      );
      if (!ok) return;
      try {
        await apiDelete(`/api/config/birthday/${c.channelId}`);
        const fresh = await loadConfig();
        // The removed card must go, so this rebuild can't be held back — but
        // say so when it takes unsaved edits with it (matches config-needle.js).
        if (hasDirtySibling(list, c.card)) {
          toast("Channel removed. Unsaved edits in the other channels were discarded.", "info");
        }
        render(container, fresh.birthday || {}, channels);
      } catch (err) {
        toast(err.message, "error");
      }
    });
  }
}

// ── Wire: add form ───────────────────────────────────────────────────

function wireAdd(form, status, picker, ta, pinBox, list, container, channels) {
  guardForm(form);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const channelId = picker.getValue() || "0";
    if (channelId === "0") {
      showStatus(status, false, "Pick a channel first.");
      return;
    }
    const message = ta.value.trim();
    if (!message) {
      showStatus(status, false, "Message cannot be empty.");
      ta.focus();
      return;
    }
    try {
      await apiPut(`/api/config/birthday/${channelId}`, { message, pin: pinBox.checked });
      const fresh = await loadConfig();
      // Adding a channel used to rebuild every card and silently drop whatever
      // was typed into an existing one — hold the rebuild while any sibling is
      // dirty and point at the reload instead (matches config-needle.js).
      if (hasDirtySibling(list, null)) {
        showStatus(status, true, "Added — reload to see it listed above.");
      } else {
        render(container, fresh.birthday || {}, channels);
      }
    } catch (err) {
      showStatus(status, false, err.message);
    }
  });
}
