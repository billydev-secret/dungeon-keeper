import { apiPost } from "../api.js";
import {
  loadConfig, loadChannels, loadRoles, apiPut, showStatus, buildField,
  mountRolePicker,
} from "../config-helpers.js";

function buildChannelInput(name, channels, selectedId) {
  if (channels.length > 0) {
    const sel = document.createElement("select");
    sel.name = name;
    const none = document.createElement("option");
    none.value = "0";
    none.textContent = "(none)";
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
  // Fallback: text input when channel list isn't available
  const inp = document.createElement("input");
  inp.type = "text";
  inp.name = name;
  inp.placeholder = "Channel ID";
  inp.pattern = "[0-9]*";
  if (selectedId && selectedId !== "0") inp.value = selectedId;
  return inp;
}


export function mount(container) {
  while (container.firstChild) container.removeChild(container.firstChild);

  const loadingPanel = document.createElement("div");
  loadingPanel.className = "panel";
  const loadingMsg = document.createElement("div");
  loadingMsg.className = "empty";
  loadingMsg.textContent = "Loading config…";
  loadingPanel.appendChild(loadingMsg);
  container.appendChild(loadingPanel);

  (async () => {
    let config, channels, roles;
    try {
      [config, channels, roles] = await Promise.all([loadConfig(), loadChannels(), loadRoles()]);
    } catch (err) {
      while (container.firstChild) container.removeChild(container.firstChild);
      const errPanel = document.createElement("div");
      errPanel.className = "panel";
      const errMsg = document.createElement("div");
      errMsg.className = "error";
      errMsg.textContent = "Failed to load config: " + err.message;
      errPanel.appendChild(errMsg);
      container.appendChild(errPanel);
      return;
    }

    const d = config.dms || {};

    while (container.firstChild) container.removeChild(container.firstChild);
    const panel = document.createElement("div");
    panel.className = "panel";
    container.appendChild(panel);

    const header = document.createElement("header");
    const h2 = document.createElement("h2");
    h2.textContent = "DM Permissions";
    const sub = document.createElement("div");
    sub.className = "subtitle";
    sub.textContent = "DM request system channels and status roles";
    header.appendChild(h2);
    header.appendChild(sub);
    panel.appendChild(header);

    if (channels.length === 0) {
      const warn = document.createElement("div");
      warn.className = "field-hint";
      warn.style.cssText = "margin-bottom:12px; color:var(--ink-dim);";
      warn.textContent = "Channel list unavailable — enter channel IDs directly, or restart the bot and refresh.";
      panel.appendChild(warn);
    }

    // ── Channel settings form ────────────────────────────────────────
    const form = document.createElement("form");
    form.className = "form";
    panel.appendChild(form);

    const reqCtrl = buildChannelInput("request_channel_id", channels, d.request_channel_id);
    form.appendChild(buildField(
      "Request Channel",
      reqCtrl,
      "Where pending DM requests are posted for moderators to review",
    ));

    const auditCtrl = buildChannelInput("audit_channel_id", channels, d.audit_channel_id);
    form.appendChild(buildField(
      "Audit Channel",
      auditCtrl,
      "Private log of all DM permission events",
    ));

    // ── Status role overrides ────────────────────────────────────────
    const rolesLabel = document.createElement("div");
    rolesLabel.className = "section-label";
    rolesLabel.textContent = "Status Roles";
    form.appendChild(rolesLabel);

    const rolesHint = document.createElement("div");
    rolesHint.className = "field-hint";
    rolesHint.style.marginBottom = "10px";
    rolesHint.textContent =
      "Use existing server roles for each DM status. Leave one unset and the bot "
      + "falls back to its own role for that status (“DMs: Open”, "
      + "“DMs: Ask” or “DMs: Closed”), creating it if needed.";
    form.appendChild(rolesHint);

    const roleDefs = [
      ["open", "Open Role", "Members with this role accept DMs from anyone"],
      ["ask", "Ask Role", "Members with this role want a request first (the default status)"],
      ["closed", "Closed Role", "Members with this role aren't accepting DM requests"],
    ];
    const rolePickers = {};
    for (const [mode, label, hint] of roleDefs) {
      const slot = document.createElement("span");
      form.appendChild(buildField(label, slot, hint));
      rolePickers[mode] = mountRolePicker(slot, roles, d[mode + "_role_id"] || "0");
    }

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
        await apiPut("/api/config/dms", {
          request_channel_id: fd.get("request_channel_id") || "0",
          audit_channel_id: fd.get("audit_channel_id") || "0",
          open_role_id: rolePickers.open.getValue() || "0",
          ask_role_id: rolePickers.ask.getValue() || "0",
          closed_role_id: rolePickers.closed.getValue() || "0",
        });
        showStatus(saveStatus, true);
      } catch (err) {
        showStatus(saveStatus, false, err.message);
      }
    });

    // ── Panel button launcher ────────────────────────────────────────
    const lh3 = document.createElement("div");
    lh3.className = "section-label";
    lh3.textContent = "Request Panel";
    panel.appendChild(lh3);

    const lsec = document.createElement("section");
    panel.appendChild(lsec);

    if (d.panel_channel_id && d.panel_channel_id !== "0") {
      const curHint = document.createElement("div");
      curHint.className = "field-hint";
      curHint.style.marginBottom = "12px";
      const ch = channels.find((x) => x.id === d.panel_channel_id);
      curHint.textContent = "Currently posted in " + (ch ? "#" + ch.name : "channel " + d.panel_channel_id) + ".";
      lsec.appendChild(curHint);
    }

    const lhint = document.createElement("div");
    lhint.className = "field-hint";
    lhint.style.marginBottom = "10px";
    lhint.textContent = "Post or move the DM request button to a channel.";
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
    const pbCtrl = buildChannelInput("pb_channel_id", channels, d.panel_channel_id || "0");
    pbField.appendChild(pbLabel);
    pbField.appendChild(pbCtrl);
    pbForm.appendChild(pbField);

    const pbRow = document.createElement("div");
    const pbBtn = document.createElement("button");
    pbBtn.type = "submit";
    pbBtn.className = "btn btn-primary";
    pbBtn.textContent = "Post Panel";
    const pbStatus = document.createElement("span");
    pbStatus.style.marginLeft = "6px";
    pbRow.appendChild(pbBtn);
    pbRow.appendChild(pbStatus);
    pbForm.appendChild(pbRow);

    pbForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const channelId = pbCtrl.tagName === "SELECT" ? pbCtrl.value : pbCtrl.value.trim();
      if (!channelId || channelId === "0") return;
      pbBtn.disabled = true;
      try {
        await apiPost("/api/config/dms/post-panel", { channel_id: channelId });
        showStatus(pbStatus, true, "Panel posted");
        setTimeout(() => mount(container), 1500);
      } catch (err) {
        showStatus(pbStatus, false, err.message);
      } finally {
        pbBtn.disabled = false;
      }
    });
  })();
}
