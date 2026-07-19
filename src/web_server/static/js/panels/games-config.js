import { api, apiPost, esc } from "../api.js";
import {
  loadChannels as loadChannelMeta,
  loadRoles as loadRoleMeta,
  channelSelect,
  roleSelect,
  channelName,
  roleName,
  apiPut,
  apiDelete,
  showStatus,
} from "../config-helpers.js";
import { toast, confirmDialog } from "../ui.js";

// All user-supplied content rendered via innerHTML uses esc() for XSS safety.

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading config…</div></div>`;

  (async () => {
    const [guildChannels, roles] = await Promise.all([loadChannelMeta(), loadRoleMeta()]);

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Games Config</h2>
          <div class="subtitle">Configure which channels can run games and where audit logs are sent.</div>
        </header>

        <section>
          <div class="section-label">Allowed Channels</div>
          <div class="field-hint">Only these channels may host party games.</div>
          <div data-region="channels-list" style="margin-bottom:10px;"><div class="empty">Loading</div></div>
          <div class="form" style="display:flex;flex-direction:row;gap:8px;align-items:flex-end;max-width:none;">
            <div class="field" style="margin:0;flex:1;max-width:280px;">
              <label>Channel
                <select data-ctrl="new-channel" style="width:100%;">
                  <option value="">Select a channel…</option>
                  ${channelSelect(guildChannels, "", { allowNone: false })}
                </select>
              </label>
            </div>
            <button class="btn btn-primary" data-action="add-channel">Add</button>
            <span data-status="channel" class="save-status" style="margin-left:4px;"></span>
          </div>
        </section>

        <section>
          <div class="section-label">Game Host Role</div>
          <div class="field-hint">Members with this role can access all game settings and the LegitLibs editor. Admins always have access. Choose "(none)" to restrict to admins only.</div>
          <div data-region="editor-role-current" style="margin-bottom:10px;"><div class="empty">Loading</div></div>
          <div class="form" style="display:flex;flex-direction:row;gap:8px;align-items:flex-end;max-width:none;">
            <div class="field" style="margin:0;flex:1;max-width:280px;">
              <label>Role
                <select data-ctrl="editor-role" style="width:100%;">${roleSelect(roles, "0")}</select>
              </label>
            </div>
            <button class="btn btn-primary" data-action="save-editor-role">Save</button>
            <span data-status="editor-role" class="save-status" style="margin-left:4px;"></span>
          </div>
        </section>

        <section>
          <div class="section-label">Audit Channel</div>
          <div class="field-hint">When set, game events are logged to this channel.</div>
          <div data-region="audit-current" style="margin-bottom:10px;"><div class="empty">Loading</div></div>
          <div class="form" style="display:flex;flex-direction:row;gap:8px;align-items:flex-end;max-width:none;">
            <div class="field" style="margin:0;flex:1;max-width:280px;">
              <label>Channel
                <select data-ctrl="audit-channel" style="width:100%;">
                  <option value="">Select a channel…</option>
                  ${channelSelect(guildChannels, "", { allowNone: false })}
                </select>
              </label>
            </div>
            <button class="btn btn-primary" data-action="save-audit">Save</button>
            <span data-status="audit" class="save-status" style="margin-left:4px;"></span>
          </div>
        </section>
      </div>
    `;

    function ctrl(name) { return container.querySelector(`[data-ctrl="${name}"]`); }
    function region(name) { return container.querySelector(`[data-region="${name}"]`); }

    async function loadAllowedChannels() {
      const el = region("channels-list");
      try {
        const data = await api("/api/games/config/channels");
        const channels = data.channels || [];
        if (!channels.length) {
          el.innerHTML = `<div class="empty">No allowed channels configured.</div>`;
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
              <select data-action="legitlibs-max-tier" data-cid="${esc(ch.channel_id)}" style="width:60px;">
                ${tierOptions(ch.legitlibs_max_tier)}
              </select>
            </td>
            <td><button class="btn" style="padding:2px 6px;font-size:12px;" data-action="remove-channel" data-cid="${esc(ch.channel_id)}">Remove</button></td>
          </tr>`;
        }
        el.innerHTML = `<table style="width:100%;max-width:560px;">
          <thead><tr><th>Channel</th><th>Added</th><th title="Highest LegitLibs heat tier allowed in this channel">LegitLibs Max Tier</th><th style="width:80px;"></th></tr></thead>
          <tbody>${rows}</tbody>
        </table>`;

        el.querySelectorAll('[data-action="remove-channel"]').forEach((btn) => {
          btn.addEventListener("click", async () => {
            const cid = btn.dataset.cid;
            const label = channelName(guildChannels, cid);
            if (!(await confirmDialog(`Remove ${label} from the allowed game channels?`, { danger: true, confirmLabel: "Remove" }))) return;
            try {
              await apiDelete(`/api/games/config/channels/${encodeURIComponent(cid)}`);
              loadAllowedChannels();
            } catch (err) { toast(`Remove failed: ${err.message}`, "error"); }
          });
        });

        el.querySelectorAll('[data-action="legitlibs-max-tier"]').forEach((sel) => {
          sel.addEventListener("change", async () => {
            const cid = sel.dataset.cid;
            try {
              await apiPut(`/api/games/config/channels/${encodeURIComponent(cid)}/legitlibs-max-tier`, {
                max_tier: parseInt(sel.value, 10),
              });
              toast("Saved");
            } catch (err) { toast(`Save failed: ${err.message}`, "error"); }
          });
        });
      } catch (err) {
        el.innerHTML = `<div class="empty">Error: ${esc(err.message)}</div>`;
      }
    }

    async function loadEditorRole() {
      const el = region("editor-role-current");
      try {
        const data = await api("/api/games/config/editor-role");
        if (data?.role_id) {
          el.innerHTML = `<div>Current: ${esc(roleName(roles, data.role_id))}</div>`;
          ctrl("editor-role").value = data.role_id;
        } else {
          el.innerHTML = `<div class="empty">No host role set — admins only.</div>`;
          ctrl("editor-role").value = "0";
        }
      } catch (err) {
        el.innerHTML = `<div class="empty">Error: ${esc(err.message)}</div>`;
      }
    }

    async function loadAudit() {
      const el = region("audit-current");
      try {
        const data = await api("/api/games/config/audit");
        if (!data) {
          el.innerHTML = `<div class="empty">No audit channel set.</div>`;
        } else {
          el.innerHTML = `<div>Current: ${esc(channelName(guildChannels, data.channel_id))}</div>`;
          ctrl("audit-channel").value = data.channel_id;
        }
      } catch (err) {
        el.innerHTML = `<div class="empty">Error: ${esc(err.message)}</div>`;
      }
    }

    container.querySelector('[data-action="save-editor-role"]').addEventListener("click", async () => {
      const st = container.querySelector('[data-status="editor-role"]');
      const rid = ctrl("editor-role").value;
      try {
        if (!rid || rid === "0") {
          await apiDelete("/api/games/config/editor-role");
          showStatus(st, true, "Cleared — admins only");
        } else {
          await apiPut("/api/games/config/editor-role", { role_id: rid });
          showStatus(st, true, "Saved");
        }
        loadEditorRole();
      } catch (err) { showStatus(st, false, err.message); }
    });

    container.querySelector('[data-action="add-channel"]').addEventListener("click", async () => {
      const st = container.querySelector('[data-status="channel"]');
      const cid = ctrl("new-channel").value;
      if (!cid) { showStatus(st, false, "Pick a channel first"); return; }
      try {
        await apiPost("/api/games/config/channels", { channel_id: cid });
        ctrl("new-channel").value = "";
        showStatus(st, true, "Added");
        loadAllowedChannels();
      } catch (err) { showStatus(st, false, err.message); }
    });

    container.querySelector('[data-action="save-audit"]').addEventListener("click", async () => {
      const st = container.querySelector('[data-status="audit"]');
      const cid = ctrl("audit-channel").value;
      if (!cid) { showStatus(st, false, "Pick a channel first"); return; }
      try {
        await apiPut("/api/games/config/audit", { channel_id: cid });
        showStatus(st, true, "Saved");
        loadAudit();
      } catch (err) { showStatus(st, false, err.message); }
    });

    loadAllowedChannels();
    loadEditorRole();
    loadAudit();
  })();

  return { unmount() {} };
}
