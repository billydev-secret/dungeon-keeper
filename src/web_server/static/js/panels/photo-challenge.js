import { api, apiPost, esc, fmtTs } from "../api.js";
import {
  apiPut, apiDelete, showStatus, guardForm,
  loadChannels, loadRoles, channelSelect, roleSelect,
} from "../config-helpers.js";
import { renderLoading, renderEmpty, renderError } from "../states.js";
import { toast, confirmDialog } from "../ui.js";
import { mountGamePanel } from "./games-panel-shared.js";

// Photo Challenge — standalone feature (config + own schedule + prompt bank).
// Pulled out of the Games menu/scheduler: one dedicated channel, its own
// recurring schedule, a ping role, an enabled toggle. Backed by
// /api/photo-challenge; the prompt bank reuses the shared game bank
// (game_type='photo') via mountGamePanel.
// All user-supplied content rendered via innerHTML uses esc() for XSS safety.

const WEEKDAYS = [
  ["Mon", "0"], ["Tue", "1"], ["Wed", "2"], ["Thu", "3"],
  ["Fri", "4"], ["Sat", "5"], ["Sun", "6"],
];

const STATUS_LABEL = {
  launched: "✅ Posted", skipped_active: "⏭️ Skipped — channel was busy",
  skipped_disabled: "🚫 Skipped — Photo Challenge is off",
  skipped_giveup: "⌛ Gave up — channel stayed busy",
  error: "⚠️ Failed", launching: "▶️ Posting now",
};

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
    const names = (row.recur_days || []).map((d) => (WEEKDAYS[d] ? WEEKDAYS[d][0] : d));
    return `Weekly · ${names.join(", ")}`;
  }
  return row.recurrence;
}

