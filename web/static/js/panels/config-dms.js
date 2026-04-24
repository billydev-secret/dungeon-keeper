import { loadConfig, loadChannels, apiPut, showStatus, buildField } from "../config-helpers.js";

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
    let config, channels;
    try {
      [config, channels] = await Promise.all([loadConfig(), loadChannels()]);
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
    panel.style.overflowY = "auto";
    container.appendChild(panel);

    const header = document.createElement("header");
    const h2 = document.createElement("h2");
    h2.textContent = "DM Permissions";
    const sub = document.createElement("div");
    sub.className = "subtitle";
    sub.textContent = "DM request system channel settings";
    header.appendChild(h2);
    header.appendChild(sub);
    panel.appendChild(header);

    if (channels.length === 0) {
      const warn = document.createElement("div");
      warn.className = "field-hint";
      warn.style.cssText = "margin-bottom:12px; color:var(--text-dim);";
      warn.textContent = "Channel list unavailable — enter channel IDs directly, or restart the bot and refresh.";
      panel.appendChild(warn);
    }

    // ── Channel settings form ────────────────────────────────────────
    const form = document.createElement("form");
    form.className = "config-form";
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

    const saveRow = document.createElement("div");
    const saveBtn = document.createElement("button");
    saveBtn.type = "submit";
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
        });
        showStatus(saveStatus, true);
      } catch (err) {
        showStatus(saveStatus, false, err.message);
      }
    });

    // ── Panel button launcher ────────────────────────────────────────
    const hr = document.createElement("hr");
    hr.style.cssText = "margin:24px 0; border-color:var(--grid);";
    panel.appendChild(hr);

    const lsec = document.createElement("section");
    panel.appendChild(lsec);

    const lh3 = document.createElement("h3");
    lh3.style.cssText = "margin:0 0 6px; font-size:15px;";
    lh3.textContent = "Request Panel";
    lsec.appendChild(lh3);

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
    pbForm.className = "config-form";
    pbForm.style.cssText = "display:flex; gap:8px; align-items:flex-end;";
    lsec.appendChild(pbForm);

    const pbField = document.createElement("div");
    pbField.className = "field";
    pbField.style.cssText = "margin:0; flex:1;";
    const pbLabel = document.createElement("label");
    pbLabel.textContent = "Channel";
    const pbCtrl = buildChannelInput("pb_channel_id", channels, d.panel_channel_id || "0");
    pbField.appendChild(pbLabel);
    pbField.appendChild(pbCtrl);
    pbForm.appendChild(pbField);

    const pbRow = document.createElement("div");
    const pbBtn = document.createElement("button");
    pbBtn.type = "submit";
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
        const res = await fetch("/api/config/dms/post-panel", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ channel_id: channelId }),
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || res.statusText);
        }
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
