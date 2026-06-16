import {
  loadConfig, loadChannels,
  channelSelectMulti,
  apiPut, showStatus,
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
            <div class="field-hint">base.en is more accurate; tiny.en is faster on slow hosts. The model downloads on first use.</div>
          </div>

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
