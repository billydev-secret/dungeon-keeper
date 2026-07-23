import {
  loadConfig,
  loadChannels,
  loadRoles,
  apiPut,
  showStatus,
  guardForm,
  renderMetaWarning,
  mountChannelPicker,
  mountRolePicker,
} from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading configuration…</div></div>`;

  (async () => {
    const [config, channels, roles] = await Promise.all([
      loadConfig(),
      loadChannels(),
      loadRoles(),
    ]);
    const w = config.whisper;

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Whisper</h2>
          <div class="subtitle">Lets members send each other anonymous notes through Dungeon Keeper</div>
        </header>
        ${renderMetaWarning()}
        <form class="form form-cards" data-form>
          <div class="card">
            <div class="section-label">Where Whispers Go</div>
            <div class="field">
              <label>Whisper Channel</label>
              <span data-picker="channel_id"></span>
              <div class="field-hint">Whispers are posted here without naming the
                sender. Whisper does nothing until this is set — "(disabled)" turns the
                whole feature off.</div>
            </div>
            <div class="field">
              <label>Log Channel</label>
              <span data-picker="log_channel_id"></span>
              <div class="field-hint">A moderator-only record naming who sent each
                whisper, so abuse can be traced. "(disabled)" means nobody, including
                moderators, can find out who sent a whisper.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Who Can Whisper</div>
            <div class="field">
              <label>Required Role</label>
              <span data-picker="role_id"></span>
              <div class="field-hint">Only members holding this role may send
                whispers. "(none)" lets everyone in the server send them.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Rate Limits</div>
            <div class="field">
              <label for="wh-cooldown">Wait Between Whispers (seconds)</label>
              <input type="number" name="cooldown_seconds" id="wh-cooldown" required
                min="0" max="86400" step="1" value="${w.cooldown_seconds ?? 30}"
                style="max-width:140px;" />
              <div class="field-hint">How long a member must wait after sending one
                whisper before they can send another. 0 removes the wait entirely,
                which makes spamming easy.</div>
            </div>
            <div class="field">
              <label for="wh-cap">Whispers Per Hour to the Same Person</label>
              <input type="number" name="hourly_cap_per_target" id="wh-cap" required
                min="1" max="1000" step="1" value="${w.hourly_cap_per_target ?? 5}"
                style="max-width:140px;" />
              <div class="field-hint">The most whispers one member may send to the same
                recipient within an hour. Keep this low — it is the main protection
                against anonymous harassment.</div>
            </div>
          </div>

          <div style="display:flex; gap:8px; align-items:center;">
            <button type="submit" class="btn btn-primary">Save</button>
            <span data-status></span>
          </div>
        </form>
      </div>
    `;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");

    const channelPicker = mountChannelPicker(
      form.querySelector('[data-picker="channel_id"]'),
      channels, String(w.channel_id || "0"), { label: "Whisper Channel" },
    );
    const logChannelPicker = mountChannelPicker(
      form.querySelector('[data-picker="log_channel_id"]'),
      channels, String(w.log_channel_id || "0"), { label: "Log Channel" },
    );
    const rolePicker = mountRolePicker(
      form.querySelector('[data-picker="role_id"]'),
      roles, String(w.role_id || "0"), { label: "Required Role" },
    );

    guardForm(form);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);

      const cooldown = parseInt(fd.get("cooldown_seconds"), 10);
      if (!Number.isFinite(cooldown) || cooldown < 0 || cooldown > 86400) {
        showStatus(status, false, "Wait Between Whispers must be a number of seconds from 0 to 86400");
        form.querySelector("[name=cooldown_seconds]").focus();
        return;
      }
      const cap = parseInt(fd.get("hourly_cap_per_target"), 10);
      if (!Number.isFinite(cap) || cap < 1 || cap > 1000) {
        showStatus(status, false, "Whispers Per Hour to the Same Person must be a number from 1 to 1000");
        form.querySelector("[name=hourly_cap_per_target]").focus();
        return;
      }

      try {
        await apiPut("/api/config/whisper", {
          // Ids stay strings, same values the plain selects posted.
          channel_id: channelPicker.getValue() || "0",
          role_id: rolePicker.getValue() || "0",
          log_channel_id: logChannelPicker.getValue() || "0",
          cooldown_seconds: cooldown,
          hourly_cap_per_target: cap,
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
