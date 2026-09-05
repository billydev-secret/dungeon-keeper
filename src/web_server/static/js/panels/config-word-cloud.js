import {
  loadConfig,
  apiPut,
  showStatus,
  guardForm,
  renderMetaWarning,
  mountAsync,
  selectValueOrAdd,
} from "../config-helpers.js";

// The preset list and the cap bounds come from the API, which reads them from
// bot_modules/word_cloud/logic.py and presets.py — re-typing them here is how
// the ceiling ends up meaning three different things.
const HINTS = {
  midnight: "Dark background, clean sans.",
  parchment: "Warm paper, serif.",
  meadow: "Light and green, condensed.",
  neon: "Near-black with bright colours.",
  notebook: "Off-white, handwritten.",
};

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading settings…</div></div>`;

  return mountAsync(
    container,
    async () => {
      const config = await loadConfig();
      const wc = config.word_cloud || {};
      const presets = wc.presets || [];
      const CAP_MIN = Number(wc.min_cap ?? 100);
      const CAP_MAX = Number(wc.max_cap ?? 12000);
      const cap = Number(wc.message_cap ?? CAP_MAX);
      const current = wc.default_preset || "";

      const presetOptions = presets
        .map((p) => `<option value="${p.key}">${p.label}</option>`)
        .join("");

      container.innerHTML = `
        <div class="panel">
          <header>
            <h2>Word Cloud</h2>
            <div class="subtitle">Settings for the moderator <code>/wordcloud</code> command</div>
          </header>
          ${renderMetaWarning()}
          <form class="form form-cards" data-form>
            <div class="card">
              <div class="section-label">How Much To Read</div>
              <div class="field">
                <label for="wordcloud-cap">Message Limit</label>
                <input type="number" data-field="message_cap" min="${CAP_MIN}" max="${CAP_MAX}" step="100" value="${cap}" id="wordcloud-cap">
                <div class="field-hint">The most messages one cloud will read, between
                  ${CAP_MIN} and ${CAP_MAX.toLocaleString()}. When a moderator asks for a
                  window holding more than this, the most recent ones are used and the
                  reply says so. Lower it if clouds feel slow; there is little to see
                  past a few thousand messages either way.</div>
              </div>
            </div>

            <div class="card">
              <div class="section-label">Look</div>
              <div class="field">
                <label for="wordcloud-preset">Default Style</label>
                <select data-field="default_preset" id="wordcloud-preset">${presetOptions}</select>
                <div class="field-hint" data-preset-hint></div>
              </div>
              <div class="field">
                <div class="field-hint">A moderator can pick a different style, or turn
                  off mood colouring, on any single cloud — this is only what they get
                  when they don't choose. Mood colouring tints each word by how happy the
                  messages using it were, and needs stored message history; servers
                  without it fall back to the style's own palette.</div>
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
      const presetSelect = form.querySelector('[data-field="default_preset"]');
      const presetHint = form.querySelector("[data-preset-hint]");

      // Never silently drop a stored key this build doesn't know: without
      // this the select falls back to the first option and the next Save
      // overwrites whatever was really configured.
      selectValueOrAdd(presetSelect, current, (v) => v);

      const syncPresetHint = () => {
        presetHint.textContent = HINTS[presetSelect.value] || "";
      };
      syncPresetHint();
      presetSelect.addEventListener("change", syncPresetHint);

      guardForm(form);

      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        // Number("") is 0, which would silently save the floor instead of
        // telling the admin they blanked the field.
        const raw = String(
          form.querySelector('[data-field="message_cap"]').value ?? "",
        ).trim();
        const value = Number(raw);
        if (
          raw === "" ||
          !Number.isInteger(value) ||
          value < CAP_MIN ||
          value > CAP_MAX
        ) {
          showStatus(
            status,
            false,
            `Message Limit must be a whole number between ${CAP_MIN} and ${CAP_MAX.toLocaleString()}.`,
          );
          form.querySelector('[data-field="message_cap"]').focus();
          return;
        }
        try {
          await apiPut("/api/config/word-cloud", {
            message_cap: value,
            default_preset: presetSelect.value,
          });
          showStatus(status, true);
        } catch (err) {
          showStatus(status, false, err.message);
        }
      });
    },
    { errorMsg: "Couldn’t load the Word Cloud settings." },
  );
}
