import { api, apiPost, esc } from "../api.js";
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

  (async () => {
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
          <div class="field-hint">Party games can only be started in these channels.
            With the list empty, no game can be played anywhere in the server.</div>
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
          <div class="section-label">Audit Channel</div>
          <div class="field-hint">Every game that starts, finishes, or is canceled is
            recorded here, so moderators can look back at what happened. Leave it unset
            to keep no record.</div>
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
          `<option value="${t}" ${t === selected ? "selected" : ""}>${t}</option>`
        ).join("");

        let rows = "";
        for (const ch of channels) {
          const added = ch.added_at ? String(ch.added_at).slice(0, 10) : "";
          rows += `<tr>
            <td>${esc(channelName(guildChannels, ch.channel_id))}</td>
            <td style="font-size:12px;">${esc(added)}</td>
            <td>
              <select data-ctrl="legitlibs-max-tier" data-cid="${esc(ch.channel_id)}" style="width:64px;"
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
      if (!cid || cid === "0") { showStatus(st, false, "Pick a channel first"); return; }
      try {
        await apiPut("/api/games/config/audit", { channel_id: cid });
        showStatus(st, true);
        loadAudit();
      } catch (err) { showStatus(st, false, err.message); }
    });

    loadAllowedChannels();
    loadEditorRole();
    loadAudit();
  })();

  return { unmount() {} };
}
