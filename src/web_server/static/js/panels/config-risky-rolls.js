import { loadConfig, loadRoles, apiPut, showStatus, buildField } from "../config-helpers.js";

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
    const [config, roles] = await Promise.all([loadConfig(), loadRoles()]);
    const r = config.risky || {};

    container.textContent = "";
    const panel = document.createElement("div");
    panel.className = "panel";

    const hdr = document.createElement("header");
    const h2 = document.createElement("h2");
    h2.textContent = "Risky Roller";
    const sub = document.createElement("div");
    sub.className = "subtitle";
    sub.textContent = "Dice game settings";
    hdr.append(h2, sub);
    panel.appendChild(hdr);

    const form = document.createElement("form");
    form.className = "form";
    panel.appendChild(form);

    // Ping role
    const roleSel = mkSel("ping_role_id");
    roleSel.appendChild(mkOpt("0", "(none)", r.ping_role_id === "0" || !r.ping_role_id));
    for (const role of roles) {
      roleSel.appendChild(mkOpt(role.id, "@" + role.name, role.id === r.ping_role_id));
    }
    form.appendChild(buildField(
      "Ping Role",
      roleSel,
      "Role mentioned when a new round starts via /risky start. \"(none)\" disables the ping.",
    ));

    // Min game time — stored as seconds, edited as minutes
    const minSecs = r.min_game_seconds || 0;
    const minInp = document.createElement("input");
    minInp.type = "number";
    minInp.name = "min_game_minutes";
    minInp.min = "0";
    minInp.step = "1";
    minInp.value = String(Math.round(minSecs / 60));
    form.appendChild(buildField(
      "Minimum Round Duration (minutes)",
      minInp,
      "How long a round must be open before it can be closed early. 0 disables the minimum.",
    ));

    // Max concurrent games per channel
    const maxGamesInp = document.createElement("input");
    maxGamesInp.type = "number";
    maxGamesInp.name = "max_games_per_channel";
    maxGamesInp.min = "1";
    maxGamesInp.step = "1";
    maxGamesInp.value = String(r.max_games_per_channel || 10);
    form.appendChild(buildField(
      "Max Concurrent Games Per Channel",
      maxGamesInp,
      "How many open rounds a single channel can have at once before /risky start is refused.",
    ));

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
      const mins = parseInt(fd.get("min_game_minutes"), 10);
      if (!Number.isFinite(mins) || mins < 0) {
        showStatus(statusEl, false, "Duration must be 0 or more");
        return;
      }
      const maxGames = parseInt(fd.get("max_games_per_channel"), 10);
      if (!Number.isFinite(maxGames) || maxGames < 1) {
        showStatus(statusEl, false, "Max concurrent games must be at least 1");
        return;
      }
      try {
        await apiPut("/api/config/risky", {
          ping_role_id: fd.get("ping_role_id"),
          min_game_seconds: mins * 60,
          max_games_per_channel: maxGames,
        });
        showStatus(statusEl, true);
      } catch (err) {
        showStatus(statusEl, false, err.message);
      }
    });

    container.appendChild(panel);
  })();
}
