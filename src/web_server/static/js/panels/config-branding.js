import { apiPost } from "../api.js";
import { loadConfig, apiPut, showStatus, escapeHtml } from "../config-helpers.js";

const DEFAULT_ACCENT = "#5865F2";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading branding...</div></div>`;

  (async () => {
    const config = await loadConfig();
    const bi = config.bot_identity || { nick: "", avatar_url: "" };
    const br = config.branding || { accent_mode: "avatar", accent_hex: "" };
    const mode = br.accent_mode === "custom" ? "custom" : "avatar";
    const pickerValue = br.accent_hex || DEFAULT_ACCENT;

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Branding</h2>
          <div class="subtitle">This server's bot name, avatar, and embed accent color</div>
        </header>

        <section class="form">
          <h3 style="margin:0 0 1rem">Bot Identity <span style="font-weight:400;font-size:.85em;opacity:.6">(this server)</span></h3>
          <img data-avatar-preview src="${escapeHtml(bi.avatar_url)}" alt="Bot avatar" style="width:64px;height:64px;border-radius:50%;object-fit:cover;margin-bottom:1rem;display:${bi.avatar_url ? "block" : "none"}" />
          <div class="field">
            <label>Nickname</label>
            <input type="text" data-nick value="${escapeHtml(bi.nick)}" placeholder="Leave blank to clear nickname" />
          </div>
          <div class="field">
            <label>Avatar URL</label>
            <input type="url" data-avatar-url placeholder="https://example.com/image.png" />
            <div class="field-hint">Paste an image URL, or upload a file below (file takes priority if both are set)</div>
          </div>
          <div class="field">
            <label>Upload Avatar</label>
            <input type="file" data-avatar-file accept="image/*" />
          </div>
          <div><button type="button" class="btn btn-primary" data-identity-apply>Apply</button><span data-identity-status></span></div>
        </section>

        <section class="form" style="margin-top:2rem;padding-top:1.5rem;border-top:1px solid var(--border,#333)">
          <h3 style="margin:0 0 1rem">Embed Accent Color</h3>
          <div class="field-hint" style="margin-bottom:1rem">The colored bar on the bot's embeds (confessions and other neutral panels). Semantic colors — win/danger/leaderboard, game phases — are not affected.</div>
          <div class="field">
            <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-weight:400">
              <input type="radio" name="accent_mode" value="avatar" ${mode === "avatar" ? "checked" : ""} />
              Derived from the bot avatar
            </label>
            <div class="field-hint">Picks a vivid color from this server's bot avatar. If the avatar is grayscale, the accent will be gray.</div>
          </div>
          <div class="field">
            <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-weight:400">
              <input type="radio" name="accent_mode" value="custom" ${mode === "custom" ? "checked" : ""} />
              Custom color
            </label>
            <div style="display:flex;align-items:center;gap:12px;margin-top:.5rem">
              <input type="color" data-accent-hex value="${escapeHtml(pickerValue)}" style="width:52px;height:36px;padding:0;border:none;background:none;cursor:pointer" />
              <code data-accent-hex-label>${escapeHtml(pickerValue.toUpperCase())}</code>
            </div>
            <div class="field-hint">The recommended way to get one consistent, deliberate color everywhere.</div>
          </div>
          <div><button type="button" class="btn btn-primary" data-accent-save>Save</button><span data-accent-status></span></div>
        </section>
      </div>
    `;

    // ── Bot identity (reuses existing /api/config/bot-identity) ──────────────
    const applyBtn = container.querySelector("[data-identity-apply]");
    const identityStatus = container.querySelector("[data-identity-status]");
    const avatarPreview = container.querySelector("[data-avatar-preview]");

    applyBtn.addEventListener("click", async () => {
      const nickInput = container.querySelector("[data-nick]");
      const avatarUrlInput = container.querySelector("[data-avatar-url]");
      const avatarFileInput = container.querySelector("[data-avatar-file]");

      const fd = new FormData();
      fd.append("nick", nickInput.value);
      if (avatarFileInput.files.length > 0) {
        fd.append("avatar_file", avatarFileInput.files[0]);
      } else if (avatarUrlInput.value.trim()) {
        fd.append("avatar_url", avatarUrlInput.value.trim());
      }

      try {
        const data = await apiPost("/api/config/bot-identity", fd);
        if (data.avatar_url) {
          avatarPreview.src = data.avatar_url;
          avatarPreview.style.display = "block";
        }
        nickInput.value = data.nick || "";
        avatarUrlInput.value = "";
        avatarFileInput.value = "";
        showStatus(identityStatus, true, "Applied");
      } catch (err) {
        showStatus(identityStatus, false, err.message);
      }
    });

    // ── Accent color ─────────────────────────────────────────────────────────
    const accentPicker = container.querySelector("[data-accent-hex]");
    const accentLabel = container.querySelector("[data-accent-hex-label]");
    const accentSave = container.querySelector("[data-accent-save]");
    const accentStatus = container.querySelector("[data-accent-status]");

    accentPicker.addEventListener("input", () => {
      accentLabel.textContent = accentPicker.value.toUpperCase();
      // Choosing a color implies you want it — switch to custom.
      const customRadio = container.querySelector('input[name="accent_mode"][value="custom"]');
      customRadio.checked = true;
    });

    accentSave.addEventListener("click", async () => {
      const selectedMode = container.querySelector('input[name="accent_mode"]:checked').value;
      try {
        await apiPut("/api/config/branding", {
          accent_mode: selectedMode,
          accent_hex: accentPicker.value,
        });
        showStatus(accentStatus, true, "Saved");
      } catch (err) {
        showStatus(accentStatus, false, err.message);
      }
    });
  })();
}
