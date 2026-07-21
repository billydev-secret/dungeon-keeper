import { loadConfig, loadChannels, channelSelect, apiPut, showStatus } from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading config…</div></div>`;

  (async () => {
    const [config, channels] = await Promise.all([loadConfig(), loadChannels()]);
    const rw = config.rules_watch || { enabled: false, channel_id: "0", guard_available: false };

    const guardBadge = rw.guard_available
      ? `<span class="badge badge-success">available</span>`
      : `<span class="badge badge-warning">unreachable</span>`;
    const guardHint = rw.guard_available
      ? "The local guard model is configured, so flagged events will be recorded once monitoring is on."
      : "No local guard model is configured. Even with monitoring enabled, <strong>no events will be recorded</strong> until the AI (Local LLM) model is set up.";

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Rules Watch</h2>
          <div class="subtitle">Passive AI moderation monitor — flags messages into the review queue for labeling</div>
        </header>
        <form class="form" data-form>
          <div class="field">
            <label><input type="checkbox" name="enabled" ${rw.enabled ? "checked" : ""} /> Enable monitoring</label>
            <div class="field-hint">When on, public messages are screened and flagged events appear under Moderation → Rules Watch for you to label as violation / false positive. Enabling alone populates the queue — an alert channel is optional. This also runs the <strong>Ledger</strong> (concrete DM-consent and cross-platform acts, recorded silently for review), which needs no AI model.</div>
          </div>
          <div class="field">
            <label>Immediate Alert Channel</label>
            <select name="channel_id">${channelSelect(channels, rw.channel_id)}</select>
            <div class="field-hint">Optional. Only controls where <em>immediate</em>-tier alerts are posted in Discord. Leave as (disabled) to just collect events in the web queue.</div>
          </div>
          <div class="field">
            <label>Guard model: ${guardBadge}</label>
            <div class="field-hint">${guardHint}</div>
          </div>
          <div><button type="submit" class="btn btn-primary">Save</button><span data-status></span></div>
        </form>
      </div>
    `;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      try {
        await apiPut("/api/config/rules-watch", {
          enabled: form.querySelector('input[name="enabled"]').checked,
          channel_id: fd.get("channel_id") || "0",
        });
        showStatus(status, true, "Saved");
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
