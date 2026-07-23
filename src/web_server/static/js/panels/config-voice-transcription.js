import {
  loadConfig, loadChannels,
  mountChannelMultiPicker,
  guardForm, renderMetaWarning,
  apiPost, apiPut, showStatus, esc,
} from "../config-helpers.js";

const MODELS = [
  { value: "base.en", label: "base.en — recommended, more accurate" },
  { value: "tiny.en", label: "tiny.en — faster, less accurate" },
];

function modelSelect(selected) {
  const sel = selected || "base.en";
  return MODELS.map(m =>
    `<option value="${m.value}" ${sel === m.value ? "selected" : ""}>${m.label}</option>`
  ).join("");
}

// Whisper model files live in a local cache the bot loads offline; a model
// that isn't cached can't be used until it's fetched. Render one row per model
// with its cache state and a Download button for any that are missing.
function modelsWidget(models) {
  const rows = (models || []).map(m => `
    <div class="vt-model-row" data-model="${esc(m.name)}"
         style="display:flex;align-items:center;gap:.6rem;padding:.35rem 0;flex-wrap:wrap">
      <code style="min-width:5.5rem">${esc(m.name)}</code>
      <span class="vt-model-state">${
        m.cached
          ? `<span style="color:var(--ok,#3a3)">✓ Downloaded</span>`
          : `<span style="color:var(--warn,#c80)">Not downloaded</span>`
      }</span>
      <span style="flex:1"></span>
      <button type="button" class="btn btn-sm vt-dl-btn"
              ${m.cached ? "disabled" : ""}>Download</button>
    </div>`).join("");

  return `
    <div class="field">
      <label>Model Files</label>
      <div class="vt-models">${rows}</div>
      <div class="field-hint">
        Each model is downloaded once onto the machine running Dungeon Keeper and
        then runs entirely offline — no audio ever leaves the server. A model has to
        read <em>Downloaded</em> before you can choose it above. Downloading base.en
        pulls about 150&nbsp;megabytes and can take a minute on a slow host.
      </div>
    </div>`;
}

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading configuration…</div></div>`;

  (async () => {
    const [config, channels] = await Promise.all([loadConfig(), loadChannels()]);
    const vt = config.voice_transcription || {};

    const unavailable = vt.available === false
      ? `<div class="field-hint" style="color:var(--warn,#c80)">
           The faster-whisper package is not installed on the machine running
           Dungeon Keeper, so nothing is transcribed even with this turned on.
           Ask whoever hosts the bot to install it and restart.
         </div>`
      : "";

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Voice Transcription</h2>
          <div class="subtitle">Turns Discord voice messages into text automatically, using a speech model that runs on your own server</div>
        </header>
        ${renderMetaWarning()}
        <form class="form form-cards" data-form>

          <div class="card">
            <div class="section-label">Transcription</div>
            ${unavailable}
            <div class="field">
              <label style="display:flex; gap:6px; align-items:center;">
                <input type="checkbox" name="enabled" ${vt.enabled ? "checked" : ""} />
                Transcribe voice messages
              </label>
              <div class="field-hint">When checked, every voice message posted in the
                channels below gets a text transcript posted underneath it, so members
                who can't listen right now can still follow along. Unchecked, voice
                messages are left alone.</div>
            </div>
            <div class="field">
              <label>Channels to Transcribe</label>
              <span data-picker="channel_ids"></span>
              <div class="field-hint">Only voice messages posted in these channels are
                transcribed. Leave the list empty to transcribe voice messages
                everywhere.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Speech Model</div>
            <div class="field">
              <label for="vt-model">Model</label>
              <select name="model_name" id="vt-model">${modelSelect(vt.model_name)}</select>
              <div class="field-hint">base.en produces better transcripts; tiny.en
                finishes sooner on a slow host. Both understand English only.</div>
            </div>
            ${vt.available === false ? "" : modelsWidget(vt.models)}
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

    const channelsPicker = mountChannelMultiPicker(
      form.querySelector('[data-picker="channel_ids"]'),
      channels,
      vt.channel_ids,
      { label: "Channels to Transcribe" },
    );

    guardForm(form);

    // Per-model download buttons.
    container.querySelectorAll(".vt-model-row").forEach((row) => {
      const btn = row.querySelector(".vt-dl-btn");
      const state = row.querySelector(".vt-model-state");
      const modelName = row.dataset.model;
      btn?.addEventListener("click", async () => {
        btn.disabled = true;
        const prev = state.innerHTML;
        state.innerHTML = `<span style="color:var(--muted,#888)">Downloading…</span>`;
        try {
          const res = await apiPost("/api/config/voice-transcription/download", { model_name: modelName });
          if (res.cached) {
            state.innerHTML = `<span style="color:var(--ok,#3a3)">✓ Downloaded</span>`;
          } else {
            state.innerHTML = prev;
            btn.disabled = false;
          }
        } catch (err) {
          state.innerHTML = `<span style="color:var(--warn,#c80)">${esc(err.message || "Download failed")}</span>`;
          btn.disabled = false;
        }
      });
    });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      try {
        await apiPut("/api/config/voice-transcription", {
          // Was an On/Off <select> posting "1"/"0"; the boolean sent to the
          // API is unchanged.
          enabled: form.querySelector('input[name="enabled"]').checked,
          model_name: fd.get("model_name"),
          // Still a list of id strings, exactly as the multi-select posted.
          channel_ids: channelsPicker.getValues(),
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
