// Daily feature rotation — the whole admin surface for the rotating rooms.
// One room out of the pool is open each day; the rest are hidden by denying
// view to @everyone in place (the channel keeps its category and position).
// The pool table is the feature: every row is a channel plus the handful of
// switches that decide how it behaves on the days it isn't the open one.
import { api, apiPut, apiDelete, apiPost, esc } from "../api.js";
import { clearFormDirty, loadChannels, guardForm, metaLoadFailed, mountAsync } from "../config-helpers.js";
import { renderLoading, renderEmpty, renderError } from "../states.js";
import { confirmDialog, toast } from "../ui.js";

let channels = [];
let triggerKinds = [];

function tzLabel(offset) {
  const n = Number(offset) || 0;
  if (!n) return "UTC";
  const whole = Math.trunc(Math.abs(n));
  const mins = Math.round((Math.abs(n) - whole) * 60);
  return `UTC${n < 0 ? "-" : "+"}${whole}${mins ? `:${String(mins).padStart(2, "0")}` : ""}`;
}

function chanName(id) {
  const c = channels.find((x) => String(x.id) === String(id));
  return c ? `#${c.name}` : `#${id}`;
}

function chanOptions(selected, placeholder) {
  const opts = channels
    .map((c) => `<option value="${c.id}" ${String(c.id) === String(selected) ? "selected" : ""}>#${esc(c.name)}</option>`)
    .join("");
  return `<option value="0">${esc(placeholder)}</option>${opts}`;
}

function kindLabel(kind) {
  const k = triggerKinds.find((t) => t.kind === kind);
  return k ? k.label : kind;
}

// A room's quest kinds as chips; the blocked ones carry a marker because that
// is the pair of facts an admin needs at a glance — what this room pays out
// for, and which of those stop working when its door is shut.
function kindChips(room) {
  if (!room.quest_kinds.length) {
    return `<span class="field-hint">No quests linked</span>`;
  }
  return room.quest_kinds
    .map((k) => {
      const blocked = room.blocked_kinds.includes(k);
      return `<span class="chip ${blocked ? "chip-warning" : ""}" title="${esc(kindLabel(k))}${blocked ? " — needs the room open" : ""}">${esc(k)}${blocked ? " 🔒" : ""}</span>`;
    })
    .join(" ");
}

export function mount(container) {
  container.innerHTML = `<div class="panel">${renderLoading("Loading feature rotation…")}</div>`;
  return mountAsync(container, async () => {
    channels = await loadChannels().catch(() => []);
    render(container);
    if (metaLoadFailed()) {
      toast("Couldn’t load the channel list — reload before saving.", "error");
    }
    await refresh(container);
  }, { errorMsg: "Couldn’t load the feature rotation settings." });
}

function render(container) {
  container.innerHTML = `
    <div class="panel">
      <header style="display:flex; align-items:flex-start; justify-content:space-between; gap:12px; flex-wrap:wrap;">
        <div>
          <h2>Daily Feature Rotation</h2>
          <div class="subtitle">Opens one room from the pool each day and hides the rest, then announces the day’s feature. Managed entirely here — there are no slash commands.</div>
        </div>
        <button class="btn" data-refresh>Refresh</button>
      </header>

      <section class="card">
        <div class="section-label">Settings</div>
        <div data-settings>${renderLoading("Loading settings…")}</div>
        <div data-settings-status></div>
      </section>

      <section class="card">
        <div class="section-label">Today</div>
        <div data-today>${renderLoading("Loading…")}</div>
      </section>

      <section class="card">
        <div class="section-label">The Pool</div>
        <div class="field-hint">
          Any channel can go in the pool. <strong>Hide when off</strong> is what makes a room disappear on days it isn’t featured;
          leave it unticked and the room stays visible all the time and simply takes its turn being announced.
          Quests keep paying out while a room is hidden — tick a quest as <em>needs the room open</em> only when it can’t be done
          from a slash command or a private panel, and it will be kept off the daily board that day.
        </div>
        <div data-pool>${renderLoading("Loading the pool…")}</div>
      </section>

      <section class="card">
        <div class="section-label">Add A Channel</div>
        <div data-add></div>
      </section>
    </div>`;

  container.querySelector("[data-refresh]").addEventListener("click", () => refresh(container));
}

