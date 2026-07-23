import {
  loadConfig,
  loadChannels,
  apiPut,
  showStatus,
  guardForm,
  renderMetaWarning,
  mountChannelPicker,
} from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading configuration…</div></div>`;

  (async () => {
    const [config, channels] = await Promise.all([loadConfig(), loadChannels()]);
    const rw = config.rules_watch || { enabled: false, channel_id: "0", guard_available: false };

    const guardBadge = rw.guard_available
      ? `<span class="badge badge-success">Ready</span>`
      : `<span class="badge badge-warning">Not set up</span>`;
    const guardHint = rw.guard_available
      ? "The local guard model is set up, so flagged messages are recorded as soon as monitoring is on."
      : "No local guard model is set up. Even with monitoring on, <strong>no messages will be flagged</strong> until you configure the model on the AI (Local LLM) page.";

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Rules Watch</h2>
          <div class="subtitle">A quiet AI second pair of eyes — it flags messages into a review queue and never acts on its own</div>
        </header>
        ${renderMetaWarning()}
        <form class="form form-cards" data-form>
          <div class="card">
            <div class="section-label">Monitoring</div>
            <div class="field">
              <label style="display:flex; gap:6px; align-items:center;">
                <input type="checkbox" name="enabled" ${rw.enabled ? "checked" : ""} />
                Screen public messages for rule breaks
              </label>
              <div class="field-hint">When checked, public messages are screened and
                anything flagged shows up under Moderation › Rules Watch for a
                moderator to label as a real violation or a false positive. Nothing is
                deleted and nobody is punished automatically. Checking this alone fills
                the queue — the alert channel below is optional. This also starts the
                <strong>Ledger</strong>, which records concrete direct-message consent
                and cross-platform events for review and needs no AI model at all.</div>
            </div>
            <div class="field">
              <label>Immediate Alert Channel</label>
              <span data-picker="channel_id"></span>
              <div class="field-hint">Optional. Only the most serious flags are posted
                here in Discord as they happen. Leave it "(disabled)" to collect
                everything quietly in the web queue instead.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Guard Model</div>
            <div class="field">
              <label>Local Guard Model: ${guardBadge}</label>
              <div class="field-hint">${guardHint}</div>
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
      channels,
      String(rw.channel_id || "0"),
      { label: "Immediate Alert Channel" },
    );

    guardForm(form);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await apiPut("/api/config/rules-watch", {
          enabled: form.querySelector('input[name="enabled"]').checked,
          channel_id: channelPicker.getValue() || "0",
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
