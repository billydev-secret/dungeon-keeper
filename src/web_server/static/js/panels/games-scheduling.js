import { api, apiPost, esc, fmtTs } from "../api.js";
import {
  apiPut, apiDelete, showStatus, guardForm,
  loadChannels, loadRoles, channelSelect, roleSelect,
} from "../config-helpers.js";
import { renderLoading, renderEmpty, renderError } from "../states.js";
import { toast, confirmDialog } from "../ui.js";

// All user-supplied content rendered via innerHTML uses esc() for XSS safety.

const WEEKDAYS = [
  ["1", "Mon", "0"], ["2", "Tue", "1"], ["3", "Wed", "2"], ["4", "Thu", "3"],
  ["5", "Fri", "4"], ["6", "Sat", "5"], ["7", "Sun", "6"],
];

let _options = null;   // { games: [{type,name,icon,fields}] }
let _channels = [];
let _roles = [];
let _editingId = null;  // null = create mode

function fmtTime(min) {
  const h = String(Math.floor(min / 60)).padStart(2, "0");
  const m = String(min % 60).padStart(2, "0");
  return `${h}:${m}`;
}

function fmtNextRun(ts) {
  return fmtTs(ts);
}

function recurrenceLabel(row) {
  if (row.recurrence === "once") return `Once · ${row.start_date || "no date set"}`;
  if (row.recurrence === "daily") return "Daily";
  if (row.recurrence === "weekly") {
    const names = (row.recur_days || []).map((d) => WEEKDAYS[d] ? WEEKDAYS[d][1] : d);
    return `Weekly · ${names.join(", ")}`;
  }
  return row.recurrence;
}

