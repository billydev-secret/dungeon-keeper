import { loadConfig, loadChannels, apiPut, showStatus } from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading config…</div></div>`;

  (async () => {
    const [config, channels] = await Promise.all([loadConfig(), loadChannels()]);
    const s = config.spoiler;
    const activeSet = new Set(s.spoiler_required_channels);

    container.innerHTML = `
      <div class="panel" style="overflow-y:auto;">
        <header>
          <h2>Spoiler Guard</h2>
          <div class="subtitle">Channels where all images must be spoilered</div>
        </header>
        <form class="config-form" data-form>
          <div class="field">
            <label>Spoiler-Required Channels</label>
            <div data-checkboxes style="max-height:300px; overflow-y:auto; background:var(--bg-sidebar); border-radius:4px; padding:8px;">
              ${channels.map((ch) => `
                <label style="display:flex; align-items:center; gap:6px; padding:3px 4px; font-size:13px; cursor:pointer;">
                  <input type="checkbox" name="channels" value="${ch.id}" ${activeSet.has(ch.id) ? "checked" : ""} />
                  #${ch.name}
                </label>
              `).join("")}
            </div>
          </div>
          <div><button type="submit">Save</button><span data-status></span></div>
        </form>
      </div>
    `;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const checked = [...form.querySelectorAll('input[name="channels"]:checked')].map((el) => el.value);
      try {
        await apiPut("/api/config/spoiler", {
          spoiler_required_channels: checked,
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
