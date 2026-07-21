import { loadConfig, loadChannels, loadRoles, apiPut, showStatus } from "../config-helpers.js";

function mkSel(name) {
  const s = document.createElement("select");
  s.name = name;
  return s;
}

function mkOpt(value, text, selected) {
  const o = document.createElement("option");
  o.value = value;
  o.textContent = text;
  o.selected = !!selected;
  return o;
}

function mkNum(name, value, min) {
  const i = document.createElement("input");
  i.type = "number";
  i.name = name;
  i.value = value;
  i.min = String(min);
  i.style.maxWidth = "90px";
  return i;
}

function mkField(labelText, ctrl, hint) {
  const d = document.createElement("div");
  d.className = "field";
  const l = document.createElement("label");
  l.textContent = labelText;
  d.appendChild(l);
  d.appendChild(ctrl);
  if (hint) {
    const h = document.createElement("div");
    h.className = "field-hint";
    h.textContent = hint;
    d.appendChild(h);
  }
  return d;
}

export function mount(container) {
  container.textContent = "";
  const wrap = document.createElement("div");
  wrap.className = "panel";
  const loading = document.createElement("div");
  loading.className = "empty";
  loading.textContent = "Loading config…";
  wrap.appendChild(loading);
  container.appendChild(wrap);

  (async () => {
    const [config, channels, roles] = await Promise.all([
      loadConfig(),
      loadChannels(),
      loadRoles(),
    ]);
    const w = config.whisper;

    container.textContent = "";
    const panel = document.createElement("div");
    panel.className = "panel";

    const hdr = document.createElement("header");
    const h2 = document.createElement("h2");
    h2.textContent = "Whisper";
    const sub = document.createElement("div");
    sub.className = "subtitle";
    sub.textContent = "Anonymous whisper relay settings";
    hdr.append(h2, sub);
    panel.appendChild(hdr);

    const form = document.createElement("form");
    form.className = "form";
    panel.appendChild(form);

    // Whisper Channel
    const chSel = mkSel("channel_id");
    chSel.appendChild(mkOpt("0", "(disabled)", w.channel_id === "0" || !w.channel_id));
    for (const ch of channels) {
      chSel.appendChild(mkOpt(ch.id, "#" + ch.name, ch.id === w.channel_id));
    }
    form.appendChild(mkField("Whisper Channel", chSel, "Channel where whispers are posted. Required for the feature to work."));

    // Required Role
    const roleSel = mkSel("role_id");
    roleSel.appendChild(mkOpt("0", "(none)", w.role_id === "0" || !w.role_id));
    for (const r of roles) {
      roleSel.appendChild(mkOpt(r.id, "@" + r.name, r.id === w.role_id));
    }
    form.appendChild(mkField("Required Role", roleSel, "Role required to send whispers. \"(none)\" allows everyone."));

    // Log Channel
    const logSel = mkSel("log_channel_id");
    logSel.appendChild(mkOpt("0", "(disabled)", w.log_channel_id === "0" || !w.log_channel_id));
    for (const ch of channels) {
      logSel.appendChild(mkOpt(ch.id, "#" + ch.name, ch.id === w.log_channel_id));
    }
    form.appendChild(mkField("Log Channel", logSel, "Moderator-visible audit log of whisper authors. \"(disabled)\" turns logging off."));

    // Rate limits
    const cooldownInput = mkNum("cooldown_seconds", w.cooldown_seconds ?? 30, 0);
    form.appendChild(mkField("Send Cooldown (seconds)", cooldownInput, "How long a member must wait between sending whispers. 0 disables the cooldown."));

    const hourlyCapInput = mkNum("hourly_cap_per_target", w.hourly_cap_per_target ?? 5, 1);
    form.appendChild(mkField("Hourly Cap Per Recipient", hourlyCapInput, "Max whispers a member can send to the same recipient within an hour."));

    const row = document.createElement("div");
    const saveBtn = document.createElement("button");
    saveBtn.type = "submit";
    saveBtn.className = "btn btn-primary";
    saveBtn.textContent = "Save";
    const statusEl = document.createElement("span");
    row.append(saveBtn, statusEl);
    form.appendChild(row);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      try {
        await apiPut("/api/config/whisper", {
          channel_id: fd.get("channel_id"),
          role_id: fd.get("role_id"),
          log_channel_id: fd.get("log_channel_id"),
          cooldown_seconds: Number(fd.get("cooldown_seconds")),
          hourly_cap_per_target: Number(fd.get("hourly_cap_per_target")),
        });
        showStatus(statusEl, true);
      } catch (err) {
        showStatus(statusEl, false, err.message);
      }
    });

    container.appendChild(panel);
  })();
}