export function mount(container) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>📸 Photo Challenge</h2>
        <div class="subtitle">Auto-posts a challenge card on a schedule, in its own channel. Members post photos there.</div>
      </header>

      <section>
        <div class="section-label">Setup</div>
        <div style="display:flex;flex-wrap:wrap;gap:12px;">
          <div class="field" style="flex:1;min-width:220px;">
            <label>Channel
              <select class="w-full" data-ctrl="channel"></select>
            </label>
            <div class="field-hint">The channel every challenge card posts in.</div>
          </div>
          <div class="field" style="flex:1;min-width:220px;">
            <label>Ping Role
              <select class="w-full" data-ctrl="role"></select>
            </label>
            <div class="field-hint">Mentioned every time a card posts. Leave on (none) to post without a ping.</div>
          </div>
        </div>
        <div class="field-hint" style="margin-bottom:8px;">Posting a photo here pays out on its own — add an active <strong>photo_post</strong> quest under Economy → Quests to set the reward.</div>
        <div class="field m-0">
          <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-weight:600;">
            <input type="checkbox" data-ctrl="enabled" style="width:18px;height:18px;cursor:pointer;" />
            <span>Post Challenges on Schedule</span>
          </label>
          <div class="field-hint">When off, every schedule below is skipped — nothing posts, and nothing is deleted.</div>
        </div>
        <div style="display:flex;align-items:center;gap:8px;margin-top:8px;">
          <button class="btn btn-primary" data-action="save-config">Save Setup</button>
          <span data-status="config" class="save-status"></span>
        </div>
      </section>

      <section>
        <div class="section-label" data-region="form-title">Add Schedule</div>
        <div class="form" style="max-width:none;">
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
          <div style="display:flex;gap:8px;align-items:center;margin-top:8px;">
            <button class="btn btn-primary" data-action="save-schedule">Create Schedule</button>
            <button class="btn" data-action="cancel-edit" style="display:none;">Cancel</button>
            <span data-status="schedule" class="save-status"></span>
          </div>
        </div>
        <div class="section-label" style="margin-top:16px;">Schedules</div>
        <div data-region="list">${renderLoading("Loading schedules…")}</div>
      </section>

      <div data-region="bank"></div>
    </div>
  `;

  const state = { editingId: null };

  // Weekday checkboxes
  container.querySelector('[data-region="weekdays"]').innerHTML = WEEKDAYS
    .map(([label, val]) =>
      `<label style="display:flex;align-items:center;gap:4px;cursor:pointer;font-weight:normal;">
        <input type="checkbox" data-weekday="${val}" /> ${esc(label)}
      </label>`)
    .join("");

  // Prompt bank (shared game bank, game_type='photo'; no status/options section).
  mountGamePanel(container.querySelector('[data-region="bank"]'), {
    gameType: "photo", gameName: "Prompt Bank", gameIcon: "📸",
    hasBank: true, hasStatus: false,
    bankHint: "Challenges posted with each card. When a schedule fires it pulls a random prompt from here.",
  });

  initConfig(container).catch((e) => {
    showStatus(
      container.querySelector('[data-status="config"]'),
      false,
      `Couldn’t load the setup — reload before saving. (${e.message})`,
    );
  });
  wireSchedule(container, state);
  refreshList(container, state).catch((e) => {
    container.querySelector('[data-region="list"]').innerHTML =
      renderError(`Couldn’t load your schedules — try again. (${e.message})`);
  });
}

// ── Setup (config) ───────────────────────────────────────────────────────────

async function initConfig(root) {
  const [channels, roles, cfg] = await Promise.all([
    loadChannels(), loadRoles(), api("/api/photo-challenge/config"),
  ]);
  root.querySelector('[data-ctrl="channel"]').innerHTML = channelSelect(channels, null, { allowNone: true });
  root.querySelector('[data-ctrl="role"]').innerHTML = roleSelect(roles, null, { allowNone: true });
  root.querySelector('[data-ctrl="channel"]').value = cfg.channel_id ? String(cfg.channel_id) : "0";
  root.querySelector('[data-ctrl="role"]').value = cfg.ping_role_id ? String(cfg.ping_role_id) : "0";
  root.querySelector('[data-ctrl="enabled"]').checked = cfg.enabled !== false;

  root.querySelector('[data-action="save-config"]').addEventListener("click", async () => {
    const st = root.querySelector('[data-status="config"]');
    const channel = root.querySelector('[data-ctrl="channel"]').value;
    const role = root.querySelector('[data-ctrl="role"]').value;
    try {
      await apiPut("/api/photo-challenge/config", {
        channel_id: channel === "0" ? "" : channel,
        ping_role_id: role === "0" ? "" : role,
        enabled: root.querySelector('[data-ctrl="enabled"]').checked,
      });
      showStatus(st, true, "Saved");
    } catch (e) {
      showStatus(st, false, `Couldn’t save — ${e.message}`);
    }
  });
  guardForm(root.querySelector("section"));
}

// ── Schedule editor ──────────────────────────────────────────────────────────

function updateConditionalFields(root) {
  const rec = root.querySelector('[data-ctrl="recurrence"]').value;
  root.querySelector('[data-region="date-field"]').style.display = rec === "once" ? "" : "none";
  root.querySelector('[data-region="weekday-field"]').style.display = rec === "weekly" ? "" : "none";
}

function wireSchedule(root, state) {
  root.querySelector('[data-ctrl="recurrence"]').addEventListener("change", () => updateConditionalFields(root));
  root.querySelector('[data-action="save-schedule"]').addEventListener("click", () => saveSchedule(root, state));
  root.querySelector('[data-action="cancel-edit"]').addEventListener("click", () => resetForm(root, state));
  updateConditionalFields(root);
}

function readForm(root) {
  const rec = root.querySelector('[data-ctrl="recurrence"]').value;
  const body = { recurrence: rec, time: root.querySelector('[data-ctrl="time"]').value };
  if (rec === "once") body.start_date = root.querySelector('[data-ctrl="date"]').value || null;
  if (rec === "weekly") {
    body.recur_days = Array.from(root.querySelectorAll('[data-weekday]:checked'))
      .map((el) => parseInt(el.getAttribute("data-weekday"), 10));
  }
  return body;
}

async function saveSchedule(root, state) {
  const status = root.querySelector('[data-status="schedule"]');
  const body = readForm(root);
  if (!body.time) { showStatus(status, false, "Pick a time of day first."); return; }
  try {
    if (state.editingId !== null) {
      await apiPut(`/api/photo-challenge/schedule/${state.editingId}`, body);
    } else {
      await apiPost("/api/photo-challenge/schedule", body);
    }
    showStatus(status, true, "Saved");
    resetForm(root, state);
    await refreshList(root, state);
  } catch (e) {
    showStatus(status, false, `Couldn’t save that schedule — ${e.message}`);
  }
}

function resetForm(root, state) {
  state.editingId = null;
  root.querySelector('[data-region="form-title"]').textContent = "Add Schedule";
  root.querySelector('[data-action="save-schedule"]').textContent = "Create Schedule";
  root.querySelector('[data-action="cancel-edit"]').style.display = "none";
  root.querySelector('[data-ctrl="recurrence"]').value = "once";
  root.querySelector('[data-ctrl="time"]').value = "20:00";
  root.querySelector('[data-ctrl="date"]').value = "";
  root.querySelectorAll('[data-weekday]').forEach((el) => { el.checked = false; });
  updateConditionalFields(root);
}

function startEdit(root, state, row) {
  state.editingId = row.id;
  root.querySelector('[data-region="form-title"]').textContent = `Editing #${row.id}`;
  root.querySelector('[data-action="save-schedule"]').textContent = "Save Changes";
  root.querySelector('[data-action="cancel-edit"]').style.display = "";
  root.querySelector('[data-ctrl="recurrence"]').value = row.recurrence;
  root.querySelector('[data-ctrl="time"]').value = fmtTime(row.time_of_day);
  root.querySelector('[data-ctrl="date"]').value = row.start_date || "";
  const days = new Set((row.recur_days || []).map(String));
  root.querySelectorAll('[data-weekday]').forEach((el) => {
    el.checked = days.has(el.getAttribute("data-weekday"));
  });
  updateConditionalFields(root);
  root.querySelector(".panel").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function refreshList(root, state) {
  const region = root.querySelector('[data-region="list"]');
  let rows;
  try {
    rows = await api("/api/photo-challenge/schedule");
  } catch (e) {
    region.innerHTML = renderError(`Couldn’t load your schedules — try again. (${e.message})`);
    return;
  }
  if (!rows.length) {
    region.innerHTML = renderEmpty(
      "No schedules yet. Use the form above to pick a day and time — Dungeon Keeper "
      + "will post a challenge card in your chosen channel each time it comes around.",
    );
    return;
  }
  region.innerHTML = rows.map((r) => {
    const paused = r.status === "paused";
    const done = r.status === "done" || r.status === "cancelled";
    const last = r.last_status ? ` · Last run: ${esc(STATUS_LABEL[r.last_status] || r.last_status)}` : "";
    const statusTag = paused ? '<span class="tag">Paused</span>'
      : done ? `<span class="tag">${esc(r.status)}</span>` : "";
    return `
      <div class="card" data-id="${r.id}" style="display:flex;justify-content:space-between;align-items:center;gap:12px;padding:10px 12px;margin-bottom:8px;border:1px solid var(--border,#333);border-radius:8px;">
        <div style="min-width:0;">
          <div style="font-weight:600;">${esc(recurrenceLabel(r))} · ${esc(fmtTime(r.time_of_day))} ${statusTag}</div>
          <div class="field-hint" style="margin:2px 0 0;">
            ${done ? "" : `Next post: ${esc(fmtNextRun(r.next_run_at))}`}${last}
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
    btn.addEventListener("click", () => handleRowAction(root, state, btn, rows));
  });
}

async function handleRowAction(root, state, btn, rows) {
  const id = btn.getAttribute("data-id");
  const act = btn.getAttribute("data-act");
  try {
    if (act === "edit") {
      const row = rows.find((r) => String(r.id) === String(id));
      if (row) startEdit(root, state, row);
      return;
    }
    if (act === "delete") {
      const ok = await confirmDialog(
        "Delete this schedule? Cards already posted stay where they are.",
        { title: "Delete Schedule", danger: true, confirmLabel: "Delete" },
      );
      if (!ok) return;
      await apiDelete(`/api/photo-challenge/schedule/${id}`);
    } else if (act === "pause") {
      await apiPost(`/api/photo-challenge/schedule/${id}/pause`, {});
    } else if (act === "resume") {
      await apiPost(`/api/photo-challenge/schedule/${id}/resume`, {});
    } else if (act === "run-now") {
      await apiPost(`/api/photo-challenge/schedule/${id}/run-now`, {});
    }
    await refreshList(root, state);
  } catch (e) {
    toast(`Couldn’t ${act === "delete" ? "delete that schedule" : "update that schedule"} — ${e.message}`, "error");
  }
}
