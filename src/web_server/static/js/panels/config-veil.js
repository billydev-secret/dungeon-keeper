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

function mkNum(name, min, value) {
  const i = document.createElement("input");
  i.type = "number";
  i.name = name;
  i.min = String(min);
  i.value = String(value);
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
    const v = config.guess;

    container.textContent = "";
    const panel = document.createElement("div");
    panel.className = "panel";

    const hdr = document.createElement("header");
    const h2 = document.createElement("h2");
    h2.textContent = "Veil";
    const sub = document.createElement("div");
    sub.className = "subtitle";
    sub.textContent = "NSFW guessing game settings";
    hdr.append(h2, sub);
    panel.appendChild(hdr);

    const form = document.createElement("form");
    form.className = "form";
    panel.appendChild(form);

    // Game Channel
    const chSel = mkSel("channel_id");
    chSel.appendChild(mkOpt("0", "(disabled)", v.channel_id === "0" || !v.channel_id));
    for (const ch of channels) {
      chSel.appendChild(mkOpt(ch.id, "#" + ch.name, ch.id === v.channel_id));
    }
    form.appendChild(mkField("Game Channel", chSel, "Channel where rounds are posted. Required for the game to work."));

    // Required Role
    const roleSel = mkSel("role_id");
    roleSel.appendChild(mkOpt("0", "(none)", v.role_id === "0" || !v.role_id));
    for (const r of roles) {
      roleSel.appendChild(mkOpt(r.id, "@" + r.name, r.id === v.role_id));
    }
    form.appendChild(mkField("Required Role", roleSel, "Role required to submit images. \"(none)\" allows everyone."));

    // Crop Difficulty
    const diffSel = mkSel("crop_difficulty");
    for (const d of ["easy", "medium", "hard"]) {
      diffSel.appendChild(mkOpt(d, d.charAt(0).toUpperCase() + d.slice(1), d === v.crop_difficulty));
    }
    form.appendChild(mkField("Crop Difficulty", diffSel, "How tightly the crop frames the detected region."));

    form.appendChild(mkField(
      "Guess Cooldown (seconds)",
      mkNum("guess_cooldown_seconds", 0, v.guess_cooldown_seconds),
      "Per-user cooldown between guesses.",
    ));
    form.appendChild(mkField("Min Image Dimension (px)", mkNum("min_image_dimension_px", 1, v.min_image_dimension_px)));
    form.appendChild(mkField("Max Image Size (MB)", mkNum("max_image_size_mb", 1, v.max_image_size_mb)));

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
        await apiPut("/api/config/guess", {
          channel_id: fd.get("channel_id"),
          role_id: fd.get("role_id"),
          crop_difficulty: fd.get("crop_difficulty"),
          guess_cooldown_seconds: parseInt(fd.get("guess_cooldown_seconds")) || 0,
          min_image_dimension_px: parseInt(fd.get("min_image_dimension_px")) || 1,
          max_image_size_mb: parseInt(fd.get("max_image_size_mb")) || 1,
        });
        showStatus(statusEl, true);
      } catch (err) {
        showStatus(statusEl, false, err.message);
      }
    });

    container.appendChild(panel);
  })();
}