export function mount(container) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Game Scheduling</h2>
        <div class="subtitle">Launch party games on their own, at times you pick. Every time below is server time.</div>
      </header>

      <section>
        <div class="section-label" data-region="form-title">New Schedule</div>
        <div class="form" style="max-width:none;">
          <div style="display:flex;flex-wrap:wrap;gap:12px;">
            <div class="field" style="flex:1;min-width:200px;">
              <label>Game
                <select class="w-full" data-ctrl="game"></select>
              </label>
            </div>
            <div class="field" style="flex:1;min-width:200px;">
              <label>Channel
                <select class="w-full" data-ctrl="channel"></select>
              </label>
            </div>
          </div>

          <div data-region="game-options" style="margin:4px 0;"></div>

          <div style="display:flex;flex-wrap:wrap;gap:12px;">
            <div class="field" style="flex:1;min-width:160px;">
              <label>Repeat
                <select class="w-full" data-ctrl="recurrence">
                  <option value="once">Once</option>
                  <option value="daily">Daily</option>
                  <option value="weekly">Weekly</option>
                </select>
              </label>
            </div>
            <div class="field" style="flex:1;min-width:140px;">
              <label>Time of Day (Server Time)
                <input class="w-full" type="time" data-ctrl="time" value="20:00" />
              </label>
            </div>
            <div class="field" data-region="date-field" style="flex:1;min-width:160px;display:none;">
              <label>Date
                <input class="w-full" type="date" data-ctrl="date" />
              </label>
            </div>
          </div>

          <fieldset class="field" data-region="weekday-field"
                    style="display:none;border:0;padding:0;margin:0;min-inline-size:0;">
            <legend style="font-size:12px;font-weight:600;color:var(--ink-dim);padding:0;">On These Days</legend>
            <div data-region="weekdays" style="display:flex;flex-wrap:wrap;gap:10px;margin-top:4px;"></div>
          </fieldset>

          <div style="display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end;">
            <div class="field m-0">
              <label style="display:flex;align-items:center;gap:6px;cursor:pointer;">
                <input type="checkbox" data-ctrl="announce" /> Announce Before Launch
              </label>
              <div class="field-hint">Posts a heads-up in the channel a few minutes ahead, so people can gather.</div>
            </div>
            <div class="field" data-region="role-field" style="flex:1;min-width:200px;display:none;">
              <label>Ping Role (Optional)
                <select class="w-full" data-ctrl="role"></select>
              </label>
            </div>
          </div>

          <div style="display:flex;gap:8px;align-items:center;margin-top:8px;">
            <button class="btn btn-primary" data-action="save">Create Schedule</button>
            <button class="btn" data-action="cancel-edit" style="display:none;">Cancel</button>
            <span data-status="save" class="save-status"></span>
          </div>
        </div>
      </section>

      <section>
        <div class="section-label">Scheduled Games</div>
        <div data-region="list">${renderLoading("Loading schedules…")}</div>
      </section>
    </div>
  `;

  _editingId = null;
  init(container).catch((e) => {
    container.querySelector('[data-region="list"]').innerHTML =
      renderError(`Couldn’t load game scheduling — try again. (${e.message})`);
  });
}

async function init(root) {
  [_options, _channels, _roles] = await Promise.all([
    api("/api/games/schedule/options"),
    loadChannels(),
    loadRoles(),
  ]);

  // Game select
  const gameSel = root.querySelector('[data-ctrl="game"]');
  gameSel.innerHTML = _options.games
    .map((g) => `<option value="${esc(g.type)}">${esc(g.icon)} ${esc(g.name)}</option>`)
    .join("");

  // Channel + role selects
  root.querySelector('[data-ctrl="channel"]').innerHTML =
    channelSelect(_channels, null, { allowNone: false });
  root.querySelector('[data-ctrl="role"]').innerHTML =
    roleSelect(_roles, null, { allowNone: true });

  // Weekday checkboxes
  root.querySelector('[data-region="weekdays"]').innerHTML = WEEKDAYS
    .map(([_n, label, val]) =>
      `<label style="display:flex;align-items:center;gap:4px;cursor:pointer;font-weight:normal;">
        <input type="checkbox" data-weekday="${val}" /> ${esc(label)}
      </label>`)
    .join("");

  renderGameOptions(root);
  wireEvents(root);
  // Warn before a sidebar click throws away a half-built schedule.
  guardForm(root.querySelector(".form"));
  await refreshList(root);
}

function renderGameOptions(root, values = {}) {
  const gameType = root.querySelector('[data-ctrl="game"]').value;
  const game = _options.games.find((g) => g.type === gameType);
  const region = root.querySelector('[data-region="game-options"]');
  const fields = (game && game.fields) || [];
  if (!fields.length) {
    region.innerHTML = "";
    return;
  }
  let html = '<div style="display:flex;flex-wrap:wrap;gap:12px;">';
  for (const f of fields) {
    const v = values[f.name] !== undefined ? values[f.name] : f.default;
    let control;
    if (f.type === "choice") {
      const opts = (f.choices || [])
        .map((c) => `<option value="${esc(c.value)}"${String(v) === String(c.value) ? " selected" : ""}>${esc(c.label)}</option>`)
        .join("");
      control = `<select class="w-full" data-opt="${esc(f.name)}" data-opt-type="choice">${opts}</select>`;
    } else if (f.type === "bool") {
      control = `<label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-weight:normal;">
        <input type="checkbox" data-opt="${esc(f.name)}" data-opt-type="bool"${v ? " checked" : ""} /> On
      </label>`;
    } else if (f.type === "int") {
      const minA = f.min !== undefined ? ` min="${f.min}"` : "";
      const maxA = f.max !== undefined ? ` max="${f.max}"` : "";
      control = `<input class="w-full" type="number"${minA}${maxA} data-opt="${esc(f.name)}" data-opt-type="int" value="${esc(String(v ?? ""))}" />`;
    } else {
      control = `<input class="w-full" type="text" data-opt="${esc(f.name)}" data-opt-type="str" value="${esc(String(v ?? ""))}" />`;
    }
    // Wrap idiom: the control lives inside its label, so a click on the text
    // focuses the field and screen readers announce the right name.
    html += `<div class="field" style="flex:1;min-width:180px;margin:0;">
      <label>${esc(f.label)} ${control}</label>
    </div>`;
  }
  html += "</div>";
  region.innerHTML = html;
}

function gatherOptions(root) {
  const out = {};
  root.querySelectorAll("[data-opt]").forEach((el) => {
    const name = el.getAttribute("data-opt");
    const type = el.getAttribute("data-opt-type");
    if (type === "bool") {
      out[name] = el.checked;
    } else if (type === "int") {
      if (el.value !== "") out[name] = parseInt(el.value, 10);
    } else {
      out[name] = el.value;
    }
  });
  return out;
}

function updateConditionalFields(root) {
  const rec = root.querySelector('[data-ctrl="recurrence"]').value;
  root.querySelector('[data-region="date-field"]').style.display = rec === "once" ? "" : "none";
  root.querySelector('[data-region="weekday-field"]').style.display = rec === "weekly" ? "" : "none";
}

function wireEvents(root) {
  root.querySelector('[data-ctrl="game"]').addEventListener("change", () => renderGameOptions(root));
  root.querySelector('[data-ctrl="recurrence"]').addEventListener("change", () => updateConditionalFields(root));
  root.querySelector('[data-ctrl="announce"]').addEventListener("change", (e) => {
    root.querySelector('[data-region="role-field"]').style.display = e.target.checked ? "" : "none";
  });
  root.querySelector('[data-action="save"]').addEventListener("click", () => save(root));
  root.querySelector('[data-action="cancel-edit"]').addEventListener("click", () => resetForm(root));
  updateConditionalFields(root);
}

function readForm(root) {
  const rec = root.querySelector('[data-ctrl="recurrence"]').value;
  const body = {
    game_type: root.querySelector('[data-ctrl="game"]').value,
    channel_id: root.querySelector('[data-ctrl="channel"]').value,
    options: gatherOptions(root),
    recurrence: rec,
    time: root.querySelector('[data-ctrl="time"]').value,
    announce: root.querySelector('[data-ctrl="announce"]').checked,
  };
  const roleVal = root.querySelector('[data-ctrl="role"]').value;
  if (body.announce && roleVal && roleVal !== "0") body.announce_role_id = roleVal;
  if (rec === "once") body.start_date = root.querySelector('[data-ctrl="date"]').value || null;
  if (rec === "weekly") {
    body.recur_days = Array.from(root.querySelectorAll('[data-weekday]:checked'))
      .map((el) => parseInt(el.getAttribute("data-weekday"), 10));
  }
  return body;
}

async function save(root) {
  const status = root.querySelector('[data-status="save"]');
  const body = readForm(root);
  if (!body.channel_id || body.channel_id === "0") {
    showStatus(status, false, "Pick a channel first.");
    return;
  }
  if (!body.time) {
    showStatus(status, false, "Pick a time of day first.");
    return;
  }
  try {
    if (_editingId !== null) {
      await apiPut(`/api/games/schedule/${_editingId}`, body);
    } else {
      await apiPost("/api/games/schedule", body);
    }
    showStatus(status, true, "Saved");
    resetForm(root);
    await refreshList(root);
  } catch (e) {
    showStatus(status, false, `Couldn’t save that schedule — ${e.message}`);
  }
}

function resetForm(root) {
  _editingId = null;
  root.querySelector('[data-region="form-title"]').textContent = "New Schedule";
  root.querySelector('[data-action="save"]').textContent = "Create Schedule";
  root.querySelector('[data-action="cancel-edit"]').style.display = "none";
  root.querySelector('[data-ctrl="recurrence"]').value = "once";
  root.querySelector('[data-ctrl="time"]').value = "20:00";
  root.querySelector('[data-ctrl="date"]').value = "";
  root.querySelector('[data-ctrl="announce"]').checked = false;
  root.querySelector('[data-region="role-field"]').style.display = "none";
  root.querySelectorAll('[data-weekday]').forEach((el) => { el.checked = false; });
  renderGameOptions(root);
  updateConditionalFields(root);
}

function startEdit(root, row) {
  _editingId = row.id;
  root.querySelector('[data-region="form-title"]').textContent = `Editing #${row.id}`;
  root.querySelector('[data-action="save"]').textContent = "Save Changes";
  root.querySelector('[data-action="cancel-edit"]').style.display = "";
  root.querySelector('[data-ctrl="game"]').value = row.game_type;
  root.querySelector('[data-ctrl="channel"]').value = String(row.channel_id);
  root.querySelector('[data-ctrl="recurrence"]').value = row.recurrence;
  root.querySelector('[data-ctrl="time"]').value = fmtTime(row.time_of_day);
  root.querySelector('[data-ctrl="date"]').value = row.start_date || "";
  root.querySelector('[data-ctrl="announce"]').checked = !!row.announce;
  root.querySelector('[data-region="role-field"]').style.display = row.announce ? "" : "none";
  root.querySelector('[data-ctrl="role"]').value = row.announce_role_id ? String(row.announce_role_id) : "0";
  const days = new Set((row.recur_days || []).map(String));
  root.querySelectorAll('[data-weekday]').forEach((el) => {
    el.checked = days.has(el.getAttribute("data-weekday"));
  });
  renderGameOptions(root, row.options || {});
  updateConditionalFields(root);
  root.querySelector(".panel").scrollIntoView({ behavior: "smooth", block: "start" });
}

