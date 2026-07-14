// Chat Revive — the feature's entire management surface (no slash commands).
// Settings, per-channel dials, the question bank, and the scoreboard, plus
// the two Discord-side actions: "fire now" and posting the opt-in button.
import { api, apiPost, apiPut, apiDelete, esc } from "../api.js";
import { loadChannels, loadRoles } from "../config-helpers.js";

let channels = [];   // guild text channels [{id,name}]
let roles = [];      // guild roles [{id,name}]
let categories = [];

function chanName(id) {
  const c = channels.find((x) => String(x.id) === String(id));
  return c ? `#${c.name}` : String(id);
}

function chanOptions(selected) {
  return channels
    .map((c) => `<option value="${c.id}" ${String(c.id) === String(selected) ? "selected" : ""}>#${esc(c.name)}</option>`)
    .join("");
}

function roleOptions(selected) {
  const opts = roles
    .map((r) => `<option value="${r.id}" ${String(r.id) === String(selected) ? "selected" : ""}>${esc(r.name)}</option>`)
    .join("");
  return `<option value="">— none —</option>${opts}`;
}

function catOptions(selected) {
  return categories
    .map((c) => `<option value="${c}" ${c === selected ? "selected" : ""}>${esc(c)}</option>`)
    .join("");
}

function flash(el, text, isError) {
  el.innerHTML = `<span class="${isError ? "error" : "field-hint"}">${esc(text)}</span>`;
  if (!isError) setTimeout(() => { el.innerHTML = ""; }, 4000);
}

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading Chat Revive…</div></div>`;
  (async () => {
    [channels, roles] = await Promise.all([
      loadChannels().catch(() => []),
      loadRoles().catch(() => []),
    ]);
    render(container);
  })();
  return null;
}

function render(container) {
  container.innerHTML = `
    <div class="panel">
      <header style="display:flex; align-items:flex-start; justify-content:space-between; gap:12px;">
        <div>
          <h2>Chat Revive</h2>
          <div class="subtitle">Stirs the coals when the hearth goes quiet — dashboard-managed</div>
        </div>
        <button class="btn" data-refresh>Refresh</button>
      </header>

      <section class="card">
        <div class="section-label">Settings</div>
        <div data-settings><div class="empty">Loading…</div></div>
        <div data-settings-status></div>
      </section>

      <section class="card">
        <div class="section-label">Channels</div>
        <div class="field-hint">Revives only ever fire in channels listed here. "Check" explains, in plain language, whether it would fire right now and why.</div>
        <div data-channels><div class="empty">Loading…</div></div>
        <div data-check-output></div>
      </section>

      <section class="card">
        <div class="section-label">Question bank</div>
        <div data-bank><div class="empty">Loading…</div></div>
      </section>

      <section class="card">
        <div class="section-label">Scoreboard</div>
        <div data-stats><div class="empty">Loading…</div></div>
      </section>
    </div>`;

  container.querySelector("[data-refresh]").addEventListener("click", () => refresh(container));
  refresh(container);
}

async function refresh(container) {
  let overview;
  try {
    overview = await api("/api/chat-revive/overview");
  } catch (err) {
    container.querySelector("[data-settings]").innerHTML = `<div class="error">${esc(err.message)}</div>`;
    return;
  }
  categories = overview.categories || [];
  renderSettings(container, overview);
  renderChannels(container, overview.channels || []);
  renderBank(container);
  renderStats(container);
}

// ── settings ─────────────────────────────────────────────────────────

function renderSettings(container, overview) {
  const cfg = overview.config;
  const host = container.querySelector("[data-settings]");
  host.innerHTML = `
    <div class="form-grid" style="display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:10px;">
      <label>Enabled
        <select data-f="enabled"><option value="1" ${cfg.enabled ? "selected" : ""}>on</option><option value="0" ${cfg.enabled ? "" : "selected"}>off</option></select>
      </label>
      <label>Opt-in ping role
        <select data-f="role_id">${roleOptions(cfg.role_id)}</select>
      </label>
      <label>Quiet hours start
        <input data-f="quiet_start" type="number" min="0" max="23" value="${cfg.quiet_start}">
      </label>
      <label>Quiet hours end
        <input data-f="quiet_end" type="number" min="0" max="23" value="${cfg.quiet_end}">
      </label>
      <label>Daily budget (server-wide)
        <input data-f="daily_budget" type="number" min="1" max="10" value="${cfg.daily_budget}">
      </label>
      <label>Breathing room (minutes)
        <input data-f="guild_gap_minutes" type="number" min="10" max="720" value="${cfg.guild_gap_minutes}">
      </label>
      <label>Flourish line
        <select data-f="flourish_enabled"><option value="1" ${cfg.flourish_enabled ? "selected" : ""}>on ("stirring the coals…")</option><option value="0" ${cfg.flourish_enabled ? "" : "selected"}>off (bone-dry)</option></select>
      </label>
    </div>
    <div style="margin-top:10px; display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
      <button class="btn primary" data-save-settings>Save settings</button>
      <span class="field-hint">Bank: ${overview.bank_size} question(s)${overview.bank_size === 0 ? " — enabling seeds the starter pack" : ""}</span>
      <span style="flex:1"></span>
      <select data-optin-channel>${chanOptions("")}</select>
      <button class="btn" data-optin-post>Post opt-in button</button>
    </div>`;

  host.querySelector("[data-save-settings]").addEventListener("click", async () => {
    const get = (k) => host.querySelector(`[data-f="${k}"]`).value;
    const body = {
      enabled: get("enabled") === "1",
      role_id: get("role_id") ? Number(get("role_id")) : null,
      quiet_start: Number(get("quiet_start")),
      quiet_end: Number(get("quiet_end")),
      daily_budget: Number(get("daily_budget")),
      guild_gap_minutes: Number(get("guild_gap_minutes")),
      flourish_enabled: get("flourish_enabled") === "1",
    };
    const status = container.querySelector("[data-settings-status]");
    try {
      const res = await apiPut("/api/chat-revive/config", body);
      flash(status, res.seeded ? `Saved — seeded ${res.seeded} starter questions.` : "Saved.");
      refresh(container);
    } catch (err) {
      flash(status, err.message, true);
    }
  });

  host.querySelector("[data-optin-post]").addEventListener("click", async () => {
    const status = container.querySelector("[data-settings-status]");
    const channelId = Number(host.querySelector("[data-optin-channel]").value);
    try {
      await apiPost("/api/chat-revive/optin-post", { channel_id: channelId });
      flash(status, `Opt-in button posted in ${chanName(channelId)}.`);
    } catch (err) {
      flash(status, err.message, true);
    }
  });
}

// ── channels ─────────────────────────────────────────────────────────

function channelRow(c) {
  return `
    <tr data-channel-id="${c.channel_id}">
      <td>${esc(chanName(c.channel_id))}</td>
      <td><input data-c="enabled" type="checkbox" ${c.enabled ? "checked" : ""}></td>
      <td><input data-c="categories" type="text" value="${esc((c.categories || []).join(", "))}" placeholder="all" style="width:110px"></td>
      <td><input data-c="ping_enabled" type="checkbox" ${c.ping_enabled ? "checked" : ""}></td>
      <td><select data-c="role_id_override" style="width:120px">${roleOptions(c.role_id_override)}</select></td>
      <td><input data-c="rest_hours" type="number" min="1" max="72" step="0.5" value="${c.rest_hours}" style="width:60px"></td>
      <td><input data-c="fire_multiplier" type="number" min="2" max="10" step="0.5" value="${c.fire_multiplier}" style="width:60px"></td>
      <td style="white-space:nowrap;">
        <button class="btn small" data-act="save">Save</button>
        <button class="btn small" data-act="check">Check</button>
        <button class="btn small" data-act="fire">Fire</button>
        <button class="btn small danger" data-act="remove">Remove</button>
      </td>
    </tr>`;
}

function renderChannels(container, rows) {
  const host = container.querySelector("[data-channels]");
  const configured = new Set(rows.map((c) => String(c.channel_id)));
  const addable = channels.filter((c) => !configured.has(String(c.id)));
  host.innerHTML = `
    <table class="data-table">
      <thead><tr>
        <th>Channel</th><th>On</th><th>Categories</th><th>Ping</th>
        <th>Role override</th><th>Rest (h)</th><th>Sensitivity ×</th><th></th>
      </tr></thead>
      <tbody>
        ${rows.map(channelRow).join("") || `<tr><td colspan="8" class="empty">No channels invited yet.</td></tr>`}
      </tbody>
    </table>
    <div style="margin-top:8px; display:flex; gap:8px; align-items:center;">
      <select data-add-channel>${addable.map((c) => `<option value="${c.id}">#${esc(c.name)}</option>`).join("")}</select>
      <button class="btn" data-add>Enable channel</button>
      <span data-channels-status></span>
    </div>`;

  const status = () => host.querySelector("[data-channels-status]");

  host.querySelector("[data-add]").addEventListener("click", async () => {
    const sel = host.querySelector("[data-add-channel]");
    if (!sel.value) return;
    try {
      await apiPut(`/api/chat-revive/channels/${sel.value}`, {});
      refresh(container);
    } catch (err) {
      flash(status(), err.message, true);
    }
  });

  host.querySelectorAll("tr[data-channel-id]").forEach((tr) => {
    const cid = tr.dataset.channelId;
    const val = (k) => tr.querySelector(`[data-c="${k}"]`);
    tr.addEventListener("click", async (evt) => {
      const act = evt.target.dataset && evt.target.dataset.act;
      if (!act) return;
      const out = container.querySelector("[data-check-output]");
      try {
        if (act === "save") {
          const body = {
            enabled: val("enabled").checked,
            categories: val("categories").value.split(",").map((s) => s.trim()).filter((s) => s && s !== "all" && s !== "*"),
            ping_enabled: val("ping_enabled").checked,
            role_id_override: val("role_id_override").value ? Number(val("role_id_override").value) : null,
            rest_hours: Number(val("rest_hours").value),
            fire_multiplier: Number(val("fire_multiplier").value),
          };
          await apiPut(`/api/chat-revive/channels/${cid}`, body);
          flash(status(), `${chanName(cid)} saved.`);
        } else if (act === "check") {
          out.innerHTML = `<div class="empty">Checking ${esc(chanName(cid))}…</div>`;
          const r = await api(`/api/chat-revive/check/${cid}`);
          out.innerHTML = renderCheck(cid, r);
        } else if (act === "fire") {
          const r = await apiPost("/api/chat-revive/fire", { channel_id: Number(cid) });
          flash(status(), `Revived ${chanName(cid)}${r.pinged ? " with a ping" : ""}: ${r.question}`);
          renderStats(container);
        } else if (act === "remove") {
          await apiDelete(`/api/chat-revive/channels/${cid}`);
          refresh(container);
        }
      } catch (err) {
        flash(status(), err.message, true);
      }
    });
  });
}

function renderCheck(cid, r) {
  const head = r.would_fire
    ? `🔥 <strong>${esc(chanName(cid))} would fire right now.</strong>`
    : `😴 <strong>${esc(chanName(cid))}: holding back.</strong>`;
  const bits = [];
  if (r.silence_minutes != null) bits.push(`quiet ${r.silence_minutes}m`);
  if (r.threshold_minutes != null) bits.push(`fires after ${r.threshold_minutes}m`);
  if (r.band) bits.push(`band ${r.band}`);
  if (r.mode) bits.push(`mode ${r.mode}`);
  bits.push(`history ${r.history_days}d`);
  return `
    <div class="card" style="margin-top:8px;">
      <div>${head}</div>
      <div class="field-hint">${esc(r.reason)}</div>
      <div class="field-hint">${esc(bits.join(" · "))}</div>
      <div>${r.would_ask ? `Would ask: <em>${esc(r.would_ask)}</em>${r.would_ping ? " (with a ping)" : ""}` : `<span class="error">No eligible question in the bank.</span>`}</div>
      ${r.live_channel ? "" : `<div class="field-hint">Bot offline or channel unresolved — live checks (slowmode, active games) skipped.</div>`}
    </div>`;
}

// ── question bank ────────────────────────────────────────────────────

async function renderBank(container) {
  const host = container.querySelector("[data-bank]");
  const state = host.dataset;
  let data;
  try {
    data = await api("/api/chat-revive/questions", {
      category: state.filter || "",
      include_retired: state.retired === "1",
    });
  } catch (err) {
    host.innerHTML = `<div class="error">${esc(err.message)}</div>`;
    return;
  }
  const qs = data.questions;
  host.innerHTML = `
    <div style="display:flex; gap:8px; align-items:center; margin-bottom:8px; flex-wrap:wrap;">
      <select data-bank-filter>
        <option value="">all categories</option>${catOptions(state.filter || "")}
      </select>
      <label class="field-hint"><input type="checkbox" data-bank-retired ${state.retired === "1" ? "checked" : ""}> show retired</label>
      <span class="field-hint">${qs.length} question(s)</span>
      <span data-bank-status></span>
    </div>
    <div style="max-height:320px; overflow-y:auto;">
      <table class="data-table">
        <thead><tr><th>#</th><th>Category</th><th>Question</th><th>Used</th><th></th></tr></thead>
        <tbody>
          ${qs.map((q) => `
            <tr>
              <td>${q.id}</td>
              <td>${esc(q.category)}${q.nsfw ? " 🔞" : ""}${q.active ? "" : " <span class='field-hint'>(retired)</span>"}</td>
              <td>${esc(q.text)}</td>
              <td>${q.use_count}</td>
              <td>${q.active ? `<button class="btn small danger" data-retire="${q.id}">Retire</button>` : ""}</td>
            </tr>`).join("") || `<tr><td colspan="5" class="empty">Nothing here.</td></tr>`}
        </tbody>
      </table>
    </div>
    <div style="margin-top:10px; display:flex; gap:8px; flex-wrap:wrap; align-items:center;">
      <input data-new-text type="text" placeholder="Add a question…" style="flex:1; min-width:220px;">
      <select data-new-cat>${catOptions("general")}</select>
      <label class="field-hint"><input type="checkbox" data-new-nsfw> 🔞</label>
      <button class="btn" data-add-q>Add</button>
    </div>
    <details style="margin-top:8px;">
      <summary class="field-hint">Bulk add (one per line; "deep: text" tags a category, "spicy,nsfw: text" flags adult-only)</summary>
      <textarea data-bulk rows="5" style="width:100%; margin-top:6px;"></textarea>
      <button class="btn" data-bulk-add style="margin-top:6px;">Add all</button>
    </details>`;

  const status = () => host.querySelector("[data-bank-status]");

  host.querySelector("[data-bank-filter]").addEventListener("change", (e) => {
    state.filter = e.target.value;
    renderBank(container);
  });
  host.querySelector("[data-bank-retired]").addEventListener("change", (e) => {
    state.retired = e.target.checked ? "1" : "0";
    renderBank(container);
  });
  host.querySelector("[data-add-q]").addEventListener("click", async () => {
    const text = host.querySelector("[data-new-text]").value.trim();
    if (!text) return;
    try {
      await apiPost("/api/chat-revive/questions", {
        text,
        category: host.querySelector("[data-new-cat]").value,
        nsfw: host.querySelector("[data-new-nsfw]").checked,
      });
      renderBank(container);
    } catch (err) {
      flash(status(), err.message, true);
    }
  });
  host.querySelector("[data-bulk-add]").addEventListener("click", async () => {
    const lines = host.querySelector("[data-bulk]").value;
    if (!lines.trim()) return;
    try {
      const r = await apiPost("/api/chat-revive/questions/bulk", { lines });
      flash(status(), `Added ${r.added}, skipped ${r.skipped}.`);
      renderBank(container);
    } catch (err) {
      flash(status(), err.message, true);
    }
  });
  host.querySelectorAll("[data-retire]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await apiPost(`/api/chat-revive/questions/${btn.dataset.retire}/retire`);
        renderBank(container);
      } catch (err) {
        flash(status(), err.message, true);
      }
    });
  });
}

// ── scoreboard ───────────────────────────────────────────────────────

async function renderStats(container) {
  const host = container.querySelector("[data-stats]");
  let s;
  try {
    s = await api("/api/chat-revive/stats");
  } catch (err) {
    host.innerHTML = `<div class="error">${esc(err.message)}</div>`;
    return;
  }
  if (!s.total) {
    host.innerHTML = `<div class="empty">No revives yet — the scoreboard starts after the first one.</div>`;
    return;
  }
  const rate = s.measured ? `${Math.round((s.successes / s.measured) * 100)}%` : "n/a";
  const qRow = (q, ok) => `
    <tr><td>${q.question_id}</td><td>${esc(q.text)}</td><td>${ok ? `${q.successes}/${q.uses}` : `0/${q.uses}`}</td></tr>`;
  host.innerHTML = `
    <div class="card-grid" style="margin-bottom:8px;">
      <div class="stat"><div class="stat-label">Revives all-time</div><div class="stat-value">${s.total}</div></div>
      <div class="stat"><div class="stat-label">This week</div><div class="stat-value">${s.week_revives}</div></div>
      <div class="stat"><div class="stat-label">Sparked conversation</div><div class="stat-value">${rate}</div></div>
      <div class="stat"><div class="stat-label">Measured</div><div class="stat-value">${s.measured}/${s.total}</div></div>
    </div>
    <div class="card-grid" style="grid-template-columns:repeat(auto-fit,minmax(280px,1fr));">
      <div>
        <div class="section-label">Channels (30d)</div>
        <table class="data-table"><thead><tr><th>Channel</th><th>Revives</th><th>Sparked</th></tr></thead>
        <tbody>${(s.channels || []).map((c) => `
          <tr><td>${esc(chanName(c.channel_id))}</td><td>${c.revives}</td><td>${c.successes}/${c.measured}</td></tr>`).join("") || `<tr><td colspan="3" class="empty">—</td></tr>`}
        </tbody></table>
      </div>
      <div>
        <div class="section-label">Carrying the team</div>
        <table class="data-table"><tbody>${(s.top_questions || []).map((q) => qRow(q, true)).join("") || `<tr><td class="empty">—</td></tr>`}</tbody></table>
        ${(s.dud_questions || []).length ? `
          <div class="section-label" style="margin-top:8px;">Dead weight (consider retiring)</div>
          <table class="data-table"><tbody>${s.dud_questions.map((q) => qRow(q, false)).join("")}</tbody></table>` : ""}
      </div>
    </div>`;
}