async function refresh(container) {
  let data;
  try {
    data = await api("/api/feature-rotation");
  } catch (err) {
    container.querySelector("[data-settings]").innerHTML =
      renderError(`Couldn’t load the rotation — try again. (${err.message})`);
    container.querySelector("[data-today]").innerHTML = "";
    container.querySelector("[data-pool]").innerHTML = "";
    return;
  }
  triggerKinds = data.trigger_kinds || [];
  renderSettings(container, data);
  renderToday(container, data);
  renderPool(container, data);
  renderAdd(container, data);
}

// ── settings ─────────────────────────────────────────────────────────

function renderSettings(container, data) {
  const cfg = data.config;
  const host = container.querySelector("[data-settings]");
  host.innerHTML = `
    <div class="form-grid" style="display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:10px;">
      <label>Rotation
        <select data-f="enabled">
          <option value="1" ${cfg.enabled ? "selected" : ""}>On</option>
          <option value="0" ${cfg.enabled ? "" : "selected"}>Off</option>
        </select>
        <span class="field-hint">When off nothing is hidden and no announcement posts. Turn it off and press “Apply now” to reopen every room.</span>
      </label>
      <label>Announce In
        <select data-f="announce_channel_id">${chanOptions(cfg.announce_channel_id, "(don’t announce)")}</select>
        <span class="field-hint">This channel is never hidden, even if it’s in the pool.</span>
      </label>
      <label>Announce At (Hour, 0-23)
        <input data-f="announce_hour" type="number" min="0" max="23" value="${cfg.announce_hour}">
        <span class="field-hint">Rooms always change at midnight so the quest board matches; only the announcement waits for this hour. Times are ${esc(tzLabel(cfg.tz_offset_hours))}, set under Server Settings.</span>
      </label>
      <label>Rooms Open Per Day
        <input data-f="rooms_per_day" type="number" min="1" max="10" value="${cfg.rooms_per_day}">
        <span class="field-hint">With a bigger pool, raise this or each room only comes round rarely.</span>
      </label>
    </div>
    <div style="display:flex; gap:8px; margin-top:10px; flex-wrap:wrap;">
      <button class="btn btn-primary" data-save-settings>Save Settings</button>
      <button class="btn" data-apply-now>Apply Now</button>
    </div>
    <div class="field-hint" style="margin-top:6px;">
      “Apply now” brings Discord into line with today’s plan straight away instead of waiting for midnight.
    </div>`;

  // Guard once: renderSettings re-runs on every refresh over the same host, so
  // re-guarding would stack another listener triple each time.
  const form = host.dataset.dkGuard ? host : guardForm(host);
  host.querySelector("[data-save-settings]").addEventListener("click", async () => {
    const body = {
      enabled: host.querySelector('[data-f="enabled"]').value === "1",
      announce_channel_id: Number(host.querySelector('[data-f="announce_channel_id"]').value || 0),
      announce_hour: Number(host.querySelector('[data-f="announce_hour"]').value),
      tz_offset_hours: Number(host.querySelector('[data-f="tz_offset_hours"]').value),
      rooms_per_day: Number(host.querySelector('[data-f="rooms_per_day"]').value),
    };
    try {
      await apiPut("/api/feature-rotation/config", body);
      clearFormDirty(form);
      toast("Settings saved.", "success");
      await refresh(container);
    } catch (err) {
      toast(`Couldn’t save: ${err.message}`, "error");
    }
  });

  host.querySelector("[data-apply-now]").addEventListener("click", async () => {
    try {
      const res = await apiPost("/api/feature-rotation/apply", {});
      toast(`Applied — ${res.shown} opened, ${res.hidden} hidden.`, "success");
      await refresh(container);
    } catch (err) {
      toast(`Couldn’t apply: ${err.message}`, "error");
    }
  });
}

// ── today ────────────────────────────────────────────────────────────

