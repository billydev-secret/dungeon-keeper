import { loadConfig, loadChannels, apiPut, apiDelete, showStatus, buildField } from "../config-helpers.js";
import { api, esc } from "../api.js";

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

function buildBoolSelect(name, value, trueLabel, falseLabel) {
  const sel = document.createElement("select");
  sel.name = name;
  const optTrue = document.createElement("option");
  optTrue.value = "true";
  optTrue.textContent = trueLabel || "Yes";
  if (value) optTrue.selected = true;
  const optFalse = document.createElement("option");
  optFalse.value = "false";
  optFalse.textContent = falseLabel || "No";
  if (!value) optFalse.selected = true;
  sel.appendChild(optTrue);
  sel.appendChild(optFalse);
  return sel;
}


function buildNumberInput(name, min, max, value) {
  const inp = document.createElement("input");
  inp.type = "number";
  inp.name = name;
  inp.min = String(min);
  inp.max = String(max);
  inp.value = String(value);
  return inp;
}

export function mount(container) {
  container.innerHTML = "";
  const loading = document.createElement("div");
  loading.className = "panel";
  loading.innerHTML = '<div class="empty">Loading config…</div>';
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
    sub.textContent = "Anonymous confession settings";
    header.appendChild(h2);
    header.appendChild(sub);
    panel.appendChild(header);

    if (!c || !c.configured) {
      const note = document.createElement("p");
      note.style.color = "var(--ink-dim)";
      note.style.marginBottom = "1rem";
      note.textContent = "Confessions are not yet configured. Set destination and log channels to enable.";
      panel.appendChild(note);

      const form = document.createElement("form");
      form.className = "form";
      panel.appendChild(form);

      const destSel = buildSelect("dest_channel_id", channels, "0", false);
      form.appendChild(buildField("Destination Channel", destSel, "Where anonymous confessions are posted"));

      const logSel = buildSelect("log_channel_id", channels, "0", false);
      form.appendChild(buildField("Log Channel", logSel, "Private moderator log channel"));

      const row = document.createElement("div");
      const saveBtn = document.createElement("button");
      saveBtn.type = "submit";
      saveBtn.className = "btn btn-primary";
      saveBtn.textContent = "Enable Confessions";
      const statusEl = document.createElement("span");
      row.appendChild(saveBtn);
      row.appendChild(statusEl);
      form.appendChild(row);

      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        const fd = new FormData(form);
        try {
          await apiPut("/api/config/confessions", {
            dest_channel_id: fd.get("dest_channel_id"),
            log_channel_id: fd.get("log_channel_id"),
          });
          showStatus(statusEl, true, "Saved — reloading…");
          setTimeout(() => mount(container), 1500);
        } catch (err) {
          showStatus(statusEl, false, err.message);
        }
      });
      return;
    }

    // ── Settings form ──────────────────────────────────────────────────
    const form = document.createElement("form");
    form.className = "form";
    panel.appendChild(form);

    const destSel = buildSelect("dest_channel_id", channels, c.dest_channel_id);
    form.appendChild(buildField("Destination Channel", destSel, "Where confessions are posted (text or forum channel)"));

    const logSel = buildSelect("log_channel_id", channels, c.log_channel_id);
    form.appendChild(buildField("Log Channel", logSel, "Private moderator log channel"));

    form.appendChild(buildField("Cooldown (seconds)", buildNumberInput("cooldown_seconds", 0, 86400, c.cooldown_seconds), "Per-user cooldown between confessions"));
    form.appendChild(buildField("Max Characters", buildNumberInput("max_chars", 100, 4000, c.max_chars)));
    form.appendChild(buildField("Per-day Limit", buildNumberInput("per_day_limit", 0, 100, c.per_day_limit), "Max confessions per user per day (0 = unlimited)"));

    form.appendChild(buildField("Replies Enabled", buildBoolSelect("replies_enabled", c.replies_enabled)));
    form.appendChild(buildField("DM Original Poster on Reply", buildBoolSelect("notify_op_on_reply", c.notify_op_on_reply)));
    form.appendChild(buildField("Panic Mode", buildBoolSelect("panic", c.panic, "On (pauses all confessions)", "Off")));

    const saveRow = document.createElement("div");
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
      try {
        await apiPut("/api/config/confessions", {
          dest_channel_id: fd.get("dest_channel_id"),
          log_channel_id: fd.get("log_channel_id"),
          cooldown_seconds: parseInt(fd.get("cooldown_seconds")) || 0,
          max_chars: parseInt(fd.get("max_chars")) || 2000,
          per_day_limit: parseInt(fd.get("per_day_limit")) || 0,
          replies_enabled: fd.get("replies_enabled") === "true",
          notify_op_on_reply: fd.get("notify_op_on_reply") === "true",
          panic: fd.get("panic") === "true",
        });
        showStatus(saveStatus, true);
      } catch (err) {
        showStatus(saveStatus, false, err.message);
      }
    });

    // ── Blocked users ──────────────────────────────────────────────────
    const sh3 = document.createElement("div");
    sh3.className = "section-label";
    sh3.textContent = "Blocked Users";
    panel.appendChild(sh3);

    const section = document.createElement("section");
    panel.appendChild(section);

    const shint = document.createElement("div");
    shint.className = "field-hint";
    shint.style.marginBottom = "10px";
    shint.textContent = "Blocked users cannot submit confessions or anonymous replies.";
    section.appendChild(shint);

    const blockedList = document.createElement("div");
    section.appendChild(blockedList);
    renderBlocked(blockedList, c.blocked_users);

    const blockForm = document.createElement("form");
    blockForm.className = "form";
    blockForm.style.cssText = "margin-top:12px; display:flex; gap:8px; align-items:flex-end; max-width:none;";
    section.appendChild(blockForm);

    const bfField = document.createElement("div");
    bfField.className = "field";
    bfField.style.cssText = "flex:1;";
    const bfLabel = document.createElement("label");
    bfLabel.textContent = "Block User by ID";
    const bfInput = document.createElement("input");
    bfInput.type = "text";
    bfInput.name = "user_id";
    bfInput.placeholder = "Discord user ID";
    bfInput.pattern = "[0-9]+";
    bfInput.style.width = "100%";
    bfField.appendChild(bfLabel);
    bfField.appendChild(bfInput);
    blockForm.appendChild(bfField);

    const bfRow = document.createElement("div");
    const bfBtn = document.createElement("button");
    bfBtn.type = "submit";
    bfBtn.className = "btn";
    bfBtn.textContent = "Block";
    const bfStatus = document.createElement("span");
    bfStatus.style.marginLeft = "6px";
    bfRow.appendChild(bfBtn);
    bfRow.appendChild(bfStatus);
    blockForm.appendChild(bfRow);

    blockForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const uid = bfInput.value.trim();
      if (!uid) return;
      try {
        await fetch("/api/config/confessions/block/" + encodeURIComponent(uid), {
          method: "PUT",
          credentials: "same-origin",
        });
        blockForm.reset();
        await refreshBlocked(blockedList);
        showStatus(bfStatus, true, "Blocked");
      } catch (err) {
        showStatus(bfStatus, false, err.message);
      }
    });

    // ── Post-button launcher ───────────────────────────────────────────
    const lh3 = document.createElement("div");
    lh3.className = "section-label";
    lh3.textContent = "Button Launcher";
    panel.appendChild(lh3);

    const lsec = document.createElement("section");
    panel.appendChild(lsec);

    if (c.launcher_channel_id !== "0") {
      const currentHint = document.createElement("div");
      currentHint.className = "field-hint";
      currentHint.style.marginBottom = "12px";
      const ch = channels.find((x) => x.id === c.launcher_channel_id);
      const chName = ch ? "#" + ch.name : "channel " + c.launcher_channel_id;
      currentHint.textContent = "Currently posted in " + chName + ".";
      lsec.appendChild(currentHint);
    }

    const lhint = document.createElement("div");
    lhint.className = "field-hint";
    lhint.style.marginBottom = "10px";
    lhint.textContent = "Post or move the Confess button to a channel.";
    lsec.appendChild(lhint);

    const pbForm = document.createElement("form");
    pbForm.className = "form";
    pbForm.style.cssText = "display:flex; gap:8px; align-items:flex-end; max-width:none;";
    lsec.appendChild(pbForm);

    const pbField = document.createElement("div");
    pbField.className = "field";
    pbField.style.cssText = "flex:1;";
    const pbLabel = document.createElement("label");
    pbLabel.textContent = "Channel";
    const pbSel = buildSelect("pb_channel_id", channels, c.dest_channel_id);
    pbField.appendChild(pbLabel);
    pbField.appendChild(pbSel);
    pbForm.appendChild(pbField);

    const pbRow = document.createElement("div");
    const pbBtn = document.createElement("button");
    pbBtn.type = "submit";
    pbBtn.className = "btn btn-primary";
    pbBtn.textContent = "Post Button";
    const pbStatus = document.createElement("span");
    pbStatus.style.marginLeft = "6px";
    pbRow.appendChild(pbBtn);
    pbRow.appendChild(pbStatus);
    pbForm.appendChild(pbRow);

    pbForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const channelId = pbSel.value;
      if (!channelId || channelId === "0") return;
      pbBtn.disabled = true;
      try {
        await fetch("/api/config/confessions/post-button", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ channel_id: channelId }),
        });
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
    em.textContent = "No blocked users.";
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
    removeBtn.title = "Unblock";
    removeBtn.dataset.unblock = u.id;
    removeBtn.textContent = "×";
    chip.appendChild(nameSpan);
    chip.appendChild(removeBtn);
    chips.appendChild(chip);
    removeBtn.addEventListener("click", async () => {
      try {
        await apiDelete("/api/config/confessions/block/" + encodeURIComponent(u.id));
        await refreshBlocked(container);
      } catch (err) {
        console.error("Unblock failed:", err);
      }
    });
  }
  container.appendChild(chips);
}

async function refreshBlocked(container) {
  const fresh = await api("/api/config");
  renderBlocked(container, fresh.confessions.blocked_users);
}
