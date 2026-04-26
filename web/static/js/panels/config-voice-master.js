import { loadChannels, showStatus, buildField } from "../config-helpers.js";

async function apiGet(path) {
  const res = await fetch(path, { credentials: "same-origin" });
  if (!res.ok) throw new Error(`${res.status}: ${res.statusText}`);
  return res.json();
}

async function apiPost(path, body) {
  const res = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: body == null ? null : JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { const b = await res.json(); if (b.detail) detail = b.detail; } catch (_) {}
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json();
}

async function apiDel(path) {
  const res = await fetch(path, { method: "DELETE", credentials: "same-origin" });
  if (!res.ok) throw new Error(`${res.status}: ${res.statusText}`);
  return res.json();
}

function clearChildren(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
}

function selectFromChannels(name, channels, selectedId, kind) {
  const sel = document.createElement("select");
  sel.name = name;
  const none = document.createElement("option");
  none.value = "0";
  none.textContent = "(unset)";
  sel.appendChild(none);
  for (const ch of channels) {
    if (kind && ch.kind !== kind) continue;
    const opt = document.createElement("option");
    opt.value = ch.id;
    opt.textContent = (kind === "voice" ? "🔊 " : kind === "category" ? "📁 " : "# ") + ch.name;
    if (ch.id === selectedId) opt.selected = true;
    sel.appendChild(opt);
  }
  return sel;
}

function numberInput(name, value, min = 0) {
  const inp = document.createElement("input");
  inp.type = "number";
  inp.name = name;
  inp.min = String(min);
  inp.value = String(value ?? 0);
  return inp;
}

function textInput(name, value) {
  const inp = document.createElement("input");
  inp.type = "text";
  inp.name = name;
  inp.value = value || "";
  return inp;
}

function boolSelect(name, value) {
  const sel = document.createElement("select");
  sel.name = name;
  for (const [v, label] of [["false", "No"], ["true", "Yes"]]) {
    const o = document.createElement("option");
    o.value = v;
    o.textContent = label;
    if ((value && v === "true") || (!value && v === "false")) o.selected = true;
    sel.appendChild(o);
  }
  return sel;
}

function checkboxList(name, options, selectedSet) {
  const div = document.createElement("div");
  div.className = "checkbox-list";
  for (const opt of options) {
    const lbl = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.name = name;
    cb.value = opt;
    if (selectedSet.has(opt)) cb.checked = true;
    lbl.appendChild(cb);
    lbl.appendChild(document.createTextNode(" " + opt));
    div.appendChild(lbl);
  }
  return div;
}

