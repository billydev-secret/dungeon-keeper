import { loadConfig, loadChannels, loadCategories, loadRoles, channelSelect, categorySelect, roleSelect, roleSelectMulti, apiPut, showStatus } from "../config-helpers.js";
import { confirmDialog } from "../ui.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading config…</div></div>`;

  (async () => {
    const [config, channels, categories, roles] = await Promise.all([loadConfig(), loadChannels(), loadCategories(), loadRoles()]);
    const m = config.moderation;
    let currentStorage = (config.privacy && config.privacy.message_storage_level) || "none";

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Moderation</h2>
          <div class="subtitle">Jail, ticket, warning, and logging settings</div>
        </header>
        <form class="form" data-form>
          <div class="field">
            <label>Jailed Role</label>
            <select name="jailed_role_id">${roleSelect(roles, m.jailed_role_id)}</select>
            <div class="field-hint">Role assigned to jailed members</div>
          </div>
          <div class="field">
            <label>Jail Category</label>
            <select name="jail_category_id">${categorySelect(categories, m.jail_category_id)}</select>
            <div class="field-hint">Discord category where jail channels are created</div>
          </div>
          <div class="field">
            <label>Ticket Category</label>
            <select name="ticket_category_id">${categorySelect(categories, m.ticket_category_id)}</select>
            <div class="field-hint">Discord category where ticket channels are created</div>
          </div>
          <div class="field">
            <label>Log Channel</label>
            <select name="log_channel_id">${channelSelect(channels, m.log_channel_id)}</select>
            <div class="field-hint">Channel for moderation log messages</div>
          </div>
          <div class="field">
            <label>Transcript Channel</label>
            <select name="transcript_channel_id">${channelSelect(channels, m.transcript_channel_id)}</select>
            <div class="field-hint">Where transcripts are posted (falls back to log channel if empty)</div>
          </div>
          <div class="field">
            <label>Mod Roles</label>
            <select name="mod_role_ids" multiple size="6">${roleSelectMulti(roles, m.mod_role_ids)}</select>
            <div class="field-hint">Roles granted moderator permissions (Ctrl/Cmd-click to select multiple)</div>
          </div>
          <div class="field">
            <label>Admin Roles</label>
            <select name="admin_role_ids" multiple size="6">${roleSelectMulti(roles, m.admin_role_ids)}</select>
            <div class="field-hint">Roles granted admin permissions — can escalate tickets (Ctrl/Cmd-click to select multiple)</div>
          </div>
          <div class="field">
            <label>Notify on Ticket Create</label>
            <select name="ticket_notify_on_create">
              <option value="1" ${m.ticket_notify_on_create === "1" ? "selected" : ""}>Yes</option>
              <option value="0" ${m.ticket_notify_on_create === "0" ? "selected" : ""}>No</option>
            </select>
          </div>
          <div class="field">
            <label>Warning Threshold</label>
            <input type="number" name="warning_threshold" min="1" max="99" value="${m.warning_threshold}" />
            <div class="field-hint">Number of active warnings before auto-action</div>
          </div>
          <div class="field">
            <label>Message Content Storage</label>
            <select name="message_storage_level">
              <option value="none" ${currentStorage === "none" ? "selected" : ""}>None — don't store message text (default)</option>
              <option value="all" ${currentStorage === "all" ? "selected" : ""}>All — archive full message content</option>
            </select>
            <div class="field-hint">Whether message text, attachments, and embeds are kept. XP, sentiment, and activity stats are always retained either way. Switching to <strong>None</strong> permanently erases this server's already-stored message content.</div>
          </div>
          <div><button type="submit" class="btn btn-primary">Save</button><span data-status></span></div>
        </form>
      </div>
    `;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");
    const collectMulti = (name) =>
      Array.from(form.querySelector(`select[name="${name}"]`).selectedOptions)
        .map((o) => o.value)
        .join(",");

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      // Switching storage to "none" permanently purges stored content —
      // never let a routine Save do that without an explicit confirmation.
      if (fd.get("message_storage_level") === "none" && currentStorage !== "none") {
        const ok = await confirmDialog(
          "Switching Message Content Storage to \"None\" permanently erases all of this server's already-stored message content.\nThis cannot be undone.",
          { title: "Erase stored messages?", confirmLabel: "Erase & Save", danger: true }
        );
        if (!ok) return;
      }
      try {
        await apiPut("/api/config/moderation", {
          jailed_role_id: fd.get("jailed_role_id"),
          jail_category_id: fd.get("jail_category_id") || "0",
          ticket_category_id: fd.get("ticket_category_id") || "0",
          log_channel_id: fd.get("log_channel_id"),
          transcript_channel_id: fd.get("transcript_channel_id"),
          mod_role_ids: collectMulti("mod_role_ids"),
          admin_role_ids: collectMulti("admin_role_ids"),
          ticket_notify_on_create: fd.get("ticket_notify_on_create"),
          warning_threshold: parseInt(fd.get("warning_threshold")) || 3,
        });
        // Storage level uses a dedicated endpoint (switching to "none" purges
        // existing content). Only call it when the value actually changed so a
        // routine moderation save doesn't re-trigger the purge.
        const newStorage = fd.get("message_storage_level");
        let note = "";
        if (newStorage !== currentStorage) {
          const res = await apiPut("/api/config/privacy", { message_storage_level: newStorage });
          if (newStorage === "none" && res && res.purged > 0) {
            note = ` — erased ${res.purged} stored message${res.purged === 1 ? "" : "s"}`;
          }
          currentStorage = newStorage;
        }
        showStatus(status, true, "Saved" + note);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
