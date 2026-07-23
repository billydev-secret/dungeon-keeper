import {
  loadConfig,
  loadChannels,
  loadCategories,
  loadRoles,
  apiPut,
  showStatus,
  guardForm,
  mountChannelPicker,
  mountCategoryPicker,
  mountRolePicker,
  mountRoleMultiPicker,
} from "../config-helpers.js";
import { confirmDialog } from "../ui.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading configuration…</div></div>`;

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
        <form class="form form-cards" data-form>
          <div class="card">
            <div class="section-label">Staff Roles</div>
            <div class="field">
              <label>Moderator Roles</label>
              <span data-picker="mod_role_ids"></span>
              <div class="field-hint">Members with any of these roles get moderator powers — jailing, tickets, warnings, and the moderator pages of this dashboard.</div>
            </div>
            <div class="field">
              <label>Admin Roles</label>
              <span data-picker="admin_role_ids"></span>
              <div class="field-hint">Members with any of these roles get admin powers — everything moderators can do, plus escalated tickets and every settings page here.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Jail</div>
            <div class="field">
              <label>Jailed Role</label>
              <span data-picker="jailed_role_id"></span>
              <div class="field-hint">Role assigned to jailed members; your channel permissions should hide the rest of the server from it.</div>
            </div>
            <div class="field">
              <label>Jail Category</label>
              <span data-picker="jail_category_id"></span>
              <div class="field-hint">Discord category where each jailed member's private channel is created.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Tickets</div>
            <div class="field">
              <label>Ticket Category</label>
              <span data-picker="ticket_category_id"></span>
              <div class="field-hint">Discord category where ticket channels are created.</div>
            </div>
            <div class="field">
              <label style="display:flex; gap:6px; align-items:center;">
                <input type="checkbox" name="ticket_notify_on_create"${m.ticket_notify_on_create === "1" ? " checked" : ""} />
                Notify moderators when a ticket is opened
              </label>
              <div class="field-hint">Posts a ping in the log channel the moment a member opens a ticket, so it isn't spotted late.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Warnings</div>
            <div class="field">
              <label for="mod-warning-threshold">Warning Threshold</label>
              <input type="number" name="warning_threshold" id="mod-warning-threshold" required min="1" max="99" step="1" value="${m.warning_threshold}" style="max-width:120px;" />
              <div class="field-hint">A member reaching this many active warnings triggers the automatic action (1–99).</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Logging</div>
            <div class="field">
              <label>Log Channel</label>
              <span data-picker="log_channel_id"></span>
              <div class="field-hint">Channel where moderation actions are logged.</div>
            </div>
            <div class="field">
              <label>Transcript Channel</label>
              <span data-picker="transcript_channel_id"></span>
              <div class="field-hint">Where jail and ticket transcripts are posted. Falls back to the log channel when unset.</div>
            </div>
          </div>

          <div class="card" style="border-color: var(--red);">
            <div class="section-label" style="color: var(--red);">Danger Zone — Message Content Storage</div>
            <div class="field">
              <label for="mod-storage-level">Message Content Storage</label>
              <select name="message_storage_level" id="mod-storage-level">
                <option value="none" ${currentStorage === "none" ? "selected" : ""}>None — don't store message text (default)</option>
                <option value="all" ${currentStorage === "all" ? "selected" : ""}>All — archive full message content</option>
              </select>
              <div class="field-hint">Whether message text, attachments, and embeds are kept. XP, sentiment, and activity stats are always retained either way. Switching to <strong>None</strong> permanently erases this server's already-stored message content — you will be asked to confirm.</div>
            </div>
          </div>

          <div><button type="submit" class="btn btn-primary">Save</button><span data-status></span></div>
        </form>
      </div>
    `;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");

    const jailedRolePicker = mountRolePicker(
      form.querySelector('[data-picker="jailed_role_id"]'),
      roles, String(m.jailed_role_id || "0"), { label: "Jailed Role" },
    );
    const jailCategoryPicker = mountCategoryPicker(
      form.querySelector('[data-picker="jail_category_id"]'),
      categories, String(m.jail_category_id || "0"), { label: "Jail Category" },
    );
    const ticketCategoryPicker = mountCategoryPicker(
      form.querySelector('[data-picker="ticket_category_id"]'),
      categories, String(m.ticket_category_id || "0"), { label: "Ticket Category" },
    );
    const logChannelPicker = mountChannelPicker(
      form.querySelector('[data-picker="log_channel_id"]'),
      channels, String(m.log_channel_id || "0"), { label: "Log Channel" },
    );
    const transcriptChannelPicker = mountChannelPicker(
      form.querySelector('[data-picker="transcript_channel_id"]'),
      channels, String(m.transcript_channel_id || "0"), { label: "Transcript Channel" },
    );
    const modRolesPicker = mountRoleMultiPicker(
      form.querySelector('[data-picker="mod_role_ids"]'),
      roles, m.mod_role_ids, { label: "Moderator Roles" },
    );
    const adminRolesPicker = mountRoleMultiPicker(
      form.querySelector('[data-picker="admin_role_ids"]'),
      roles, m.admin_role_ids, { label: "Admin Roles" },
    );

    guardForm(form);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const threshold = parseInt(fd.get("warning_threshold"), 10);
      if (!Number.isFinite(threshold) || threshold < 1 || threshold > 99) {
        showStatus(status, false, "Warning Threshold must be a number from 1 to 99");
        form.querySelector("[name=warning_threshold]").focus();
        return;
      }
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
          // Ids stay strings; multi-pickers serialize to the same
          // comma-joined string the old multi-selects posted.
          jailed_role_id: jailedRolePicker.getValue() || "0",
          jail_category_id: jailCategoryPicker.getValue() || "0",
          ticket_category_id: ticketCategoryPicker.getValue() || "0",
          log_channel_id: logChannelPicker.getValue() || "0",
          transcript_channel_id: transcriptChannelPicker.getValue() || "0",
          mod_role_ids: modRolesPicker.getValues().join(","),
          admin_role_ids: adminRolesPicker.getValues().join(","),
          ticket_notify_on_create: fd.has("ticket_notify_on_create") ? "1" : "0",
          warning_threshold: threshold,
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
