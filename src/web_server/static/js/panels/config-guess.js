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

const DIFFICULTIES = [
  ["easy", "Easy — a generous crop, most people get it"],
  ["medium", "Medium — a balanced crop"],
  ["hard", "Hard — a tight crop, only the sharp-eyed get it"],
];

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading configuration…</div></div>`;

  (async () => {
    const [config, channels, roles] = await Promise.all([
      loadConfig(),
      loadChannels(),
      loadRoles(),
    ]);
    const v = config.guess;

    const num = (name, label, value, hint, { min = 0, max = 100000 } = {}) => `
      <div class="field">
        <label for="gs-${name}">${label}</label>
        <input type="number" name="${name}" id="gs-${name}" required
          min="${min}" max="${max}" step="1" value="${value}" style="max-width:140px;" />
        <div class="field-hint">${hint}</div>
      </div>`;

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Guess Who</h2>
          <div class="subtitle">A guessing game built from cropped member-submitted images, for adults-only channels</div>
        </header>
        ${renderMetaWarning()}
        <form class="form form-cards" data-form>
          <div class="card">
            <div class="section-label">Where and Who</div>
            <div class="field">
              <label>Game Channel</label>
              <span data-picker="channel_id"></span>
              <div class="field-hint">Rounds are posted here. The game does nothing
                until this is set — "(disabled)" turns it off. The channel must be
                marked age-restricted in Discord; Dungeon Keeper refuses to post
                anywhere else.</div>
            </div>
            <div class="field">
              <label>Required Role</label>
              <span data-picker="role_id"></span>
              <div class="field-hint">Only members holding this role may submit
                images. "(none)" lets anyone who can see the channel submit.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Round Difficulty</div>
            <div class="field">
              <label for="gs-difficulty">Crop Difficulty</label>
              <select name="crop_difficulty" id="gs-difficulty">
                ${DIFFICULTIES.map(([val, label]) =>
                  `<option value="${val}"${val === v.crop_difficulty ? " selected" : ""}>${label}</option>`).join("")}
              </select>
              <div class="field-hint">How tightly each round's image is cropped around
                the detected region. Tighter crops make rounds harder and last
                longer.</div>
            </div>
            ${num("guess_cooldown_seconds", "Wait Between Guesses (seconds)", v.guess_cooldown_seconds,
              "How long one member must wait after guessing before guessing again. 0 removes the wait, which lets a fast typist brute-force a round.",
              { min: 0, max: 3600 })}
            ${num("max_guesses_per_round", "Guesses Per Round, Per Member", v.max_guesses_per_round,
              "The most guesses one member may make on a single round. Keep it low so nobody can simply try every answer.",
              { min: 1, max: 1000 })}
          </div>

          <div class="card">
            <div class="section-label">Image Submissions</div>
            ${num("min_image_dimension_px", "Smallest Allowed Image (pixels)", v.min_image_dimension_px,
              "Images narrower or shorter than this are rejected, because a tiny picture cropped down is unguessable.",
              { min: 1, max: 10000 })}
            ${num("max_image_size_mb", "Largest Allowed Image (megabytes)", v.max_image_size_mb,
              "Images bigger than this are rejected, to keep uploads and storage in check.",
              { min: 1, max: 100 })}
            ${num("submit_max_per_window", "Submissions Allowed Per Member", v.submit_max_per_window,
              "The most images one member may submit inside the time window below. This is the flood protection — beyond it, submissions are refused.",
              { min: 1, max: 1000 })}
            ${num("submit_window_seconds", "Submission Window (seconds)", v.submit_window_seconds,
              "The rolling stretch of time the submission limit above is measured over.",
              { min: 1, max: 86400 })}
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
      channels, String(v.channel_id || "0"), { label: "Game Channel" },
    );
    const rolePicker = mountRolePicker(
      form.querySelector('[data-picker="role_id"]'),
      roles, String(v.role_id || "0"), { label: "Required Role" },
    );

    guardForm(form);

    // Blank or out-of-range numbers used to post NaN and come back as a raw
    // 422 naming no field — validate here and say which field is wrong.
    const NUMS = [
      ["guess_cooldown_seconds", "Wait Between Guesses", 0, 3600],
      ["min_image_dimension_px", "Smallest Allowed Image", 1, 10000],
      ["max_image_size_mb", "Largest Allowed Image", 1, 100],
      ["submit_max_per_window", "Submissions Allowed Per Member", 1, 1000],
      ["submit_window_seconds", "Submission Window", 1, 86400],
      ["max_guesses_per_round", "Guesses Per Round, Per Member", 1, 1000],
    ];

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const payload = {
        // Ids stay strings, same values the plain selects posted.
        channel_id: channelPicker.getValue() || "0",
        role_id: rolePicker.getValue() || "0",
        crop_difficulty: fd.get("crop_difficulty"),
      };
      for (const [name, label, min, max] of NUMS) {
        const n = parseInt(fd.get(name), 10);
        if (!Number.isFinite(n) || n < min || n > max) {
          showStatus(status, false, `${label} must be a number from ${min} to ${max}`);
          form.querySelector(`[name=${name}]`).focus();
          return;
        }
        payload[name] = n;
      }
      try {
        await apiPut("/api/config/guess", payload);
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
