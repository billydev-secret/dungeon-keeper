import {
  loadConfig, loadChannels, apiPut, apiDelete, showStatus, buildField,
  mountChannelPicker, guardForm, renderMetaWarning,
} from "../config-helpers.js";
import { api, apiPost } from "../api.js";
import { confirmDialog, toast } from "../ui.js";

let _fieldSeq = 0;

// buildField renders a bare <label>; tie it to its control by id so screen
// readers announce the label and a label tap focuses the field (W-A7).
function field(labelText, control, hint) {
  const div = buildField(labelText, control, hint);
  if (control instanceof HTMLElement && /^(INPUT|SELECT|TEXTAREA)$/.test(control.tagName)) {
    const id = control.id || `cf-field-${++_fieldSeq}`;
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

function buildNumberInput(name, min, max, value) {
  const inp = document.createElement("input");
  inp.type = "number";
  inp.name = name;
  inp.required = true;
  inp.min = String(min);
  inp.max = String(max);
  inp.step = "1";
  inp.value = String(value);
  inp.style.maxWidth = "140px";
  return inp;
}

function card(parent, title) {
  const el = document.createElement("div");
  el.className = "card";
  const lbl = document.createElement("div");
  lbl.className = "section-label";
  lbl.textContent = title;
  el.appendChild(lbl);
  parent.appendChild(el);
  return el;
}

export function mount(container) {
  container.innerHTML = "";
  const loading = document.createElement("div");
  loading.className = "panel";
  loading.innerHTML = '<div class="empty">Loading confession settings…</div>';
  container.appendChild(loading);

  (async () => {
    const [config, channels] = await Promise.all([loadConfig(), loadChannels()]);
    const c = config.confessions;

    container.innerHTML = "";
    const panel = document.createElement("div");
    panel.className = "panel";
    container.appendChild(panel);

    const header = document.createElement("header");
    const h2 = document.createElement("h2");
    h2.textContent = "Confessions";
    const sub = document.createElement("div");
    sub.className = "subtitle";
    sub.textContent = "Let members post anonymously, with a private record only your moderators can see";
    header.appendChild(h2);
    header.appendChild(sub);
    panel.appendChild(header);

    const warning = renderMetaWarning();
    if (warning) {
      const w = document.createElement("div");
      w.innerHTML = warning;
      panel.appendChild(w.firstElementChild);
    }

    // ── First-run state: pick the two channels and turn it on ──────────
    if (!c || !c.configured) {
      const note = document.createElement("p");
      note.style.color = "var(--ink-dim)";
      note.style.marginBottom = "1rem";
      note.textContent =
        "Confessions isn't set up yet. Choose where confessions appear and where "
        + "moderators can review them, and it starts working right away.";
      panel.appendChild(note);

      const form = document.createElement("form");
      form.className = "form form-cards";
      panel.appendChild(form);

      const setupCard = card(form, "Get Started");

      const destSlot = document.createElement("span");
      setupCard.appendChild(field(
        "Confessions Channel",
        destSlot,
        "Where anonymous confessions are posted for everyone to read. A text or forum channel.",
      ));
      const destPicker = mountChannelPicker(destSlot, channels, "0", {
        emptyValue: "0", emptyLabel: "(pick a channel)", label: "Confessions Channel",
      });

      const logSlot = document.createElement("span");
      setupCard.appendChild(field(
        "Moderator Log Channel",
        logSlot,
        "A private channel where each confession is recorded with its author, so your team can act if one crosses a line. Keep it staff-only.",
      ));
      const logPicker = mountChannelPicker(logSlot, channels, "0", {
        emptyValue: "0", emptyLabel: "(pick a channel)", label: "Moderator Log Channel",
      });

      const row = document.createElement("div");
      row.style.cssText = "display:flex; gap:8px; align-items:center;";
      const saveBtn = document.createElement("button");
      saveBtn.type = "submit";
      saveBtn.className = "btn btn-primary";
      saveBtn.textContent = "Turn On Confessions";
      const statusEl = document.createElement("span");
      row.appendChild(saveBtn);
      row.appendChild(statusEl);
      form.appendChild(row);

      guardForm(form);

      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        // Snowflakes stay strings.
        const dest = destPicker.getValue() || "0";
        const log = logPicker.getValue() || "0";
        if (dest === "0") {
          showStatus(statusEl, false, "Pick a Confessions Channel first.");
          return;
        }
        if (log === "0") {
          showStatus(statusEl, false, "Pick a Moderator Log Channel first.");
          return;
        }
        try {
          await apiPut("/api/config/confessions", {
            dest_channel_id: dest,
            log_channel_id: log,
          });
          showStatus(statusEl, true, "Saved — reloading…");
          setTimeout(() => mount(container), 1500);
        } catch (err) {
          showStatus(statusEl, false, err.message);
        }
      });
      return;
    }

    // ── Panic banner (W-C6): say plainly that confessions are paused ────
    const panicBanner = document.createElement("div");
    panicBanner.className = "field-hint";
    panicBanner.setAttribute("role", "status");
    panicBanner.style.cssText =
      "border:1px solid var(--red,#c00); border-radius:6px; padding:10px; margin-bottom:14px; line-height:1.5;";
    panicBanner.textContent =
      "Confessions are paused — members can't submit anything until you uncheck "
      + "\"Pause All Confessions\" below and save.";
    panel.appendChild(panicBanner);

    // ── Settings form ──────────────────────────────────────────────────
    const form = document.createElement("form");
    form.className = "form form-cards";
    panel.appendChild(form);

    const channelCard = card(form, "Channels");

    const destSlot = document.createElement("span");
    channelCard.appendChild(field(
      "Confessions Channel",
      destSlot,
      "Where anonymous confessions are posted for everyone to read. A text or forum channel.",
    ));
    const destPicker = mountChannelPicker(
      destSlot, channels, String(c.dest_channel_id || "0"),
      { emptyValue: "0", emptyLabel: "(disabled)", label: "Confessions Channel" },
    );

    const logSlot = document.createElement("span");
    channelCard.appendChild(field(
      "Moderator Log Channel",
      logSlot,
      "A private channel where each confession is recorded with its author, so your team can act if one crosses a line. Keep it staff-only.",
    ));
    const logPicker = mountChannelPicker(
      logSlot, channels, String(c.log_channel_id || "0"),
      { emptyValue: "0", emptyLabel: "(disabled)", label: "Moderator Log Channel" },
    );

    const limitsCard = card(form, "Limits");
    limitsCard.appendChild(field(
      "Wait Between Confessions (seconds)",
      buildNumberInput("cooldown_seconds", 0, 86400, c.cooldown_seconds),
      "How long one member must wait after posting before they can post again. Enter 0 for no wait.",
    ));
    limitsCard.appendChild(field(
      "Longest Confession (characters)",
      buildNumberInput("max_chars", 100, 4000, c.max_chars),
      "Anything longer is refused when the member presses submit. Between 100 and 4000.",
    ));
    limitsCard.appendChild(field(
      "Confessions per Member per Day",
      buildNumberInput("per_day_limit", 0, 100, c.per_day_limit),
      "How many one member may post in a day. Enter 0 for no daily limit.",
    ));

    const behaviorCard = card(form, "Replies & Safety");
    const repliesToggle = toggleField(
      "replies_enabled",
      "Allow Anonymous Replies",
      c.replies_enabled,
      "Members can reply to a confession without revealing who they are. Unchecked, confessions can only be reacted to.",
    );
    behaviorCard.appendChild(repliesToggle.wrap);

    const notifyToggle = toggleField(
      "notify_op_on_reply",
      "Send the Author a DM When Someone Replies",
      c.notify_op_on_reply,
      "The original poster gets a private message pointing at the reply. They stay anonymous to everyone else.",
    );
    behaviorCard.appendChild(notifyToggle.wrap);

    const panicToggle = toggleField(
      "panic",
      "Pause All Confessions",
      c.panic,
      "An emergency stop: nobody can submit a confession or a reply while this is checked. Existing posts stay up.",
    );
    behaviorCard.appendChild(panicToggle.wrap);

    function syncPanic() {
      panicBanner.style.display = panicToggle.box.checked ? "" : "none";
    }
    panicToggle.box.addEventListener("change", syncPanic);
    syncPanic();

    const saveRow = document.createElement("div");
    saveRow.style.cssText = "display:flex; gap:8px; align-items:center;";
    const saveBtn = document.createElement("button");
    saveBtn.type = "submit";
    saveBtn.className = "btn btn-primary";
    saveBtn.textContent = "Save";
    const saveStatus = document.createElement("span");
    saveRow.appendChild(saveBtn);
    saveRow.appendChild(saveStatus);
    form.appendChild(saveRow);

    guardForm(form);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const nums = {};
      for (const [name, label, min, max] of [
        ["cooldown_seconds", "Wait Between Confessions", 0, 86400],
        ["max_chars", "Longest Confession", 100, 4000],
        ["per_day_limit", "Confessions per Member per Day", 0, 100],
      ]) {
        const raw = String(fd.get(name) ?? "").trim();
        const v = parseInt(raw, 10);
        if (raw === "" || !Number.isFinite(v) || v < min || v > max) {
          showStatus(saveStatus, false, `${label} must be a whole number between ${min} and ${max}.`);
          form.querySelector(`[name="${name}"]`).focus();
          return;
        }
        nums[name] = v;
      }
      try {
        await apiPut("/api/config/confessions", {
          dest_channel_id: destPicker.getValue() || "0",
          log_channel_id: logPicker.getValue() || "0",
          ...nums,
          replies_enabled: repliesToggle.box.checked,
          notify_op_on_reply: notifyToggle.box.checked,
          panic: panicToggle.box.checked,
        });
        showStatus(saveStatus, true);
      } catch (err) {
        showStatus(saveStatus, false, err.message);
      }
    });

    // ── Blocked users ──────────────────────────────────────────────────
    const blockForm = document.createElement("form");
    blockForm.className = "form form-cards";
    panel.appendChild(blockForm);

    const blockCard = card(blockForm, "Members Who May Not Confess");

    const shint = document.createElement("div");
    shint.className = "field-hint";
    shint.style.marginBottom = "10px";
    shint.textContent =
      "Blocked members can't submit confessions or anonymous replies. They aren't told they're blocked. "
      + "Blocking and unblocking take effect straight away.";
    blockCard.appendChild(shint);

    const blockedList = document.createElement("div");
    blockCard.appendChild(blockedList);
    renderBlocked(blockedList, c.blocked_users);

    const bfField = document.createElement("div");
    bfField.className = "field";
    const bfLabel = document.createElement("label");
    bfLabel.textContent = "Block a Member by ID";
    bfLabel.htmlFor = "cf-block-id";
    const bfInput = document.createElement("input");
    bfInput.type = "text";
    bfInput.id = "cf-block-id";
    bfInput.name = "user_id";
    bfInput.placeholder = "e.g. 123456789012345678";
    bfInput.inputMode = "numeric";
    bfInput.pattern = "[0-9]+";
    bfInput.style.maxWidth = "280px";
    const bfHint = document.createElement("div");
    bfHint.className = "field-hint";
    bfHint.textContent =
      "The long number Discord copies with \"Copy User ID\" (turn on Developer Mode in Discord's settings to see it).";
    bfField.appendChild(bfLabel);
    bfField.appendChild(bfInput);
    bfField.appendChild(bfHint);
    blockCard.appendChild(bfField);

    const bfRow = document.createElement("div");
    bfRow.style.cssText = "display:flex; gap:8px; align-items:center;";
    const bfBtn = document.createElement("button");
    bfBtn.type = "submit";
    bfBtn.className = "btn";
    bfBtn.textContent = "Block Member";
    const bfStatus = document.createElement("span");
    bfRow.appendChild(bfBtn);
    bfRow.appendChild(bfStatus);
    blockForm.appendChild(bfRow);

    guardForm(blockForm);

    blockForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const uid = bfInput.value.trim();
      if (!uid) {
        showStatus(bfStatus, false, "Enter a member ID first.");
        bfInput.focus();
        return;
      }
      if (!/^\d{15,25}$/.test(uid)) {
        showStatus(bfStatus, false, "That doesn't look like a member ID — it should be 17 to 20 digits.");
        bfInput.focus();
        return;
      }
      try {
        await apiPut("/api/config/confessions/block/" + encodeURIComponent(uid), {});
        blockForm.reset();
        await refreshBlocked(blockedList);
        showStatus(bfStatus, true, "Blocked");
      } catch (err) {
        showStatus(bfStatus, false, err.message);
      }
    });

    // ── Post-button launcher ───────────────────────────────────────────
    const pbForm = document.createElement("form");
    pbForm.className = "form form-cards";
    panel.appendChild(pbForm);

    const pbCard = card(pbForm, "Confess Button");

    if (c.launcher_channel_id !== "0") {
      const currentHint = document.createElement("div");
      currentHint.className = "field-hint";
      currentHint.style.marginBottom = "12px";
      const ch = channels.find((x) => x.id === c.launcher_channel_id);
      const chName = ch ? "#" + ch.name : "channel " + c.launcher_channel_id;
      currentHint.textContent = "The button is posted in " + chName + " right now.";
      pbCard.appendChild(currentHint);
    }

    const pbSlot = document.createElement("span");
    pbCard.appendChild(field(
      "Post the Button In",
      pbSlot,
      "Posts the Confess button members press to write a confession. Posting it again moves it: the old button stops working.",
    ));
    const pbPicker = mountChannelPicker(
      pbSlot, channels, String(c.dest_channel_id || "0"),
      { emptyValue: "0", emptyLabel: "(pick a channel)", label: "Post the Button In" },
    );

    const pbRow = document.createElement("div");
    pbRow.style.cssText = "display:flex; gap:8px; align-items:center;";
    const pbBtn = document.createElement("button");
    pbBtn.type = "submit";
    pbBtn.className = "btn btn-primary";
    pbBtn.textContent = "Post Button";
    const pbStatus = document.createElement("span");
    pbRow.appendChild(pbBtn);
    pbRow.appendChild(pbStatus);
    pbForm.appendChild(pbRow);

    guardForm(pbForm);

    pbForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const channelId = pbPicker.getValue() || "0";
      if (channelId === "0") {
        showStatus(pbStatus, false, "Pick a channel first.");
        return;
      }
      pbBtn.disabled = true;
      try {
        await apiPost("/api/config/confessions/post-button", { channel_id: channelId });
        showStatus(pbStatus, true, "Button posted");
        setTimeout(() => mount(container), 1500);
      } catch (err) {
        showStatus(pbStatus, false, err.message);
      } finally {
        pbBtn.disabled = false;
      }
    });
  })();
}

