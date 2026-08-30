import {
  loadConfig,
  loadChannels,
  loadRoles,
  apiPut,
  apiPost,
  apiDelete,
  showStatus,
  guardForm,
  renderMetaWarning,
  mountChannelPicker,
  mountRolePicker,
  esc,
} from "../config-helpers.js";
import { fmtTs } from "../api.js";
import { renderLoading, renderEmpty, renderError } from "../states.js";
import { confirmDialog, toast } from "../ui.js";

// Listing sites people bump the server on. Each site has its own cooldown;
// when one expires the bot pings the reminder role in the reminder channel and
// keeps a live status widget up to date. Members record their own bumps with
// /bump log, or a detector bot's message can record it automatically.

const HOUR = 3600;

function readyLabel(site) {
  if (!site.bumped_at) return `<span class="badge badge-success">Ready</span>`;
  if (site.ready) return `<span class="badge badge-success">Ready</span>`;
  const mins = Math.round(site.seconds_remaining / 60);
  const text = mins >= 60
    ? `${Math.floor(mins / 60)}h ${mins % 60}m`
    : `${mins}m`;
  return `<span class="badge">Ready in ${esc(text)}</span>`;
}

export function mount(container) {
  container.innerHTML = `<div class="panel">${renderLoading("Loading Bump Tracker…")}</div>`;

  let timer = null;

  (async () => {
    let config, channels, roles;
    try {
      [config, channels, roles] = await Promise.all([
        loadConfig(),
        loadChannels(),
        loadRoles(),
      ]);
    } catch (err) {
      container.querySelector(".panel").innerHTML = renderError(
        `Couldn't load the Bump Tracker settings — ${err.message}. Reload the page to try again.`
      );
      return;
    }

    const bt = config.bump_tracker || {
      configured: false, enabled: false, channel_id: null, role_id: null, sites: [],
    };

    function render() {
      const sites = bt.sites || [];
      const rows = sites.map((s) => `
        <tr data-site="${esc(s.site_name)}">
          <td>${esc(s.site_name)}</td>
          <td>${readyLabel(s)}</td>
          <td>${s.bumped_at ? esc(fmtTs(s.bumped_at)) : "Never"}</td>
          <td>
            <input aria-label="Cooldown for ${esc(s.site_name)} in hours"
                   type="number" data-cooldown step="0.5" min="0.5" max="168"
                   value="${(s.cooldown_seconds / HOUR).toFixed(2).replace(/\.?0+$/, "")}"
                   style="width:80px;" required />
          </td>
          <td>
            <div style="display:flex; flex-direction:column; gap:4px; min-width:190px;">
              <input aria-label="Detector bot ID for ${esc(s.site_name)}"
                     type="text" data-detector inputmode="numeric"
                     value="${esc(s.detector_bot_id || "")}" placeholder="Bot ID (optional)" />
              <input aria-label="Bump-confirmed text for ${esc(s.site_name)}"
                     type="text" data-pattern maxlength="200"
                     value="${esc(s.detector_pattern || "")}" placeholder="Text when bumped" />
              <input aria-label="Bump-refused text for ${esc(s.site_name)}"
                     type="text" data-failure maxlength="200"
                     value="${esc(s.failure_pattern || "")}" placeholder="Text when refused" />
            </div>
          </td>
          <td style="white-space:nowrap;">
            <button type="button" class="btn" data-act="save">Save</button>
            <button type="button" class="btn" data-act="log">Log Bump</button>
            <button type="button" class="btn btn-danger" data-act="remove">Remove</button>
          </td>
          <td><span data-row-status></span></td>
        </tr>`).join("");

      container.querySelector(".panel").innerHTML = `
        <header>
          <h2>Bump Tracker</h2>
          <div class="subtitle">Remind a role when a listing site is ready to be bumped again.</div>
        </header>
        ${renderMetaWarning()}

        <form data-form class="form form-cards">
          <div class="card">
            <div class="section-label">Reminders</div>
            <div class="field">
              <label>
                <input type="checkbox" name="enabled" ${bt.enabled ? "checked" : ""} />
                Send Bump Reminders
              </label>
              <div class="field-hint">When checked, the bot pings the role below in the
                channel below as soon as a site's cooldown expires, and keeps a live
                status message updated there. Unchecked, bumps are still recorded —
                both from <code>/bump log</code> and from detection — but nobody is
                pinged and the status message stops updating.</div>
            </div>
            <div class="field">
              <label>Reminder Channel</label>
              <span data-picker="channel"></span>
              <div class="field-hint">Where reminders and the live status message are
                posted. Members also run <code>/bump log</code> here after bumping, and
                auto-detection only watches this channel.</div>
            </div>
            <div class="field">
              <label>Ping Role</label>
              <span data-picker="role"></span>
              <div class="field-hint">The role mentioned when a site becomes ready.
                Choose "(none)" to post reminders without pinging anyone.</div>
            </div>
            <div style="display:flex; gap:8px; align-items:center;">
              <button type="submit" class="btn btn-primary">Save</button>
              <span data-status></span>
            </div>
          </div>
        </form>

        <div class="card">
          <div class="section-label">Sites</div>
          ${sites.length ? `
          <div style="overflow-x:auto;">
            <table class="table" data-sites>
              <thead><tr>
                <th>Site</th><th>Status</th><th>Last Bumped</th>
                <th>Cooldown (hours)</th><th>Detection</th><th>Actions</th><th></th>
              </tr></thead>
              <tbody>${rows}</tbody>
            </table>
          </div>` : renderEmpty(
            "No sites tracked yet. Add the listing sites your server is on below — " +
            "each one gets its own cooldown and reminder."
          )}
          <div class="field-hint" style="margin-top:8px;">
            <strong>Detection</strong> is optional. Set <strong>Bot ID</strong> to the
            listing bot's user ID and a bump is recorded automatically when that bot
            posts <em>in the Reminder Channel above</em> — that is the only channel
            watched, so a listing bot that answers somewhere else is never detected,
            and detection is off entirely until a Reminder Channel is picked. Leave the
            Bot ID blank to rely on <code>/bump log</code> alone.
            <br /><strong>Text when bumped</strong> narrows that to messages containing
            a phrase (e.g. <code>Bump done!</code>); blank means any message from the bot
            counts.
            <br /><strong>Text when refused</strong> is the safety catch — a phrase that
            marks a <em>rejected</em> bump (e.g. <code>already bumped recently</code>).
            Matching messages are ignored instead of resetting the timer. Set this
            whenever the bot replies publicly on failure, otherwise a refusal looks
            like a successful bump.
          </div>
        </div>

        <form data-add-form class="card">
          <div class="section-label">Add a Site</div>
          <div class="field">
            <label for="new-site">Site Name</label>
            <input id="new-site" name="site_name" type="text" required maxlength="40"
                   placeholder="disboard" />
            <div class="field-hint">Shown in reminders and in <code>/bump log</code>.
              Use the listing site's common name, lowercase.</div>
          </div>
          <div class="field">
            <label for="new-cooldown">Cooldown (hours)</label>
            <input id="new-cooldown" name="cooldown_hours" type="number" step="0.5"
                   min="0.5" max="168" value="2" required />
            <div class="field-hint">How long after a bump before the site can be bumped
              again. Disboard is 2 hours; most directory sites are 6 or 24.</div>
          </div>
          <div class="field">
            <label for="new-detector">Detector Bot ID</label>
            <input id="new-detector" name="detector_bot_id" type="text" inputmode="numeric"
                   placeholder="Optional" />
            <div class="field-hint">Optional, as described above.</div>
          </div>
          <div class="field">
            <label for="new-pattern">Text when bumped</label>
            <input id="new-pattern" name="detector_pattern" type="text" maxlength="200"
                   placeholder="Bump done!" />
            <div class="field-hint">Leave blank to count any message from that bot.</div>
          </div>
          <div class="field">
            <label for="new-failure">Text when refused</label>
            <input id="new-failure" name="failure_pattern" type="text" maxlength="200"
                   placeholder="already bumped recently" />
            <div class="field-hint">Messages containing this are ignored, so a rejected
              bump can't reset the timer.</div>
          </div>
          <div style="display:flex; gap:8px; align-items:center;">
            <button type="submit" class="btn btn-primary">Add Site</button>
            <span data-add-status></span>
          </div>
        </form>
      `;

      wire();
    }

    function wire() {
      const form = container.querySelector("[data-form]");
      const status = form.querySelector("[data-status]");

      const channelPicker = mountChannelPicker(
        form.querySelector('[data-picker="channel"]'),
        channels,
        String(bt.channel_id || "0"),
        { label: "Reminder Channel" },
      );
      const rolePicker = mountRolePicker(
        form.querySelector('[data-picker="role"]'),
        roles,
        String(bt.role_id || "0"),
        { label: "Ping Role" },
      );

      guardForm(form);

      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        const enabled = form.querySelector('input[name="enabled"]').checked;
        const channelId = channelPicker.getValue() || "0";
        if (enabled && channelId === "0") {
          showStatus(status, false, "Pick a Reminder Channel before switching reminders on.");
          return;
        }
        try {
          await apiPut("/api/config/bump-tracker", {
            channel_id: channelId,
            role_id: rolePicker.getValue() || "0",
            enabled,
          });
          bt.enabled = enabled;
          bt.channel_id = channelId;
          bt.role_id = rolePicker.getValue() || "0";
          showStatus(status, true);
        } catch (err) {
          showStatus(status, false, err.message);
        }
      });

      // ── Per-site rows ────────────────────────────────────────────────
      const table = container.querySelector("[data-sites]");
      table?.addEventListener("click", async (e) => {
        const btn = e.target.closest("button[data-act]");
        if (!btn) return;
        const row = btn.closest("tr[data-site]");
        const site = row.dataset.site;
        const rowStatus = row.querySelector("[data-row-status]");
        const act = btn.dataset.act;

        if (act === "save") {
          const hours = parseFloat(row.querySelector("[data-cooldown]").value);
          if (!Number.isFinite(hours) || hours < 0.5 || hours > 168) {
            showStatus(rowStatus, false, "Cooldown must be between 0.5 and 168 hours.");
            return;
          }
          const detector = row.querySelector("[data-detector]").value.trim();
          if (detector && !/^\d{15,25}$/.test(detector)) {
            showStatus(rowStatus, false, "Detector Bot ID must be a Discord user ID (numbers only).");
            return;
          }
          try {
            await apiPut(`/api/config/bump-tracker/sites/${encodeURIComponent(site)}`, {
              cooldown_hours: hours,
              detector_bot_id: detector || null,
              detector_pattern: row.querySelector("[data-pattern]").value.trim(),
              failure_pattern: row.querySelector("[data-failure]").value.trim(),
            });
            showStatus(rowStatus, true);
          } catch (err) {
            showStatus(rowStatus, false, err.message);
          }
          return;
        }

        if (act === "log") {
          try {
            await apiPost(`/api/config/bump-tracker/sites/${encodeURIComponent(site)}/log`, {});
            toast(`Recorded a bump for ${site}.`, "success");
            await refresh();
          } catch (err) {
            showStatus(rowStatus, false, err.message);
          }
          return;
        }

        if (act === "remove") {
          const ok = await confirmDialog(
            `Stop tracking ${site}? Its cooldown and bump history are deleted, and ` +
            `nobody will be reminded to bump it again. You can add it back later.`,
            { danger: true, title: `Remove ${site}?`, confirmLabel: "Remove" },
          );
          if (!ok) return;
          try {
            await apiDelete(`/api/config/bump-tracker/sites/${encodeURIComponent(site)}`);
            await refresh();
          } catch (err) {
            showStatus(rowStatus, false, err.message);
          }
        }
      });

      // ── Add a site ───────────────────────────────────────────────────
      const addForm = container.querySelector("[data-add-form]");
      const addStatus = addForm.querySelector("[data-add-status]");
      guardForm(addForm);

      addForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const name = addForm.querySelector('[name="site_name"]').value.trim().toLowerCase();
        const hours = parseFloat(addForm.querySelector('[name="cooldown_hours"]').value);
        const detector = addForm.querySelector('[name="detector_bot_id"]').value.trim();

        if (!name) {
          showStatus(addStatus, false, "Give the site a name.");
          return;
        }
        if ((bt.sites || []).some((s) => s.site_name.toLowerCase() === name)) {
          showStatus(addStatus, false, `${name} is already tracked — edit it in the table above.`);
          return;
        }
        if (!Number.isFinite(hours) || hours < 0.5 || hours > 168) {
          showStatus(addStatus, false, "Cooldown must be between 0.5 and 168 hours.");
          return;
        }
        if (detector && !/^\d{15,25}$/.test(detector)) {
          showStatus(addStatus, false, "Detector Bot ID must be a Discord user ID (numbers only).");
          return;
        }

        try {
          await apiPut(`/api/config/bump-tracker/sites/${encodeURIComponent(name)}`, {
            cooldown_hours: hours,
            detector_bot_id: detector || null,
            detector_pattern: addForm.querySelector('[name="detector_pattern"]').value.trim(),
            failure_pattern: addForm.querySelector('[name="failure_pattern"]').value.trim(),
          });
          addForm.reset();
          await refresh();
          toast(`Now tracking ${name}.`, "success");
        } catch (err) {
          showStatus(addStatus, false, err.message);
        }
      });
    }

    async function refresh() {
      try {
        const fresh = await loadConfig();
        Object.assign(bt, fresh.bump_tracker || {});
      } catch (_) {
        // Keep the current view rather than blanking it on a refresh failure.
      }
      render();
    }

    render();

    // Countdowns are relative to now, so re-render them once a minute rather
    // than letting "Ready in 12m" sit frozen on screen.
    timer = setInterval(() => {
      const now = Date.now() / 1000;
      for (const s of bt.sites || []) {
        if (!s.bumped_at) continue;
        const elapsed = now - s.bumped_at;
        s.seconds_remaining = Math.max(0, Math.round(s.cooldown_seconds - elapsed));
        s.ready = elapsed >= s.cooldown_seconds;
      }
      const table = container.querySelector("[data-sites]");
      if (!table) return;
      for (const s of bt.sites || []) {
        const cell = table.querySelector(`tr[data-site="${CSS.escape(s.site_name)}"] td:nth-child(2)`);
        if (cell) cell.innerHTML = readyLabel(s);
      }
    }, 60000);
  })();

  return {
    unmount() {
      if (timer) clearInterval(timer);
    },
  };
}
