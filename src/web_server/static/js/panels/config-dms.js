import { apiPost } from "../api.js";
import {
  loadConfig, loadChannels, loadRoles, apiPut, showStatus, buildField,
  mountRolePicker, mountChannelPicker, guardForm, renderMetaWarning,
} from "../config-helpers.js";

let _fieldSeq = 0;

// buildField renders a bare <label>; tie it to its control by id so screen
// readers announce the label and a label tap focuses the field (W-A7).
function field(labelText, control, hint) {
  const div = buildField(labelText, control, hint);
  if (control instanceof HTMLElement && /^(INPUT|SELECT|TEXTAREA)$/.test(control.tagName)) {
    const id = control.id || `dm-field-${++_fieldSeq}`;
    control.id = id;
    div.querySelector("label").htmlFor = id;
  }
  return div;
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
  while (container.firstChild) container.removeChild(container.firstChild);

  const loadingPanel = document.createElement("div");
  loadingPanel.className = "panel";
  const loadingMsg = document.createElement("div");
  loadingMsg.className = "empty";
  loadingMsg.textContent = "Loading DM permission settings…";
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
      errMsg.textContent = "Couldn't load these settings: " + err.message;
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
    sub.textContent = "Let members say who may message them privately, and give moderators a place to review requests";
    header.appendChild(h2);
    header.appendChild(sub);
    panel.appendChild(header);

    const warning = renderMetaWarning();
    if (warning) {
      const w = document.createElement("div");
      w.innerHTML = warning;
      panel.appendChild(w.firstElementChild);
    }

    // ── Channel settings form ────────────────────────────────────────
    const form = document.createElement("form");
    form.className = "form form-cards";
    panel.appendChild(form);

    const channelCard = card(form, "Channels");

    const reqSlot = document.createElement("span");
    channelCard.appendChild(field(
      "Request Channel",
      reqSlot,
      "Where a pending DM request is posted so your moderators can approve or decline it. Choose \"(none)\" and requests go nowhere, so nobody can approve them.",
    ));
    // Snowflakes stay strings; "0" is the saved value meaning "not set".
    const reqPicker = mountChannelPicker(
      reqSlot, channels, String(d.request_channel_id || "0"),
      { emptyValue: "0", emptyLabel: "(none)", label: "Request Channel" },
    );

    const auditSlot = document.createElement("span");
    channelCard.appendChild(field(
      "Audit Channel",
      auditSlot,
      "A private record of every DM permission change — who asked, who approved, who blocked whom. Choose \"(none)\" to keep no record.",
    ));
    const auditPicker = mountChannelPicker(
      auditSlot, channels, String(d.audit_channel_id || "0"),
      { emptyValue: "0", emptyLabel: "(none)", label: "Audit Channel" },
    );

    // ── Status role overrides ────────────────────────────────────────
    const rolesCard = card(form, "Status Roles");

    const rolesHint = document.createElement("div");
    rolesHint.className = "field-hint";
    rolesHint.style.marginBottom = "10px";
    rolesHint.textContent =
      "Members carry one of three statuses, each shown as a role. Point each status at a "
      + "role you already have, or leave it as \"(none)\" and Dungeon Keeper uses its own "
      + "role for that status (“DMs: Open”, “DMs: Ask”, or “DMs: Closed”), creating it "
      + "the first time it's needed.";
    rolesCard.appendChild(rolesHint);

    const roleDefs = [
      ["open", "“Open” Role", "Holders accept direct messages from anyone, with no request needed."],
      ["ask", "“Ask First” Role", "Holders want a request approved before anyone may message them. This is what new members start on."],
      ["closed", "“Closed” Role", "Holders are not accepting DM requests at all; the request button is refused for them."],
    ];
    const rolePickers = {};
    for (const [mode, label, hint] of roleDefs) {
      const slot = document.createElement("span");
      rolesCard.appendChild(field(label, slot, hint));
      rolePickers[mode] = mountRolePicker(slot, roles, d[mode + "_role_id"] || "0", { label });
    }

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
      try {
        await apiPut("/api/config/dms", {
          request_channel_id: reqPicker.getValue() || "0",
          audit_channel_id: auditPicker.getValue() || "0",
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
    const pbForm = document.createElement("form");
    pbForm.className = "form form-cards";
    panel.appendChild(pbForm);

    const pbCard = card(pbForm, "Member Request Panel");

    if (d.panel_channel_id && d.panel_channel_id !== "0") {
      const curHint = document.createElement("div");
      curHint.className = "field-hint";
      curHint.style.marginBottom = "12px";
      const ch = channels.find((x) => x.id === d.panel_channel_id);
      curHint.textContent = "The panel is posted in "
        + (ch ? "#" + ch.name : "channel " + d.panel_channel_id) + " right now.";
      pbCard.appendChild(curHint);
    }

    const pbSlot = document.createElement("span");
    pbCard.appendChild(field(
      "Post the Panel In",
      pbSlot,
      "Posts the button members press to set their own DM status or ask someone for permission. Posting it again moves it: the old panel stops working.",
    ));
    const pbPicker = mountChannelPicker(
      pbSlot, channels, String(d.panel_channel_id || "0"),
      { emptyValue: "0", emptyLabel: "(pick a channel)", label: "Post the Panel In" },
    );

    const pbRow = document.createElement("div");
    pbRow.style.cssText = "display:flex; gap:8px; align-items:center;";
    const pbBtn = document.createElement("button");
    pbBtn.type = "submit";
    pbBtn.className = "btn btn-primary";
    pbBtn.textContent = "Post Panel";
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