function renderToday(container, data) {
  const host = container.querySelector("[data-today]");
  const t = data.today;
  if (!data.config.enabled) {
    host.innerHTML = `<div class="field-hint">The rotation is off — every room is left exactly as it is.</div>`;
    return;
  }
  if (!t.featured.length) {
    host.innerHTML = renderEmpty("No channels in the pool yet — add one below.");
    return;
  }
  const featured = t.featured.map((c) => `<strong>${esc(chanName(c))}</strong>`).join(", ");
  const hidden = t.hidden.length
    ? t.hidden.map((c) => esc(chanName(c))).join(", ")
    : "nothing";
  const tomorrow = data.tomorrow.featured.length
    ? data.tomorrow.featured.map((c) => esc(chanName(c))).join(", ")
    : "—";
  const blocked = t.blocked_quest_kinds.length
    ? `<div class="field-hint">Quests held off today’s board: ${t.blocked_quest_kinds.map((k) => `<span class="chip chip-warning">${esc(k)}</span>`).join(" ")}</div>`
    : `<div class="field-hint">Every quest still works today.</div>`;
  host.innerHTML = `
    <div>Open today (${esc(t.local_day)}): ${featured}</div>
    <div class="field-hint">Hidden: ${hidden}</div>
    <div class="field-hint">Next up: ${tomorrow}</div>
    ${blocked}`;
}

// ── the pool table ───────────────────────────────────────────────────

function roomRow(room) {
  const state = room.featured_today
    ? `<span class="chip chip-success">open today</span>`
    : room.hidden_now
      ? `<span class="chip">hidden</span>`
      : `<span class="chip">visible</span>`;
  return `
    <tr data-room="${room.channel_id}">
      <td><input type="number" min="0" max="999" value="${room.position}" data-r="position" style="width:4.5em;"></td>
      <td>
        <div>${esc(chanName(room.channel_id))}</div>
        <div>${state}</div>
      </td>
      <td><input type="text" value="${esc(room.label)}" data-r="label" placeholder="${esc(chanName(room.channel_id))}" style="width:9em;"></td>
      <td style="text-align:center;"><input type="checkbox" data-r="in_rotation" ${room.in_rotation ? "checked" : ""}></td>
      <td style="text-align:center;"><input type="checkbox" data-r="hide_when_off" ${room.hide_when_off ? "checked" : ""}></td>
      <td style="text-align:center;"><input type="checkbox" data-r="announce" ${room.announce ? "checked" : ""}></td>
      <td>${kindChips(room)}</td>
      <td style="white-space:nowrap;">
        <button class="btn btn-sm" data-edit>Quests</button>
        <button class="btn btn-sm" data-save-room>Save</button>
        <button class="btn btn-sm btn-danger" data-remove>Remove</button>
      </td>
    </tr>
    <tr data-kinds-for="${room.channel_id}" hidden>
      <td colspan="8">
        <label style="display:block; margin-bottom:8px;">Blurb (shown under the announcement)
          <input type="text" data-r="blurb" value="${esc(room.blurb)}" maxlength="300" placeholder="One line about what happens in here" style="width:100%; max-width:32em;">
        </label>
        <div class="field-hint">Tick every quest this room owns. Then tick <em>needs the room open</em> for the ones that can only be done from a message inside the channel — those are the only ones held off the board on a hidden day.</div>
        <div class="kind-grid" style="display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:4px; margin-top:6px;">
          ${triggerKinds.map((t) => {
            const owned = room.quest_kinds.includes(t.kind);
            const blocked = room.blocked_kinds.includes(t.kind);
            const ownId = `fr-k-${room.channel_id}-${t.kind}`;
            const lockId = `fr-b-${room.channel_id}-${t.kind}`;
            return `<div style="display:flex; align-items:center; gap:6px;">
              <input type="checkbox" id="${esc(ownId)}" data-kind="${esc(t.kind)}" ${owned ? "checked" : ""}>
              <label for="${esc(ownId)}" style="flex:1;">${esc(t.label)}</label>
              <input type="checkbox" id="${esc(lockId)}" data-blocked="${esc(t.kind)}" ${blocked ? "checked" : ""}>
              <label for="${esc(lockId)}" class="field-hint" style="white-space:nowrap;" title="Needs the room open">🔒</label>
            </div>`;
          }).join("")}
        </div>
      </td>
    </tr>`;
}

