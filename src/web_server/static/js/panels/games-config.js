import { api, apiPost, esc } from "../api.js";
import { apiPut, apiDelete, showStatus } from "../config-helpers.js";

// All user-supplied content rendered via innerHTML uses esc() for XSS safety.

export function mount(container) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Games Config</h2>
        <div class="subtitle">Configure which channels can run games and where audit logs are sent.</div>
      </header>

      <section>
        <div class="section-label">Allowed Channels</div>
        <div class="field-hint">Only these channel IDs may host party games. Use the Discord channel ID (a large number).</div>
        <div data-region="channels-list" style="margin-bottom:10px;"><div class="empty">Loading</div></div>
        <div class="form" style="display:flex;flex-direction:row;gap:8px;align-items:flex-end;max-width:none;">
          <div class="field" style="margin:0;flex:1;max-width:280px;">
            <label>Channel ID
              <input type="text" data-ctrl="new-channel" placeholder="e.g. 123456789012345678" style="width:100%;" />
            </label>
          </div>
          <button class="btn btn-primary" data-action="add-channel">Add</button>
          <span data-status="channel" class="save-status" style="margin-left:4px;"></span>
        </div>
      </section>

      <section>
        <div class="section-label">Audit Channel</div>
        <div class="field-hint">When set, game events are logged to this channel.</div>
        <div data-region="audit-current" style="margin-bottom:10px;"><div class="empty">Loading</div></div>
        <div class="form" style="display:flex;flex-direction:row;gap:8px;align-items:flex-end;max-width:none;">
          <div class="field" style="margin:0;flex:1;max-width:280px;">
            <label>Channel ID
              <input type="text" data-ctrl="audit-channel" placeholder="e.g. 123456789012345678" style="width:100%;" />
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

  async function loadChannels() {
    const el = region("channels-list");
    try {
      const data = await api("/api/games/config/channels");
      const channels = data.channels || [];
      if (!channels.length) {
        el.innerHTML = `<div class="empty">No allowed channels configured.</div>`;
        return;
      }
      let rows = "";
      for (const ch of channels) {
        const added = ch.added_at ? String(ch.added_at).slice(0, 10) : "";
        rows += `<tr>
          <td style="font-family:monospace;">${esc(ch.channel_id)}</td>
          <td style="font-size:12px;">${esc(added)}</td>
          <td><button class="btn" style="padding:2px 6px;font-size:12px;" data-action="remove-channel" data-cid="${esc(ch.channel_id)}">Remove</button></td>
        </tr>`;
      }
      el.innerHTML = `<table style="width:100%;max-width:500px;">
        <thead><tr><th>Channel ID</th><th>Added</th><th style="width:80px;"></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;

      el.querySelectorAll('[data-action="remove-channel"]').forEach((btn) => {
        btn.addEventListener("click", async () => {
          const cid = btn.dataset.cid;
          if (!confirm(`Remove channel ${cid}?`)) return;
          try {
            await apiDelete(`/api/games/config/channels/${encodeURIComponent(cid)}`);
            loadChannels();
          } catch (err) { alert(`Remove failed: ${err.message}`); }
        });
      });
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
        el.innerHTML = `<div>Current: <code>${esc(data.channel_id)}</code> (guild ${esc(data.guild_id)})</div>`;
        ctrl("audit-channel").value = data.channel_id;
      }
    } catch (err) {
      el.innerHTML = `<div class="empty">Error: ${esc(err.message)}</div>`;
    }
  }

  container.querySelector('[data-action="add-channel"]').addEventListener("click", async () => {
    const st = container.querySelector('[data-status="channel"]');
    const cid = ctrl("new-channel").value.trim();
    if (!cid) { showStatus(st, false, "Channel ID required"); return; }
    try {
      await apiPost("/api/games/config/channels", { channel_id: cid });
      ctrl("new-channel").value = "";
      showStatus(st, true, "Added");
      loadChannels();
    } catch (err) { showStatus(st, false, err.message); }
  });

  container.querySelector('[data-action="save-audit"]').addEventListener("click", async () => {
    const st = container.querySelector('[data-status="audit"]');
    const cid = ctrl("audit-channel").value.trim();
    if (!cid) { showStatus(st, false, "Channel ID required"); return; }
    try {
      await apiPut("/api/games/config/audit", { channel_id: cid });
      showStatus(st, true, "Saved");
      loadAudit();
    } catch (err) { showStatus(st, false, err.message); }
  });

  ctrl("new-channel").addEventListener("keydown", (e) => {
    if (e.key === "Enter") container.querySelector('[data-action="add-channel"]').click();
  });

  loadChannels();
  loadAudit();

  return { unmount() {} };
}