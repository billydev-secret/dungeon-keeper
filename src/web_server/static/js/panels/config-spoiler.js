import {
  loadConfig,
  loadChannels,
  apiPut,
  showStatus,
  guardForm,
  renderMetaWarning,
  mountChannelMultiPicker,
} from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading configuration…</div></div>`;

  (async () => {
    const [config, channels] = await Promise.all([loadConfig(), loadChannels()]);
    const s = config.spoiler;

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Spoiler Guard</h2>
          <div class="subtitle">Channels where every image has to be posted as a spoiler</div>
        </header>
        ${renderMetaWarning()}
        <form class="form form-cards" data-form>
          <div class="card">
            <div class="section-label">Watched Channels</div>
            <div class="field">
              <label>Spoiler-Required Channels</label>
              <span data-picker="spoiler_required_channels"></span>
              <div class="field-hint">In these channels, an image posted without
                Discord's spoiler blur is removed and the poster is told why. Leave the
                list empty to switch the guard off everywhere. Members holding a bypass
                role (set on Global Settings) are never caught by this.</div>
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

    const picker = mountChannelMultiPicker(
      form.querySelector('[data-picker="spoiler_required_channels"]'),
      channels,
      s.spoiler_required_channels,
      { label: "Spoiler-Required Channels" },
    );

    guardForm(form);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        // Same payload shape as the old checkbox wall: a list of id strings.
        await apiPut("/api/config/spoiler", {
          spoiler_required_channels: picker.getValues(),
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
