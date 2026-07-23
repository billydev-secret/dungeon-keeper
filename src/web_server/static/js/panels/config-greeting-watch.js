import {
  loadConfig,
  loadChannels,
  loadMembers,
  mountMemberMultiPicker,
  mountChannelMultiPicker,
  apiPut,
  showStatus,
  guardForm,
  renderMetaWarning,
} from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading configuration…</div></div>`;

  (async () => {
    const [config, channels, members] = await Promise.all([
      loadConfig(),
      loadChannels(),
      loadMembers(),
    ]);
    const g = config.greeting_watch || {
      enabled: false, channel_ids: "", notify_user_ids: [], window_minutes: 10,
    };

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Greeting Watch</h2>
          <div class="subtitle">Catch “good morning” / “hello” messages that go unanswered, so nobody falls through the cracks</div>
        </header>
        ${renderMetaWarning()}
        <form class="form" data-form>
          <div class="field">
            <label><input type="checkbox" name="enabled" ${g.enabled ? "checked" : ""} /> Enable Greeting Watch</label>
            <div class="field-hint">When on, greetings posted in the watched channels are tracked. If nobody replies to or @mentions the greeter within the window, the notify members each get a DM with a jump link.</div>
          </div>
          <div class="field">
            <label>Watched Channels</label>
            <span data-picker="channels"></span>
            <div class="field-hint">Your “main chat” channel(s) to watch. Nothing selected means nothing is watched.</div>
          </div>
          <div class="field">
            <label>Notify (DM) These Members</label>
            <span data-picker="notify_users"></span>
            <div class="field-hint">Everyone selected gets the direct message when a greeting goes unanswered. Add as many as you like; leave it empty and nothing is sent.</div>
          </div>
          <div class="field">
            <label>Unanswered Window (minutes)</label>
            <input type="number" name="window_minutes" min="1" max="180" value="${g.window_minutes}" required />
            <div class="field-hint">How long to wait for a reply or @mention before flagging the greeting as unanswered.</div>
          </div>
          <div style="display:flex; gap:8px; align-items:center;">
            <button type="submit" class="btn btn-primary">Save</button>
            <span data-status></span>
          </div>
        </form>
      </div>
    `;

    const channelsPicker = mountChannelMultiPicker(
      container.querySelector('[data-picker="channels"]'),
      channels,
      String(g.channel_ids || "").split(",").filter(Boolean),
      { label: "Watched Channels" },
    );
    const notifyPicker = mountMemberMultiPicker(
      container.querySelector('[data-picker="notify_users"]'),
      members,
      g.notify_user_ids || [],
      { label: "Notify Members" },
    );

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");
    guardForm(form);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const window_minutes = parseInt(
        form.querySelector('input[name="window_minutes"]').value, 10,
      );
      if (!Number.isFinite(window_minutes) || window_minutes < 1 || window_minutes > 180) {
        showStatus(status, false, "Unanswered Window must be between 1 and 180 minutes.");
        return;
      }
      try {
        // Ids stay strings; both multi-pickers serialize to the same
        // comma-joined string the server's CSV fields expect.
        await apiPut("/api/config/greeting-watch", {
          enabled: form.querySelector('input[name="enabled"]').checked,
          channel_ids: channelsPicker.getValues().join(","),
          notify_user_ids: notifyPicker.getValues().join(","),
          window_minutes,
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
