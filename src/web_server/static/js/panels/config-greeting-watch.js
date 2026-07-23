import { loadConfig, loadChannels, loadMembers, channelSelectMulti, mountMemberMultiPicker, apiPut, showStatus } from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading config…</div></div>`;

  (async () => {
    const [config, channels, members] = await Promise.all([loadConfig(), loadChannels(), loadMembers()]);
    const g = config.greeting_watch || { enabled: false, channel_ids: "", notify_user_ids: [], window_minutes: 10 };

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Greeting Watch</h2>
          <div class="subtitle">Catch “good morning” / “hello” messages that go unanswered, so nobody falls through the cracks</div>
        </header>
        <form class="form" data-form>
          <div class="field">
            <label><input type="checkbox" name="enabled" ${g.enabled ? "checked" : ""} /> Enable greeting watch</label>
            <div class="field-hint">When on, greetings posted in the watched channels are tracked. If nobody replies to or @mentions the greeter within the window, the notify member gets a DM with a jump link.</div>
          </div>
          <div class="field">
            <label>Watched channels</label>
            <select name="channel_ids" multiple size="8">${channelSelectMulti(channels, g.channel_ids)}</select>
            <div class="field-hint">Your “main chat” channel(s) to watch. Ctrl/Cmd-click to select several. Nothing selected = nothing is watched.</div>
          </div>
          <div class="field">
            <label>Notify (DM) these members</label>
            <span data-picker="notify_users"></span>
            <div class="field-hint">Everyone selected gets the direct message when a greeting goes unanswered. Add as many as you like; leave empty and nothing is sent.</div>
          </div>
          <div class="field">
            <label>Unanswered window (minutes)</label>
            <input type="number" name="window_minutes" min="1" max="180" value="${g.window_minutes}" />
            <div class="field-hint">How long to wait for a reply or @mention before flagging the greeting as unanswered.</div>
          </div>
          <div><button type="submit" class="btn btn-primary">Save</button><span data-status></span></div>
        </form>
      </div>
    `;

    const notifyPicker = mountMemberMultiPicker(
      container.querySelector('[data-picker="notify_users"]'),
      members,
      g.notify_user_ids || [],
      { placeholder: "Search members…" },
    );

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");
    const collectMulti = (name) =>
      Array.from(form.querySelector(`select[name="${name}"]`).selectedOptions)
        .map((o) => o.value)
        .join(",");

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      try {
        await apiPut("/api/config/greeting-watch", {
          enabled: form.querySelector('input[name="enabled"]').checked,
          channel_ids: collectMulti("channel_ids"),
          notify_user_ids: notifyPicker.getValues().join(","),
          window_minutes: parseInt(fd.get("window_minutes")) || 10,
        });
        showStatus(status, true, "Saved");
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
