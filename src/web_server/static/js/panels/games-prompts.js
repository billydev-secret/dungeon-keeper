import { api, esc } from "../api.js";
import { apiPut, showStatus } from "../config-helpers.js";

// All user-supplied content rendered via innerHTML uses esc() for XSS safety.

const GAME_TYPES = ["wyr", "nhie", "mlt", "rushmore", "price", "clapback", "ama"];
const GAME_ICONS = { wyr: "🤔", nhie: "⛔", mlt: "👑", rushmore: "🗿", price: "💰", clapback: "⚔️", ama: "🎙️" };
const GAME_NAMES = {
  wyr: "Would You Rather", nhie: "Never Have I Ever", mlt: "Most Likely To",
  rushmore: "Mt. Rushmore Draft", price: "Name Your Price", clapback: "Clapback", ama: "Anonymous AMA",
};

export function mount(container) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>AI Prompts</h2>
        <div class="subtitle">Edit the global audience/tone context and per-game prompt templates.</div>
      </header>

      <section>
        <div class="section-label">Global Context</div>
        <div class="form">
          <div class="field">
            <label>Audience description
              <textarea data-ctrl="audience" rows="4" style="width:100%;"></textarea>
            </label>
            <div class="field-hint">Describes who the audience is. Used as part of every system prompt.</div>
          </div>
          <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;">
            <div class="field" style="margin:0;">
              <label>SFW tone
                <textarea data-ctrl="sfw_tone" rows="4" style="width:100%;"></textarea>
              </label>
            </div>
            <div class="field" style="margin:0;">
              <label>NSFW tone
                <textarea data-ctrl="nsfw_tone" rows="4" style="width:100%;"></textarea>
              </label>
            </div>
          </div>
          <div style="display:flex;align-items:center;gap:8px;">
            <button class="btn btn-primary" data-action="save-global">Save Global</button>
            <span data-status="global" class="save-status"></span>
          </div>
        </div>
      </section>

      <section style="margin-top:24px;">
        <div class="section-label">Per-Game Prompts</div>
        <div data-region="games"><div class="empty">Loading</div></div>
      </section>
    </div>
  `;

  function ctrl(name) { return container.querySelector(`[data-ctrl="${name}"]`); }
  function region(name) { return container.querySelector(`[data-region="${name}"]`); }

  async function load() {
    try {
      const cfg = await api("/api/games/prompts");

      // Fill global fields
      ctrl("audience").value = cfg.audience || "";
      ctrl("sfw_tone").value = cfg.sfw_tone || "";
      ctrl("nsfw_tone").value = cfg.nsfw_tone || "";

      // Build per-game accordion cards
      const gamesRegion = region("games");
      const games = cfg.games || {};
      let html = "";
      for (const gt of GAME_TYPES) {
        const g = games[gt] || {};
        html += `<details style="margin-bottom:8px;background:var(--bg-card);border:1px solid var(--rule);border-radius:var(--r);overflow:hidden;" data-game="${gt}">
          <summary style="padding:10px 14px;cursor:pointer;font-weight:600;color:var(--ink-bright);">${GAME_ICONS[gt] || ""} ${esc(g.name || GAME_NAMES[gt] || gt)}</summary>
          <div class="form" style="padding:12px 14px;border-top:1px solid var(--rule-soft);">
            <div class="field">
              <label>Descriptor
                <input type="text" data-ctrl="${gt}-descriptor" value="${esc(g.descriptor || "").replace(/"/g, "&quot;")}" style="width:100%;" />
              </label>
              <div class="field-hint">Short label used in system prompt context.</div>
            </div>
            <div class="field">
              <label>User prompt
                <textarea data-ctrl="${gt}-user_prompt" rows="4" style="width:100%;">${esc(g.user_prompt || "")}</textarea>
              </label>
            </div>
            <div class="field">
              <label>Max tokens
                <input type="number" data-ctrl="${gt}-max_tokens" value="${g.max_tokens || 200}" min="50" max="800" style="width:120px;" />
              </label>
            </div>
            <button class="btn btn-primary" data-action="save-game" data-game="${gt}">Save ${esc(g.name || gt)}</button>
            <span data-status="${gt}" class="save-status" style="margin-left:8px;"></span>
          </div>
        </details>`;
      }
      gamesRegion.innerHTML = html || `<div class="empty">No games found in config.</div>`;
    } catch (err) {
      region("games").innerHTML = `<div class="empty">Error: ${esc(err.message)}</div>`;
    }
  }

  container.querySelector('[data-action="save-global"]').addEventListener("click", async () => {
    const st = container.querySelector('[data-status="global"]');
    try {
      await apiPut("/api/games/prompts/global", {
        audience: ctrl("audience").value,
        sfw_tone: ctrl("sfw_tone").value,
        nsfw_tone: ctrl("nsfw_tone").value,
      });
      showStatus(st, true, "Saved");
    } catch (err) {
      showStatus(st, false, err.message);
    }
  });

  container.addEventListener("click", async (e) => {
    const btn = e.target.closest('[data-action="save-game"]');
    if (!btn) return;
    const gt = btn.dataset.game;
    const st = container.querySelector(`[data-status="${gt}"]`);
    try {
      const descriptor = container.querySelector(`[data-ctrl="${gt}-descriptor"]`).value;
      const user_prompt = container.querySelector(`[data-ctrl="${gt}-user_prompt"]`).value;
      const max_tokens = parseInt(container.querySelector(`[data-ctrl="${gt}-max_tokens"]`).value) || 200;
      await apiPut(`/api/games/prompts/game/${gt}`, { descriptor, user_prompt, max_tokens });
      showStatus(st, true, "Saved");
    } catch (err) {
      showStatus(st, false, err.message);
    }
  });

  load();

  return { unmount() {} };
}