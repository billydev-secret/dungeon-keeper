import {
  loadConfig, loadChannels,
  channelSelectMulti,
  apiPost, apiPut, showStatus, esc,
} from "../config-helpers.js";

const MODELS = [
  { value: "base.en", label: "base.en  (recommended — more accurate)" },
  { value: "tiny.en", label: "tiny.en  (faster, lower accuracy)" },
];

function modelSelect(selected) {
  const sel = selected || "base.en";
  return MODELS.map(m =>
    `<option value="${m.value}" ${sel === m.value ? "selected" : ""}>${m.label}</option>`
  ).join("");
}

// Whisper model files live in a repo-local cache the bot loads offline; a model
// that isn't cached can't be used until it's fetched. Render one row per model
// with its cache state and a Download button for any that are missing.
function modelsWidget(models) {
  const rows = (models || []).map(m => `
    <div class="vt-model-row" data-model="${esc(m.name)}"
         style="display:flex;align-items:center;gap:.6rem;padding:.35rem 0">
      <code style="min-width:5.5rem">${esc(m.name)}</code>
      <span class="vt-model-state">${
        m.cached
          ? `<span style="color:var(--ok,#3a3)">✓ downloaded</span>`
          : `<span style="color:var(--warn,#c80)">not downloaded</span>`
      }</span>
      <span style="flex:1"></span>
      <button type="button" class="btn btn-sm vt-dl-btn"
              ${m.cached ? "disabled" : ""}>Download</button>
    </div>`).join("");

  return `
    <div class="field">
      <label>Model files</label>
      <div class="vt-models">${rows}</div>
      <div class="field-hint">
        Models are downloaded once into the bot's local cache and then run
        fully offline. A model must show <em>downloaded</em> before it can be
        selected above. Downloading base.en fetches ~150&nbsp;MB and can take a
        minute on a slow host.
      </div>
    </div>`;
}

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading…</div></div>`;

  (async () => {
    const [config, channels] = await Promise.all([loadConfig(), loadChannels()]);
    const vt = config.voice_transcription || {};

    const unavailable = vt.available === false
      ? `<div class="field-hint" style="color:var(--warn,#c80)">
           ⚠ faster-whisper isn't installed on the bot host, so transcription is
           inactive even when enabled. Run <code>pip install faster-whisper</code>
           and restart the bot.
         </div>`
      : "";

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Voice Transcription</h2>
          <div class="subtitle">Auto-transcribes Discord voice notes with a local Whisper model</div>
        </header>
        <form class="form" data-form>

          ${unavailable}

          <div class="field">
            <label>Enabled</label>
            <select name="enabled">
              <option value="1" ${vt.enabled ? "selected" : ""}>On</option>
              <option value="0" ${!vt.enabled ? "selected" : ""}>Off</option>
            </select>
          </div>

          <div class="field">
            <label>Model</label>
            <select name="model_name">${modelSelect(vt.model_name)}</select>
            <div class="field-hint">base.en is more accurate; tiny.en is faster on slow hosts.</div>
          </div>

          ${vt.available === false ? "" : modelsWidget(vt.models)}

          <div class="field">
            <label>Channels</label>
            <select name="channel_ids" multiple size="8">${channelSelectMulti(channels, vt.channel_ids)}</select>
            <div class="field-hint">Only transcribe voice notes posted in these channels (Ctrl/Cmd-click to select multiple). Leave empty to transcribe in every channel.</div>
          </div>

          <div><button type="submit" class="btn btn-primary">Save</button><span data-status></span></div>
        </form>
      </div>
    `;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");

    // Per-model download buttons.
    container.querySelectorAll(".vt-model-row").forEach((row) => {
      const btn = row.querySelector(".vt-dl-btn");
      const state = row.querySelector(".vt-model-state");
      const modelName = row.dataset.model;
      btn?.addEventListener("click", async () => {
        btn.disabled = true;
        const prev = state.innerHTML;
        state.innerHTML = `<span style="color:var(--muted,#888)">downloading…</span>`;
        try {
          const res = await apiPost("/api/config/voice-transcription/download", { model_name: modelName });
          if (res.cached) {
            state.innerHTML = `<span style="color:var(--ok,#3a3)">✓ downloaded</span>`;
          } else {
            state.innerHTML = prev;
            btn.disabled = false;
          }
        } catch (err) {
          state.innerHTML = `<span style="color:var(--warn,#c80)">${esc(err.message || "download failed")}</span>`;
          btn.disabled = false;
        }
      });
    });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      try {
        await apiPut("/api/config/voice-transcription", {
          enabled: fd.get("enabled") === "1",
          model_name: fd.get("model_name"),
          channel_ids: Array.from(
            form.querySelector('select[name="channel_ids"]').selectedOptions
          ).map((o) => o.value),
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
