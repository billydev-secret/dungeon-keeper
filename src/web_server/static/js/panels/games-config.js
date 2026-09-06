import { api, apiPost, esc } from "../api.js";
import { TIER_LABELS, TIER_EMOJI } from "./games-panel-shared.js";
import {
  loadChannels as loadChannelMeta,
  loadRoles as loadRoleMeta,
  mountChannelPicker,
  mountRolePicker,
  channelName,
  roleName,
  renderMetaWarning,
  apiPut,
  apiDelete,
  showStatus,
  mountAsync,
} from "../config-helpers.js";
import { confirmDialog } from "../ui.js";

// All user-supplied content rendered via innerHTML uses esc() for XSS safety.
//
// Every row on this page commits on its own button and reports through its own
// showStatus line — the LegitLibs tier used to auto-save with a toast while its
// siblings used Save buttons, which made it impossible to tell when a change
// had actually stuck (W-C8).

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading configuration…</div></div>`;

  return mountAsync(container, async () => {
    const [guildChannels, roles] = await Promise.all([loadChannelMeta(), loadRoleMeta()]);

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Games Global Config</h2>
          <div class="subtitle">Which channels may host party games, who can change game settings, and where game events are logged</div>
        </header>
        ${renderMetaWarning()}

        <section>
          <div class="section-label">Allowed Channels</div>
          <div class="field-hint">Party games members start themselves with /games play can only be
            started in these channels. With the list empty, no game can be played anywhere in the server.
            Games the dashboard schedules, and rooms the feature rotation runs, post in the channel
            they were pointed at whether or not it is listed here.</div>
          <div data-region="channels-list" style="margin-bottom:10px;"><div class="empty">Loading…</div></div>
          <div class="form" style="display:flex;flex-wrap:wrap;gap:8px;align-items:flex-end;max-width:none;">
            <div class="field" style="margin:0;flex:1;min-width:220px;max-width:280px;">
              <label>Channel to Allow</label>
              <span data-picker="new-channel"></span>
            </div>
            <button class="btn btn-primary" data-action="add-channel">Add</button>
            <span data-status="channel" class="save-status" style="margin-left:4px;"></span>
          </div>
        </section>

        <section>
          <div class="section-label">Game Host Role</div>
          <div class="field-hint">Members with this role can open every game settings
            page and the LegitLibs editor. Admins always have that access. Choose
            "(none)" to keep game settings admin-only.</div>
          <div data-region="editor-role-current" style="margin-bottom:10px;"><div class="empty">Loading…</div></div>
          <div class="form" style="display:flex;flex-wrap:wrap;gap:8px;align-items:flex-end;max-width:none;">
            <div class="field" style="margin:0;flex:1;min-width:220px;max-width:280px;">
              <label>Host Role</label>
              <span data-picker="editor-role"></span>
            </div>
            <button class="btn btn-primary" data-action="save-editor-role">Save</button>
            <span data-status="editor-role" class="save-status" style="margin-left:4px;"></span>
          </div>
        </section>

        <section>
          <div class="section-label">Available on This Server</div>
          <div class="field-hint">Untick a game to take it off the menu here: the
            command refuses to start it and the scheduler skips it. Games already
            running finish normally. Each game with its own settings page carries
            the same switch — this is the whole list in one place, including the
            games that have no page of their own.</div>
          <div data-region="availability" style="margin-top:10px;"><div class="empty">Loading…</div></div>
        </section>

        <section>
          <div class="section-label">Idle Lobbies</div>
          <div class="field-hint">Six games open a lobby and wait for someone to press start
            (Clapback, Spin the Compliment, Marry-Fornicate-Kiss, Most Likely To, Mt. Rushmore Draft,
            Story Builder). A lobby opened without a countdown gets its host tagged after the first
            number of minutes, and one that still has fewer people than the game needs to start is
            closed after the second — no coins are paid for a lobby that never began. A lobby with
            enough players to start is always left to its host. Set either to 0 to turn that step off.</div>
          <div class="form" style="display:flex;flex-wrap:wrap;gap:8px;align-items:flex-end;max-width:none;">
            <div class="field" style="margin:0;min-width:180px;max-width:220px;">
              <label>Tag the Host After (Minutes)
                <input class="w-full" type="number" min="0" max="1440" step="1" data-ctrl="idle-nudge" />
              </label>
            </div>
            <div class="field" style="margin:0;min-width:180px;max-width:220px;">
              <label>Close an Empty Lobby After (Minutes)
                <input class="w-full" type="number" min="0" max="1440" step="1" data-ctrl="idle-cancel" />
              </label>
            </div>
            <button class="btn btn-primary" data-action="save-lobby">Save</button>
            <span data-status="lobby" class="save-status" style="margin-left:4px;"></span>
          </div>
        </section>

        <section>
          <div class="section-label">Game Night Ping</div>
          <div class="field-hint">Whenever one of those six lobbies opens — started by a member
            with /games play or by a schedule — the bot posts one line in that channel tagging this
            role, with a link to the lobby and when it starts. Members pick the role up themselves
            (offer it on <a href="/#/onboarding">Config &rarr; Discord Onboarding</a>, like
            the other ping roles). Leave it untouched and the bot creates <strong>@Game Night</strong>
            the first time a lobby opens; choose "(none)" and no lobby is announced. A schedule that
            announces itself uses its own ping instead, never both.</div>
          <div data-region="game-night-current" style="margin-bottom:10px;"><div class="empty">Loading…</div></div>
          <div class="form" style="display:flex;flex-wrap:wrap;gap:8px;align-items:flex-end;max-width:none;">
            <div class="field" style="margin:0;flex:1;min-width:220px;max-width:280px;">
              <label>Game Night Ping Role</label>
              <span data-picker="game-night-role"></span>
            </div>
            <button class="btn btn-primary" data-action="save-game-night">Save</button>
            <span data-status="game-night" class="save-status" style="margin-left:4px;"></span>
          </div>
        </section>

        <section>
          <div class="section-label">Audit Channel</div>
          <div class="field-hint">Anonymous submissions &mdash; the answers, hot takes,
            compliments, fantasies and AMA questions members send without their name on
            them &mdash; are mirrored here with the author attached, so moderators can
            trace anything that crosses a line. Nothing else is logged: this is not a
            record of games starting or finishing. Leave it unset to keep no record.</div>
          <div data-region="audit-current" style="margin-bottom:10px;"><div class="empty">Loading…</div></div>
          <div class="form" style="display:flex;flex-wrap:wrap;gap:8px;align-items:flex-end;max-width:none;">
            <div class="field" style="margin:0;flex:1;min-width:220px;max-width:280px;">
              <label>Audit Channel</label>
              <span data-picker="audit-channel"></span>
            </div>
            <button class="btn btn-primary" data-action="save-audit">Save</button>
            <span data-status="audit" class="save-status" style="margin-left:4px;"></span>
          </div>
        </section>
      </div>
    `;

    function region(name) { return container.querySelector(`[data-region="${name}"]`); }
    function statusEl(name) { return container.querySelector(`[data-status="${name}"]`); }

    const newChannelPicker = mountChannelPicker(
      container.querySelector('[data-picker="new-channel"]'),
      guildChannels, "0", { label: "Channel to Allow" },
    );
    const editorRolePicker = mountRolePicker(
      container.querySelector('[data-picker="editor-role"]'),
      roles, "0", { label: "Host Role" },
    );
    const auditChannelPicker = mountChannelPicker(
      container.querySelector('[data-picker="audit-channel"]'),
      guildChannels, "0", { label: "Audit Channel" },
    );
    const gameNightPicker = mountRolePicker(
      container.querySelector('[data-picker="game-night-role"]'),
      roles, "0", { label: "Game Night Ping Role" },
    );

    async function loadAllowedChannels() {
      const el = region("channels-list");
      try {
        const data = await api("/api/games/config/channels");
        const channels = data.channels || [];
        if (!channels.length) {
          el.innerHTML = `<div class="empty">No channels can host games yet. Add one below — until then, every party game is unavailable.</div>`;
          return;
        }
        const tierOptions = (selected) => [1, 2, 3, 4].map((t) =>
          `<option value="${t}" ${t === selected ? "selected" : ""}>`
          + `${TIER_EMOJI[t]} ${TIER_LABELS[t]}</option>`
        ).join("");

        let rows = "";
        for (const ch of channels) {
          const added = ch.added_at ? String(ch.added_at).slice(0, 10) : "";
          rows += `<tr>
            <td>${esc(channelName(guildChannels, ch.channel_id))}</td>
            <td style="font-size:12px;">${esc(added)}</td>
            <td>
              <select data-ctrl="legitlibs-max-tier" data-cid="${esc(ch.channel_id)}" style="min-width:130px;"
                      aria-label="Highest LegitLibs heat tier allowed in this channel">
                ${tierOptions(ch.legitlibs_max_tier)}
              </select>
            </td>
            <td>
              <button class="btn" style="padding:2px 8px;font-size:12px;"
                      data-action="save-tier" data-cid="${esc(ch.channel_id)}">Save</button>
              <span class="save-status" data-tier-status="${esc(ch.channel_id)}" style="margin-left:4px;font-size:12px;"></span>
            </td>
            <td><button class="btn" style="padding:2px 8px;font-size:12px;" data-action="remove-channel" data-cid="${esc(ch.channel_id)}">Remove</button></td>
          </tr>`;
        }
        el.innerHTML = `<div style="overflow-x:auto;"><table style="width:100%;max-width:680px;">
          <thead><tr>
            <th>Channel</th><th>Added</th>
            <th title="The spiciest LegitLibs heat tier allowed in this channel">Highest LegitLibs Tier</th>
            <th style="width:110px;"></th><th style="width:90px;"></th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table></div>`;

        el.querySelectorAll('[data-action="remove-channel"]').forEach((btn) => {
          btn.addEventListener("click", async () => {
            const cid = btn.dataset.cid;
            const label = channelName(guildChannels, cid);
            const ok = await confirmDialog(
              `Games can no longer be started in ${label}. Games already running there are unaffected.`,
              { title: "Remove this channel?", danger: true, confirmLabel: "Remove" },
            );
            if (!ok) return;
            try {
              await apiDelete(`/api/games/config/channels/${encodeURIComponent(cid)}`);
              loadAllowedChannels();
            } catch (err) {
              showStatus(statusEl("channel"), false, `Could not remove the channel: ${err.message}`);
            }
          });
        });

        // Explicit Save per row, feedback in the row's own status line — the
        // same commit model as every other control on this page.
        el.querySelectorAll('[data-action="save-tier"]').forEach((btn) => {
          btn.addEventListener("click", async () => {
            const cid = btn.dataset.cid;
            const sel = el.querySelector(`[data-ctrl="legitlibs-max-tier"][data-cid="${CSS.escape(cid)}"]`);
            const st = el.querySelector(`[data-tier-status="${CSS.escape(cid)}"]`);
            try {
              await apiPut(`/api/games/config/channels/${encodeURIComponent(cid)}/legitlibs-max-tier`, {
                max_tier: parseInt(sel.value, 10),
              });
              showStatus(st, true);
            } catch (err) { showStatus(st, false, err.message); }
          });
        });
      } catch (err) {
        el.innerHTML = `<div class="error">The allowed-channel list failed to load: ${esc(err.message)}</div>`;
      }
    }

    // Photo Challenge is addressed by the same config row, but it has its own
    // page (Photo Challenge → Setup & Schedule) where the switch sits beside
    // its channel and schedule, so it isn't repeated here.
    const AVAILABILITY_SKIP = new Set(["photo"]);

    async function loadAvailability() {
      const el = region("availability");
      try {
        const data = await api("/api/games/config/games");
        const games = data.games || {};
        const names = Object.keys(games)
          .filter((gt) => !AVAILABILITY_SKIP.has(gt))
          .sort((a, b) => (games[a].label || a).localeCompare(games[b].label || b));
        if (!names.length) { el.innerHTML = `<div class="empty">No games registered.</div>`; return; }
        el.innerHTML = `<div style="display:flex;flex-wrap:wrap;gap:8px 24px;">` + names.map((gt) => `
          <label style="display:flex;align-items:center;gap:8px;cursor:pointer;min-width:220px;">
            <input type="checkbox" data-game="${esc(gt)}" style="width:16px;height:16px;cursor:pointer;"
                   ${games[gt].enabled === false ? "" : "checked"} />
            <span>${esc(games[gt].label || gt)}</span>
            <span class="save-status" data-game-status="${esc(gt)}" style="font-size:12px;"></span>
          </label>`).join("") + `</div>`;

        // Each switch commits on its own change — there is nothing else on the
        // row to fill in, so a Save button would only add a step to forget.
        el.querySelectorAll("[data-game]").forEach((box) => {
          box.addEventListener("change", async () => {
            const gt = box.dataset.game;
            const st = el.querySelector(`[data-game-status="${CSS.escape(gt)}"]`);
            try {
              // No options in the payload: this must not disturb the dials set
              // on the game's own page.
              await apiPut(`/api/games/config/games/${encodeURIComponent(gt)}`, { enabled: box.checked });
              showStatus(st, true, box.checked ? "On" : "Off");
            } catch (err) {
              box.checked = !box.checked;
              showStatus(st, false, err.message);
            }
          });
        });
      } catch (err) {
        el.innerHTML = `<div class="error">The game list failed to load: ${esc(err.message)}</div>`;
      }
    }

    async function loadEditorRole() {
      const el = region("editor-role-current");
      try {
        const data = await api("/api/games/config/editor-role");
        if (data?.role_id) {
          el.innerHTML = `<div>Currently: ${esc(roleName(roles, data.role_id))}</div>`;
          editorRolePicker.setValue(String(data.role_id));
        } else {
          el.innerHTML = `<div class="empty">No host role set — only admins can change game settings.</div>`;
          editorRolePicker.setValue("0");
        }
      } catch (err) {
        el.innerHTML = `<div class="error">The host role failed to load: ${esc(err.message)}</div>`;
      }
    }

    function renderGameNight(data) {
      const el = region("game-night-current");
      // null: never touched, so the bot will make the role itself; "0": an
      // admin chose "(none)"; otherwise a role id. The three states read
      // differently on purpose — a blank and a decision are not the same.
      const rid = data.game_night_ping_role_id;
      if (rid === null || rid === undefined) {
        el.innerHTML = `<div class="empty">Not set yet — the bot will create <strong>@Game Night</strong> the next time a lobby opens and tag it from then on.</div>`;
        gameNightPicker.setValue("0");
      } else if (String(rid) === "0") {
        el.innerHTML = `<div class="empty">Off — "(none)" is chosen, so lobbies aren't announced. Pick a role (or use <em>Make it now</em> on Config &rarr; Bot-Managed Roles) to turn it back on.</div>`;
        gameNightPicker.setValue("0");
      } else {
        el.innerHTML = `<div>Currently: ${esc(roleName(roles, rid))}</div>`;
        gameNightPicker.setValue(String(rid));
      }
    }

    async function loadLobbyDials() {
      const st = statusEl("lobby");
      try {
        const data = await api("/api/games/config/lobby");
        container.querySelector('[data-ctrl="idle-nudge"]').value = data.idle_nudge_minutes ?? "";
        container.querySelector('[data-ctrl="idle-cancel"]').value = data.idle_cancel_minutes ?? "";
        renderGameNight(data);
      } catch (err) {
        showStatus(st, false, `The idle-lobby settings failed to load: ${err.message}`);
      }
    }

    async function loadAudit() {
      const el = region("audit-current");
      try {
        const data = await api("/api/games/config/audit");
        if (!data) {
          el.innerHTML = `<div class="empty">No audit channel set — game events are not being recorded.</div>`;
          auditChannelPicker.setValue("0");
        } else {
          el.innerHTML = `<div>Currently: ${esc(channelName(guildChannels, data.channel_id))}</div>`;
          auditChannelPicker.setValue(String(data.channel_id));
        }
      } catch (err) {
        el.innerHTML = `<div class="error">The audit channel failed to load: ${esc(err.message)}</div>`;
      }
    }

    container.querySelector('[data-action="save-editor-role"]').addEventListener("click", async () => {
      const st = statusEl("editor-role");
      // Role id stays a string, exactly as the plain <select> posted it.
      const rid = editorRolePicker.getValue();
      try {
        if (!rid || rid === "0") {
          await apiDelete("/api/games/config/editor-role");
          showStatus(st, true, "Cleared — admins only");
        } else {
          await apiPut("/api/games/config/editor-role", { role_id: rid });
          showStatus(st, true);
        }
        loadEditorRole();
      } catch (err) { showStatus(st, false, err.message); }
    });

    container.querySelector('[data-action="add-channel"]').addEventListener("click", async () => {
      const st = statusEl("channel");
      const cid = newChannelPicker.getValue();
      if (!cid || cid === "0") { showStatus(st, false, "Pick a channel first"); return; }
      try {
        await apiPost("/api/games/config/channels", { channel_id: cid });
        newChannelPicker.setValue("0");
        showStatus(st, true, "Added");
        loadAllowedChannels();
      } catch (err) { showStatus(st, false, err.message); }
    });

    container.querySelector('[data-action="save-audit"]').addEventListener("click", async () => {
      const st = statusEl("audit");
      const cid = auditChannelPicker.getValue();
      try {
        // "(none)" means stop recording. The hint above this control has always
        // promised the audit channel can be left unset, but Save used to reject
        // "(none)" with "Pick a channel first" — so once a channel was chosen
        // there was no way back. DELETE clears it, same as the host role does.
        if (!cid || cid === "0") {
          await apiDelete("/api/games/config/audit");
          showStatus(st, true, "Cleared — no record kept");
        } else {
          await apiPut("/api/games/config/audit", { channel_id: cid });
          showStatus(st, true);
        }
        loadAudit();
      } catch (err) { showStatus(st, false, err.message); }
    });

    container.querySelector('[data-action="save-game-night"]').addEventListener("click", async () => {
      const st = statusEl("game-night");
      // Role id stays a string, exactly as the picker holds it — "0" is
      // "(none)", a stored decision, not a cleared field.
      const rid = gameNightPicker.getValue() || "0";
      try {
        const data = await apiPut("/api/games/config/lobby", { game_night_ping_role_id: rid });
        showStatus(st, true, rid === "0" ? "Off — lobbies aren't announced" : undefined);
        renderGameNight(data);
      } catch (err) { showStatus(st, false, err.message); }
    });

    container.querySelector('[data-action="save-lobby"]').addEventListener("click", async () => {
      const st = statusEl("lobby");
      const nudge = parseInt(container.querySelector('[data-ctrl="idle-nudge"]').value, 10);
      const cancel = parseInt(container.querySelector('[data-ctrl="idle-cancel"]').value, 10);
      if (Number.isNaN(nudge) || Number.isNaN(cancel) || nudge < 0 || cancel < 0) {
        showStatus(st, false, "Both fields need a whole number of minutes (0 turns a step off).");
        return;
      }
      try {
        await apiPut("/api/games/config/lobby", { idle_nudge_minutes: nudge, idle_cancel_minutes: cancel });
        showStatus(st, true);
        loadLobbyDials();
      } catch (err) { showStatus(st, false, err.message); }
    });

    loadAllowedChannels();
    loadAvailability();
    loadEditorRole();
    loadLobbyDials();
    loadAudit();
  }, { errorMsg: "Couldn’t load the games global config." });
}