export function mount(container) {
  clearChildren(container);
  const loading = document.createElement("div");
  loading.className = "panel";
  loading.textContent = "Loading Voice Master config…";
  container.appendChild(loading);

  (async () => {
    let cfg, channels;
    try {
      [cfg, channels] = await Promise.all([
        apiGet("/api/voice-master/config"),
        loadChannels(),
      ]);
    } catch (err) {
      loading.textContent = "Failed to load: " + err.message;
      return;
    }

    clearChildren(container);
    const panel = document.createElement("div");
    panel.className = "panel";
    container.appendChild(panel);

    const h2 = document.createElement("h2");
    h2.textContent = "Voice Master";
    panel.appendChild(h2);
    const sub = document.createElement("div");
    sub.className = "subtitle";
    sub.textContent = "Member-owned voice channels created by joining the Hub.";
    panel.appendChild(sub);

    const form = document.createElement("form");
    form.className = "form";
    panel.appendChild(form);

    form.appendChild(buildField(
      "Hub channel",
      selectFromChannels("hub_channel_id", channels, cfg.hub_channel_id, "voice"),
      "Voice channel members join to spin up their own room.",
    ));
    form.appendChild(buildField(
      "Target category",
      selectFromChannels("category_id", channels, cfg.category_id, "category"),
      "Category where created channels live.",
    ));
    form.appendChild(buildField(
      "Control channel",
      selectFromChannels("control_channel_id", channels, cfg.control_channel_id, "text"),
      "Text channel for the persistent panel and knock requests.",
    ));
    form.appendChild(buildField(
      "Default name template",
      textInput("default_name_template", cfg.default_name_template),
      "Tokens: {display_name}, {username}.",
    ));
    form.appendChild(buildField("Default user limit (0 = no cap)", numberInput("default_user_limit", cfg.default_user_limit)));
    form.appendChild(buildField("Default bitrate (0 = guild default)", numberInput("default_bitrate", cfg.default_bitrate)));
    form.appendChild(buildField("Create cooldown (seconds)", numberInput("create_cooldown_s", cfg.create_cooldown_s)));
    form.appendChild(buildField("Max channels per member", numberInput("max_per_member", cfg.max_per_member, 1)));
    form.appendChild(buildField("Trust list cap", numberInput("trust_cap", cfg.trust_cap)));
    form.appendChild(buildField("Block list cap", numberInput("block_cap", cfg.block_cap)));
    form.appendChild(buildField("Owner-disconnect grace (s) before claim", numberInput("owner_grace_s", cfg.owner_grace_s)));
    form.appendChild(buildField("Empty-channel grace (s) before delete", numberInput("empty_grace_s", cfg.empty_grace_s)));
    form.appendChild(buildField("Trusted prune (days, 0 = never)", numberInput("trusted_prune_days", cfg.trusted_prune_days)));
    form.appendChild(buildField("Disable saves (force defaults)", boolSelect("disable_saves", cfg.disable_saves)));
    form.appendChild(buildField(
      "Post panel in new channel chat",
      boolSelect("post_inline_panel", cfg.post_inline_panel),
      "Auto-post the control panel into each new channel's text chat.",
    ));
    form.appendChild(buildField(
      "Saveable fields",
      checkboxList("saveable_fields",
        ["name", "limit", "locked", "hidden", "trusted", "blocked"],
        new Set(cfg.saveable_fields),
      ),
      "Which profile fields owners may persist.",
    ));

    const saveRow = document.createElement("div");
    const saveBtn = document.createElement("button");
    saveBtn.type = "submit";
    saveBtn.className = "btn btn-primary";
    saveBtn.textContent = "Save settings";
    const saveStatus = document.createElement("span");
    saveRow.appendChild(saveBtn);
    saveRow.appendChild(saveStatus);
    form.appendChild(saveRow);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const saveable = [...form.querySelectorAll('input[name="saveable_fields"]:checked')].map(el => el.value);
      const payload = {
        hub_channel_id: parseInt(fd.get("hub_channel_id"), 10) || 0,
        category_id: parseInt(fd.get("category_id"), 10) || 0,
        control_channel_id: parseInt(fd.get("control_channel_id"), 10) || 0,
        default_name_template: String(fd.get("default_name_template") || ""),
        default_user_limit: parseInt(fd.get("default_user_limit"), 10) || 0,
        default_bitrate: parseInt(fd.get("default_bitrate"), 10) || 0,
        create_cooldown_s: parseInt(fd.get("create_cooldown_s"), 10) || 0,
        max_per_member: parseInt(fd.get("max_per_member"), 10) || 1,
        trust_cap: parseInt(fd.get("trust_cap"), 10) || 0,
        block_cap: parseInt(fd.get("block_cap"), 10) || 0,
        owner_grace_s: parseInt(fd.get("owner_grace_s"), 10) || 0,
        empty_grace_s: parseInt(fd.get("empty_grace_s"), 10) || 0,
        trusted_prune_days: parseInt(fd.get("trusted_prune_days"), 10) || 0,
        disable_saves: fd.get("disable_saves") === "true",
        saveable_fields: saveable,
        post_inline_panel: fd.get("post_inline_panel") === "true",
      };
      try {
        await apiPost("/api/voice-master/config", payload);
        showStatus(saveStatus, true);
      } catch (err) {
        showStatus(saveStatus, false, err.message);
      }
    });

    // ── Name blocklist ────────────────────────────────────────────────
    const blHeader = document.createElement("div");
    blHeader.className = "section-label";
    blHeader.textContent = "Channel-name blocklist";
    panel.appendChild(blHeader);

    const blForm = document.createElement("form");
    blForm.className = "form";
    panel.appendChild(blForm);
    const blInput = document.createElement("input");
    blInput.type = "text";
    blInput.placeholder = "substring (case-insensitive)";
    blForm.appendChild(blInput);
    const blAddBtn = document.createElement("button");
    blAddBtn.type = "submit";
    blAddBtn.className = "btn";
    blAddBtn.textContent = "Add";
    blForm.appendChild(blAddBtn);

    const blList = document.createElement("ul");
    blList.style.marginTop = "10px";
    panel.appendChild(blList);

    function renderList(patterns) {
      clearChildren(blList);
      for (const p of patterns) {
        const li = document.createElement("li");
        li.textContent = p + " ";
        const del = document.createElement("button");
        del.type = "button";
        del.className = "btn btn-danger btn-sm";
        del.textContent = "remove";
        del.addEventListener("click", async () => {
          try {
            await apiDel("/api/voice-master/name-blocklist/" + encodeURIComponent(p));
            li.remove();
          } catch (err) {
            alert("Remove failed: " + err.message);
          }
        });
        li.appendChild(del);
        blList.appendChild(li);
      }
    }
    renderList(cfg.name_blocklist || []);

    blForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const pattern = blInput.value.trim();
      if (!pattern) return;
      try {
        await apiPost("/api/voice-master/name-blocklist", { pattern });
        blInput.value = "";
        const fresh = await apiGet("/api/voice-master/config");
        renderList(fresh.name_blocklist || []);
      } catch (err) {
        alert("Add failed: " + err.message);
      }
    });
  })();
}
