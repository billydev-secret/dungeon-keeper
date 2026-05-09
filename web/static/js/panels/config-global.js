import { loadConfig, loadChannels, loadRoles, channelSelect, roleSelectMulti, apiPut, showStatus } from "../config-helpers.js";

function _esc(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading config...</div></div>`;

  (async () => {
    const [config, channels, roles] = await Promise.all([loadConfig(), loadChannels(), loadRoles()]);
    const g = config.global;
    const bi = config.bot_identity || { nick: "", avatar_url: "" };

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Global Config</h2>
          <div class="subtitle">Timezone, mod channel, bypass roles, and recorded bots</div>
        </header>
        <form class="form" data-form>
          <div class="field">
            <label>Timezone Offset (hours from UTC)</label>
            <input type="number" step="0.5" name="tz_offset_hours" value="${_esc(g.tz_offset_hours)}" />
            <div class="field-hint">e.g. -5 for EST, 1 for CET</div>
          </div>
          <div class="field">
            <label>Mod Channel</label>
            <select name="mod_channel_id">${channelSelect(channels, g.mod_channel_id)}</select>
          </div>
          <div class="field">
            <label>Bypass Roles</label>
            <select name="bypass_role_ids" multiple size="6">${roleSelectMulti(roles, g.bypass_role_ids)}</select>
            <div class="field-hint">Roles that bypass spoiler guard and other restrictions (Ctrl/Cmd-click to select multiple)</div>
          </div>
          <div class="field">
            <label>Recorded Bot User IDs</label>
            <input type="text" name="recorded_bot_user_ids" value="${_esc((g.recorded_bot_user_ids || []).join(", "))}" />
            <div class="field-hint">Bot accounts whose messages should be stored (e.g. Risky Roller). Comma-separated user IDs. These bots still don't earn XP or trigger wellness/moderation.</div>
          </div>
          <div class="field">
            <label>Booster Swatch Directory</label>
            <input type="text" name="booster_swatch_dir" value="${_esc(g.booster_swatch_dir || "")}" />
            <div class="field-hint">Folder with booster color swatch images</div>
          </div>
          <div><button type="submit" class="btn btn-primary">Save</button><span data-status></span></div>
        </form>

        <section class="form" style="margin-top:2rem;padding-top:1.5rem;border-top:1px solid var(--border,#333)">
          <h3 style="margin:0 0 1rem">Bot Identity <span style="font-weight:400;font-size:.85em;opacity:.6">(this server)</span></h3>
          ${bi.avatar_url ? `<img data-avatar-preview src="${_esc(bi.avatar_url)}" alt="Bot avatar" style="width:64px;height:64px;border-radius:50%;object-fit:cover;margin-bottom:1rem;display:block" />` : ""}
          <div class="field">
            <label>Nickname</label>
            <input type="text" data-nick value="${_esc(bi.nick)}" placeholder="Leave blank to clear nickname" />
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
      </div>
    `;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      try {
        await apiPut("/api/config/global", {
          tz_offset_hours: parseFloat(fd.get("tz_offset_hours")) || 0,
          mod_channel_id: fd.get("mod_channel_id"),
          bypass_role_ids: Array.from(form.querySelector('select[name="bypass_role_ids"]').selectedOptions).map((o) => o.value),
          recorded_bot_user_ids: fd.get("recorded_bot_user_ids").split(",").map((s) => s.trim()).filter(Boolean),
          booster_swatch_dir: fd.get("booster_swatch_dir"),
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });

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
        const res = await fetch("/api/config/bot-identity", {
          method: "POST",
          credentials: "same-origin",
          body: fd,
        });
        if (!res.ok) {
          let detail = res.statusText;
          try { const b = await res.json(); if (b.detail) detail = b.detail; } catch (_) {}
          throw new Error(`${res.status}: ${detail}`);
        }
        const data = await res.json();
        if (avatarPreview && data.avatar_url) avatarPreview.src = data.avatar_url;
        nickInput.value = data.nick || "";
        avatarUrlInput.value = "";
        avatarFileInput.value = "";
        showStatus(identityStatus, true, "Applied");
      } catch (err) {
        showStatus(identityStatus, false, err.message);
      }
    });
  })();
}
