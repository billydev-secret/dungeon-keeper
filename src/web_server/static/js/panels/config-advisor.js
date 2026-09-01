import { api, apiPost, apiDelete, esc } from "../api.js";
import { apiPut, showStatus, guardForm, mountAsync } from "../config-helpers.js";

// Cheapest win first — mirrors advisor_gaps.STATUS_ORDER and the Home tile.
const SUGG_STATUS = {
  ready_but_off: { label: "Just switch on", cls: "sugg-ready" },
  partial: { label: "Half set up", cls: "sugg-partial" },
  unconfigured: { label: "Not set up", cls: "sugg-unset" },
};

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading configuration…</div></div>`;

  return mountAsync(container, async () => {
    let cfg;
    try {
      cfg = await api("/api/config/advisor");
    } catch (err) {
      container.innerHTML = `<div class="panel"><div class="error">Assistant settings failed to load: ${esc(err.message)}</div></div>`;
      return;
    }

    // The name itself is per-guild branding, edited on the Branding panel.
    const name = esc(cfg.assistant_name || "Billy-bot");

    const optionsFor = (selected) =>
      (cfg.models || [])
        .map(
          (m) =>
            `<option value="${esc(m.id)}" ${m.id === selected ? "selected" : ""}>${esc(m.label)}</option>`,
        )
        .join("");

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>${name}</h2>
          <div class="subtitle">The AI helper behind <code>/ask</code> and the ask box in the Help panel. Rename it under <strong>Branding</strong>.</div>
        </header>
        <form class="form form-cards" data-form>
          <div class="card">
            <div class="section-label">Models</div>
            <div class="field">
              <label for="ad-model">Model — Members</label>
              <select name="model" id="ad-model">${optionsFor(cfg.model)}</select>
              <div class="field-hint">Which Claude model answers regular members. Haiku is
                the default — fastest and cheapest, and plenty for grounded help. Every
                answer is billed to whoever hosts the bot.</div>
            </div>
            <div class="field">
              <label for="ad-staff-model">Model — Mods &amp; Admins</label>
              <select name="staff_model" id="ad-staff-model">${optionsFor(cfg.staff_model)}</select>
              <div class="field-hint">Which model answers anyone with a moderator or admin
                permission. Defaults to Sonnet 5: staff asks are the ones that look up and
                change settings, where a stronger model pays for itself. Set it to Haiku to
                treat everyone the same.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">What ${name} Can Read</div>
            <div class="field">
              <label style="display:flex; gap:6px; align-items:center;">
                <input type="checkbox" name="server_context" ${cfg.server_context ? "checked" : ""} />
                Let ${name} read this server as well as the manual
              </label>
              <div class="field-hint">
                <strong>Off by default.</strong> Checked, ${name} can also cite this
                server's channel topics, announcements, and dashboard docs — always
                limited to the channels the asker can see. <strong>Either way</strong>,
                it never sees member messages, DMs, bios, or stats.
              </div>
            </div>
            <div class="field">
              <label style="display:flex; gap:6px; align-items:center;">
                <input type="checkbox" name="config_tools" ${cfg.config_tools ? "checked" : ""} />
                Let ${name} look up settings when an admin asks
              </label>
              <div class="field-hint">
                <strong>On by default.</strong> Admins and mods asking about a feature
                get its live settings and a change to confirm in Discord. Unchecked,
                they get a fixed summary instead. Members are unaffected either way.
              </div>
            </div>
          </div>

          <div style="display:flex; gap:8px; align-items:center;">
            <button type="submit" class="btn btn-primary">Save</button>
            <span data-status></span>
          </div>
        </form>

        <div class="card" data-sec="suggestions">
          <div class="section-label">Suggested Setup</div>
          <div class="field-hint" style="margin-bottom:8px;">
            The features this server hasn't set up — the same list the Home page
            tile shows, in full. Dismiss one and it stops being suggested to
            everybody, permanently; restore it here to bring it back.
          </div>
          <div data-suggestions><div class="empty">Loading…</div></div>
        </div>
      </div>
    `;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");

    guardForm(form);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await apiPut("/api/config/advisor", {
          model: form.querySelector('select[name="model"]').value,
          staff_model: form.querySelector('select[name="staff_model"]').value,
          server_context: form.querySelector('input[name="server_context"]').checked,
          config_tools: form.querySelector('input[name="config_tools"]').checked,
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });

    // Fired off after the settings render rather than awaited alongside them:
    // this card is advisory, and a failure here must not take the page down.
    refreshSuggestions(container);
  }, { errorMsg: "Couldn’t load the advisor settings." });
}

// ── suggested setup (manage view) ────────────────────────────────────
//
// The Home tile can dismiss but has no room to list what was dismissed, so the
// way back lives here — the page that tile already links to. Dismissal is
// guild-level: it records that the server passed on a feature, not that one
// admin is tired of seeing it, so what's cleared here is cleared for everyone.

async function refreshSuggestions(container) {
  const host = container.querySelector("[data-suggestions]");
  if (!host) return;
  let rows;
  try {
    rows = (await api("/api/help/suggestions", { limit: 40, include_dismissed: "true" }))
      .suggestions;
  } catch (err) {
    host.innerHTML = `<div class="error">Setup suggestions failed to load: ${esc(err.message)}</div>`;
    return;
  }
  if (!rows.length) {
    host.innerHTML = `<div class="empty">Everything I track is already set up. Nice.</div>`;
    return;
  }
  host.innerHTML = rows.map((s) => {
    const st = SUGG_STATUS[s.status] || SUGG_STATUS.unconfigured;
    const needs = (s.missing || []).map((m) => m.label);
    return `
      <div class="sugg-manage-row${s.dismissed ? " sugg-dismissed" : ""}">
        <div class="sugg-row">
          <div class="sugg-head">
            <span class="sugg-name">${esc(s.label)}</span>
            <span class="sugg-badge ${st.cls}">${esc(st.label)}</span>
            ${s.dismissed ? `<span class="sugg-badge sugg-unset">Dismissed</span>` : ""}
          </div>
          <div class="sugg-blurb">${esc(s.blurb)}</div>
          ${needs.length ? `<div class="sugg-needs">Still needs: ${esc(needs.join(", "))}</div>` : ""}
          <div class="sugg-panel">${esc(s.panel)}</div>
        </div>
        <button type="button" class="btn" data-sugg-toggle="${esc(s.slug)}"
          data-sugg-dismissed="${s.dismissed ? "1" : ""}">
          ${s.dismissed ? "Restore" : "Dismiss"}
        </button>
      </div>`;
  }).join("");

  host.querySelectorAll("[data-sugg-toggle]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const slug = btn.dataset.suggToggle;
      const path = `/api/help/suggestions/${encodeURIComponent(slug)}/dismiss`;
      btn.disabled = true;
      try {
        if (btn.dataset.suggDismissed) await apiDelete(path);
        else await apiPost(path);
      } catch (_) {
        btn.disabled = false;
        return;
      }
      refreshSuggestions(container);
    });
  });
}