function readRoom(tr, kindsRow) {
  const val = (sel) => tr.querySelector(`[data-r="${sel}"]`);
  const quest_kinds = [];
  const blocked_kinds = [];
  if (kindsRow) {
    kindsRow.querySelectorAll("[data-kind]").forEach((el) => {
      if (el.checked) quest_kinds.push(el.getAttribute("data-kind"));
    });
    kindsRow.querySelectorAll("[data-blocked]").forEach((el) => {
      if (el.checked) blocked_kinds.push(el.getAttribute("data-blocked"));
    });
  }
  // The blurb input lives in the expanded row, so read it from there.
  const blurbEl = kindsRow ? kindsRow.querySelector('[data-r="blurb"]') : null;
  return {
    position: Number(val("position").value || 0),
    label: val("label").value,
    blurb: blurbEl ? blurbEl.value : "",
    in_rotation: val("in_rotation").checked,
    hide_when_off: val("hide_when_off").checked,
    announce: val("announce").checked,
    quest_kinds,
    // A quest can only be "needs the room open" if the room owns it at all;
    // otherwise an unticked-then-reticked box could smuggle in a stray kind.
    blocked_kinds: blocked_kinds.filter((k) => quest_kinds.includes(k)),
  };
}

function renderPool(container, data) {
  const host = container.querySelector("[data-pool]");
  const rooms = data.rooms || [];
  if (!rooms.length) {
    host.innerHTML = renderEmpty("No channels in the pool yet.");
    return;
  }
  host.innerHTML = `
    <div style="overflow-x:auto;">
      <table class="table">
        <thead>
          <tr>
            <th>#</th><th>Channel</th><th>Name Shown</th>
            <th title="Take part in the rotation at all">In Rotation</th>
            <th title="Disappear on the days it isn't featured">Hide When Off</th>
            <th title="Name this room in the daily announcement">Announce</th>
            <th>Quests</th><th></th>
          </tr>
        </thead>
        <tbody>${rooms.map(roomRow).join("")}</tbody>
      </table>
    </div>`;

  host.querySelectorAll("tr[data-room]").forEach((tr) => {
    const id = tr.getAttribute("data-room");
    const kindsRow = host.querySelector(`tr[data-kinds-for="${id}"]`);

    tr.querySelector("[data-edit]").addEventListener("click", () => {
      kindsRow.hidden = !kindsRow.hidden;
    });

    tr.querySelector("[data-save-room]").addEventListener("click", async () => {
      try {
        await apiPut(`/api/feature-rotation/rooms/${id}`, readRoom(tr, kindsRow));
        toast("Saved.", "success");
        await refresh(container);
      } catch (err) {
        toast(`Couldn’t save: ${err.message}`, "error");
      }
    });

    tr.querySelector("[data-remove]").addEventListener("click", async () => {
      const ok = await confirmDialog(
        `Take ${chanName(id)} out of the rotation? Its permissions are put back first, so it becomes visible again.`,
      );
      if (!ok) return;
      try {
        await apiDelete(`/api/feature-rotation/rooms/${id}`);
        toast("Removed from the pool.", "success");
        await refresh(container);
      } catch (err) {
        toast(`Couldn’t remove: ${err.message}`, "error");
      }
    });
  });
}

// ── add ──────────────────────────────────────────────────────────────

function renderAdd(container, data) {
  const host = container.querySelector("[data-add]");
  const inPool = new Set((data.rooms || []).map((r) => String(r.channel_id)));
  const available = channels.filter((c) => !inPool.has(String(c.id)));
  if (!available.length) {
    host.innerHTML = `<div class="field-hint">Every channel is already in the pool.</div>`;
    return;
  }
  host.innerHTML = `
    <div style="display:flex; gap:8px; align-items:flex-end; flex-wrap:wrap;">
      <label>Channel
        <select data-add-channel>${available.map((c) => `<option value="${c.id}">#${esc(c.name)}</option>`).join("")}</select>
      </label>
      <button class="btn btn-primary" data-add-go>Add To Pool</button>
    </div>
    <div class="field-hint" style="margin-top:6px;">
      A new room starts in rotation and hidden when off, with no quests linked. Use <strong>Quests</strong> on its row to say what it pays out for.
    </div>`;
  host.querySelector("[data-add-go]").addEventListener("click", async () => {
    const id = host.querySelector("[data-add-channel]").value;
    try {
      await apiPut(`/api/feature-rotation/rooms/${id}`, {
        position: (data.rooms || []).length + 1,
        label: "",
        blurb: "",
        in_rotation: true,
        hide_when_off: true,
        announce: true,
        quest_kinds: [],
        blocked_kinds: [],
      });
      toast("Added to the pool.", "success");
      await refresh(container);
    } catch (err) {
      toast(`Couldn’t add: ${err.message}`, "error");
    }
  });
}
