import { apiPost } from "../api.js";
import { loadConfig, apiPut, showStatus, escapeHtml, guardForm } from "../config-helpers.js";

const DEFAULT_ACCENT = "#5865F2";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading branding…</div></div>`;

  (async () => {
    const config = await loadConfig();
    const bi = config.bot_identity || { nick: "", avatar_url: "" };
    const br = config.branding || { accent_mode: "avatar", accent_hex: "" };
    const mode = br.accent_mode === "custom" ? "custom" : "avatar";
    // Defaults come from the server so the placeholder can never drift from
    // the name the bot actually falls back to.
    const defaultCasinoName = br.default_casino_name || "Golden Meadow";
    const defaultAssistantName = br.default_assistant_name || "Billy-bot";
    const pickerValue = br.accent_hex || DEFAULT_ACCENT;

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Branding</h2>
          <div class="subtitle">This server's bot name, avatar, embed accent color, and feature names</div>
        </header>

        <form class="form card" data-identity-form>
          <div class="section-label">Bot Identity in This Server</div>
          <div class="field-hint" style="margin-bottom:12px;">Dungeon Keeper can wear a different name and avatar in every server it joins. These settings apply here only.</div>
          <img data-avatar-preview src="${escapeHtml(bi.avatar_url)}" alt="Current bot avatar" style="width:64px;height:64px;border-radius:50%;object-fit:cover;margin-bottom:1rem;display:${bi.avatar_url ? "block" : "none"}" />
          <div class="field">
            <label for="cb-nick">Nickname</label>
            <input type="text" id="cb-nick" data-nick value="${escapeHtml(bi.nick)}" maxlength="32" placeholder="Dungeon Keeper" />
            <div class="field-hint">The name members see on the bot in this server. Leave blank to fall back to its default name.</div>
          </div>
          <div class="field">
            <label for="cb-avatar-url">Avatar Image URL</label>
            <input type="url" id="cb-avatar-url" data-avatar-url placeholder="https://example.com/image.png" />
            <div class="field-hint">A public link to a PNG, JPG, or GIF. If you also pick a file below, the file wins.</div>
          </div>
          <div class="field">
            <label for="cb-avatar-file">Upload an Avatar Image</label>
            <input type="file" id="cb-avatar-file" data-avatar-file accept="image/*" />
            <div class="field-hint">Uploading replaces the bot's avatar in this server as soon as you press Save.</div>
          </div>
          <div style="display:flex;gap:8px;align-items:center;">
            <button type="submit" class="btn btn-primary">Save Identity</button>
            <span data-identity-status></span>
          </div>
        </form>

        <form class="form card" data-accent-form style="margin-top:12px;">
          <div class="section-label">Embed Accent Color</div>
          <div class="field-hint" style="margin-bottom:1rem">The colored bar down the side of the bot's embeds (confessions and other neutral panels). Meaningful colors — wins, warnings, leaderboards, game phases — keep their own color and are not affected.</div>
          <div class="field">
            <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-weight:400">
              <input type="radio" name="accent_mode" value="avatar" ${mode === "avatar" ? "checked" : ""} />
              Match the Bot Avatar
            </label>
            <div class="field-hint">Picks the most vivid color out of this server's bot avatar. If the avatar is black and white, the accent comes out gray.</div>
          </div>
          <div class="field">
            <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-weight:400">
              <input type="radio" name="accent_mode" value="custom" ${mode === "custom" ? "checked" : ""} />
              Pick a Color
            </label>
            <div style="display:flex;align-items:center;gap:12px;margin-top:.5rem">
              <input type="color" id="cb-accent-hex" data-accent-hex value="${escapeHtml(pickerValue)}" aria-label="Custom accent color" style="width:52px;height:36px;padding:0;border:none;background:none;cursor:pointer" />
              <code data-accent-hex-label>${escapeHtml(pickerValue.toUpperCase())}</code>
            </div>
            <div class="field-hint">The reliable choice if you want exactly one deliberate color everywhere. Changing the swatch selects this option for you.</div>
          </div>
          <div style="display:flex;gap:8px;align-items:center;">
            <button type="submit" class="btn btn-primary">Save Accent Color</button>
            <span data-accent-status></span>
          </div>
        </form>

        <form class="form card" data-names-form style="margin-top:12px;">
          <div class="section-label">Feature Names</div>
          <div class="field-hint" style="margin-bottom:1rem">Two features carry a name of their own. Rename them to fit this server, or leave a field blank to keep the built-in name.</div>
          <div class="field">
            <label for="cb-casino-name">Casino Name</label>
            <input type="text" id="cb-casino-name" data-casino-name maxlength="60" value="${escapeHtml(br.casino_name || "")}" placeholder="${escapeHtml(defaultCasinoName)}" />
            <div class="field-hint">Used in the casino's hub panel and payout guide — "The ${escapeHtml(defaultCasinoName)} Casino", "How the ${escapeHtml(defaultCasinoName)} pays". Blank uses <strong>${escapeHtml(defaultCasinoName)}</strong>.</div>
          </div>
          <div class="field">
            <label for="cb-assistant-name">AI Assistant Name</label>
            <input type="text" id="cb-assistant-name" data-assistant-name maxlength="60" value="${escapeHtml(br.assistant_name || "")}" placeholder="${escapeHtml(defaultAssistantName)}" />
            <div class="field-hint">What the <code>/ask</code> helper calls itself — in its reply title, in the Help panel's ask box, and in its own answers. Blank uses <strong>${escapeHtml(defaultAssistantName)}</strong>.</div>
          </div>
          <div style="display:flex;gap:8px;align-items:center;">
            <button type="submit" class="btn btn-primary">Save</button>
            <span data-names-status></span>
          </div>
        </form>

        <section class="form card" style="margin-top:12px;">
          <div class="section-label">Quote Card Border</div>
          <div class="field-hint">Upload a custom frame that wraps this server's quote cards. <a href="#/config-quote-border">Open the Quote Tool&nbsp;→</a></div>
        </section>
      </div>
    `;

    // ── Bot identity (reuses existing /api/config/bot-identity) ──────────────
    const identityForm = container.querySelector("[data-identity-form]");
    const identityStatus = container.querySelector("[data-identity-status]");
    const avatarPreview = container.querySelector("[data-avatar-preview]");
    guardForm(identityForm);

    identityForm.addEventListener("submit", async (e) => {
      e.preventDefault();
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
        showStatus(identityStatus, true, "Saved");
      } catch (err) {
        showStatus(identityStatus, false, err.message);
      }
    });

    // ── Accent color ─────────────────────────────────────────────────────────
    const accentForm = container.querySelector("[data-accent-form]");
    const accentPicker = container.querySelector("[data-accent-hex]");
    const accentLabel = container.querySelector("[data-accent-hex-label]");
    const accentStatus = container.querySelector("[data-accent-status]");
    guardForm(accentForm);

    accentPicker.addEventListener("input", () => {
      accentLabel.textContent = accentPicker.value.toUpperCase();
      // Choosing a color implies you want it — switch to custom.
      const customRadio = container.querySelector('input[name="accent_mode"][value="custom"]');
      customRadio.checked = true;
    });

    // ── Feature names ────────────────────────────────────────────────────────
    const namesForm = container.querySelector("[data-names-form]");
    const namesStatus = container.querySelector("[data-names-status]");
    guardForm(namesForm);

    namesForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        // Blank clears the override — the bot falls back to the built-in name.
        await apiPut("/api/config/branding", {
          casino_name: container.querySelector("[data-casino-name]").value.trim(),
          assistant_name: container.querySelector("[data-assistant-name]").value.trim(),
        });
        showStatus(namesStatus, true, "Saved");
      } catch (err) {
        showStatus(namesStatus, false, err.message);
      }
    });

    accentForm.addEventListener("submit", async (e) => {
      e.preventDefault();
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