function renderBlocked(container, users) {
  container.innerHTML = "";
  if (!users || !users.length) {
    const em = document.createElement("div");
    em.className = "empty";
    em.style.padding = "4px 0";
    em.textContent = "Nobody is blocked.";
    container.appendChild(em);
    return;
  }
  const chips = document.createElement("div");
  chips.className = "exempt-chips";
  for (const u of users) {
    const chip = document.createElement("span");
    chip.className = "exempt-chip";
    const nameSpan = document.createElement("span");
    nameSpan.textContent = u.name;
    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.title = `Unblock ${u.name}`;
    removeBtn.setAttribute("aria-label", `Unblock ${u.name}`);
    removeBtn.dataset.unblock = u.id;
    removeBtn.textContent = "×";
    chip.appendChild(nameSpan);
    chip.appendChild(removeBtn);
    chips.appendChild(chip);
    removeBtn.addEventListener("click", async () => {
      const ok = await confirmDialog(
        `Unblock ${u.name}? They will be able to post confessions and anonymous replies again.`,
        { title: "Unblock Member", confirmLabel: "Unblock" },
      );
      if (!ok) return;
      try {
        await apiDelete("/api/config/confessions/block/" + encodeURIComponent(u.id));
        await refreshBlocked(container);
      } catch (err) {
        toast("Couldn't unblock that member: " + err.message, "error");
      }
    });
  }
  container.appendChild(chips);
}

async function refreshBlocked(container) {
  const fresh = await api("/api/config");
  renderBlocked(container, fresh.confessions.blocked_users);
}