const STATUS_LABEL = {
  launched: "✅ Launched", skipped_active: "⏭️ Skipped — channel was busy",
  skipped_disabled: "🚫 Skipped — game is off",
  skipped_giveup: "⌛ Gave up — channel stayed busy",
  error: "⚠️ Failed", launching: "▶️ Launching now",
};

async function refreshList(root) {
  const region = root.querySelector('[data-region="list"]');
  let rows;
  try {
    rows = await api("/api/games/schedule");
  } catch (e) {
    region.innerHTML = renderError(`Couldn’t load your schedules — try again. (${e.message})`);
    return;
  }
  if (!rows.length) {
    region.innerHTML = renderEmpty(
      "No scheduled games yet. Fill in the form above to have Dungeon Keeper start a "
      + "game on its own — handy for a standing games night.",
    );
    return;
  }
  const chName = (id) => {
    const c = _channels.find((x) => String(x.id) === String(id));
    return c ? `#${c.name}` : `#${id}`;
  };
  region.innerHTML = rows.map((r) => {
    const paused = r.status === "paused";
    const done = r.status === "done" || r.status === "cancelled";
    const last = r.last_status ? ` · Last run: ${esc(STATUS_LABEL[r.last_status] || r.last_status)}` : "";
    const statusTag = paused ? '<span class="tag">Paused</span>'
      : done ? `<span class="tag">${esc(r.status)}</span>` : "";
    return `
      <div class="card" data-id="${r.id}" style="display:flex;justify-content:space-between;align-items:center;gap:12px;padding:10px 12px;margin-bottom:8px;border:1px solid var(--border,#333);border-radius:8px;">
        <div style="min-width:0;">
          <div style="font-weight:600;">${esc(r.game_icon)} ${esc(r.game_name)} ${statusTag}</div>
          <div class="field-hint" style="margin:2px 0 0;">
            ${esc(chName(r.channel_id))} · ${esc(recurrenceLabel(r))} · ${esc(fmtTime(r.time_of_day))}
            ${r.announce ? " · 📣 Announced ahead" : ""}
          </div>
          <div class="field-hint" style="margin:2px 0 0;">
            ${done ? "" : `Next launch: ${esc(fmtNextRun(r.next_run_at))}`}${last}
          </div>
        </div>
        <div style="display:flex;gap:6px;flex-shrink:0;">
          ${done ? "" : `<button class="btn" data-act="run-now" data-id="${r.id}">Run Now</button>`}
          ${done ? "" : (paused
            ? `<button class="btn" data-act="resume" data-id="${r.id}">Resume</button>`
            : `<button class="btn" data-act="pause" data-id="${r.id}">Pause</button>`)}
          <button class="btn" data-act="edit" data-id="${r.id}">Edit</button>
          <button class="btn btn-danger" data-act="delete" data-id="${r.id}">Delete</button>
        </div>
      </div>`;
  }).join("");

  region.querySelectorAll("[data-act]").forEach((btn) => {
    btn.addEventListener("click", () => handleRowAction(root, btn, rows));
  });
}

async function handleRowAction(root, btn, rows) {
  const id = btn.getAttribute("data-id");
  const act = btn.getAttribute("data-act");
  try {
    if (act === "edit") {
      const row = rows.find((r) => String(r.id) === String(id));
      if (row) startEdit(root, row);
      return;
    }
    if (act === "delete") {
      const ok = await confirmDialog(
        "Delete this schedule? Games already running keep going.",
        { title: "Delete Schedule", danger: true, confirmLabel: "Delete" },
      );
      if (!ok) return;
      await apiDelete(`/api/games/schedule/${id}`);
    } else if (act === "pause") {
      await apiPost(`/api/games/schedule/${id}/pause`, {});
    } else if (act === "resume") {
      await apiPost(`/api/games/schedule/${id}/resume`, {});
    } else if (act === "run-now") {
      await apiPost(`/api/games/schedule/${id}/run-now`, {});
    }
    await refreshList(root);
  } catch (e) {
    toast(`Couldn’t ${act === "delete" ? "delete that schedule" : "update that schedule"} — ${e.message}`, "error");
  }
}
