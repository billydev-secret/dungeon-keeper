import { api, apiPost, esc } from "../api.js";
import { apiPut, showStatus } from "../config-helpers.js";

// All user-supplied content rendered via innerHTML uses esc() for XSS safety.

const GAME_NAMES = {
  wyr: "Would You Rather", nhie: "Never Have I Ever", mlt: "Most Likely To",
  rushmore: "Mt. Rushmore Draft", price: "Name Your Price", clapback: "Clapback", ama: "Anonymous AMA",
};

const GAME_HINTS = {
  wyr:      "Generates two-option dilemma questions.",
  nhie:     "Generates confessional statements players vote on.",
  mlt:      "Generates group-ranking prompts.",
  rushmore: "Generates topics for players to draft their top four picks.",
  price:    "Generates opinion-based value questions.",
  clapback: "Generates provocative debate topics.",
  ama:      "Generates anonymous question prompts.",
};

export function mount(container, params = {}) {
  const gt = params.gt || "wyr";
  const gameName = GAME_NAMES[gt] || gt.toUpperCase();
  const hint = GAME_HINTS[gt] || "";

  let pendingResults = [];

  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>${esc(gameName)} — Prompts &amp; AI</h2>
        <div class="subtitle">${esc(hint)} Edit AI prompts and test generation.</div>
      </header>

      <section>
        <div class="section-label">Game Prompt</div>
        <div class="form" data-region="game-form">
          <div class="empty">Loading…</div>
        </div>
      </section>

      <section style="margin-top:20px;">
        <div class="section-label">Global Context</div>
        <div class="field-hint">Shared across all games. Changes here affect every game type.</div>
        <div class="form" data-region="global-form">
          <div class="empty">Loading…</div>
        </div>
      </section>

      <section style="margin-top:20px;">
        <div class="section-label">Test Generation</div>
        <div class="form">
          <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;">
            <div class="field" style="margin:0;">
              <label>Category
                <select data-ctrl="cat">
                  <option value="sfw">SFW</option>
                  <option value="nsfw">NSFW</option>
                </select>
              </label>
            </div>
            <div class="field" style="margin:0;">
              <label>Count
                <select data-ctrl="count">
                  <option value="1">1</option>
                  <option value="3">3</option>
                  <option value="5" selected>5</option>
                  <option value="10">10</option>
                  <option value="20">20</option>
                </select>
              </label>
            </div>
            <button class="btn btn-primary" data-action="generate">Generate</button>
          </div>
          <div style="margin-top:8px;">
            <button class="btn" data-action="toggle-custom" style="font-size:12px;padding:2px 8px;">Custom prompt</button>
            <div data-region="custom-prompt" style="display:none;margin-top:6px;">
              <textarea data-ctrl="custom-prompt" rows="3" style="width:100%;" placeholder="Override the user prompt sent to the AI (optional)"></textarea>
            </div>
          </div>
        </div>
        <div data-region="results" style="margin-top:12px;"><div class="empty">Results will appear here after generation.</div></div>
        <div data-region="add-selected" style="display:none;margin-top:10px;">
          <button class="btn btn-primary" data-action="add-selected">Add Selected to Bank</button>
          <span data-status="add-sel" class="save-status" style="margin-left:8px;"></span>
        </div>
      </section>
    </div>
  `;

  function ctrl(name) { return container.querySelector(`[data-ctrl="${name}"]`); }
  function region(name) { return container.querySelector(`[data-region="${name}"]`); }

  async function load() {
    try {
      const cfg = await api("/api/games/prompts");
      const g = (cfg.games || {})[gt] || {};

      region("game-form").innerHTML = `
        <div class="field">
          <label>Descriptor
            <input type="text" data-ctrl="descriptor" value="${esc((g.descriptor || "")).replace(/"/g, "&quot;")}" style="width:100%;" />
          </label>
          <div class="field-hint">Short label used in the AI system prompt context.</div>
        </div>
        <div class="field">
          <label>User prompt
            <textarea data-ctrl="user_prompt" rows="4" style="width:100%;">${esc(g.user_prompt || "")}</textarea>
          </label>
        </div>
        <div class="field">
          <label>Max tokens
            <input type="number" data-ctrl="max_tokens" value="${g.max_tokens || 200}" min="50" max="800" style="width:120px;" />
          </label>
        </div>
        <div style="display:flex;align-items:center;gap:8px;">
          <button class="btn btn-primary" data-action="save-game">Save Game Prompt</button>
          <span data-status="game" class="save-status"></span>
        </div>
      `;

      region("global-form").innerHTML = `
        <div class="field">
          <label>Audience description
            <textarea data-ctrl="audience" rows="3" style="width:100%;">${esc(cfg.audience || "")}</textarea>
          </label>
        </div>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;">
          <div class="field" style="margin:0;">
            <label>SFW tone
              <textarea data-ctrl="sfw_tone" rows="3" style="width:100%;">${esc(cfg.sfw_tone || "")}</textarea>
            </label>
          </div>
          <div class="field" style="margin:0;">
            <label>NSFW tone
              <textarea data-ctrl="nsfw_tone" rows="3" style="width:100%;">${esc(cfg.nsfw_tone || "")}</textarea>
            </label>
          </div>
        </div>
        <div style="display:flex;align-items:center;gap:8px;margin-top:4px;">
          <button class="btn btn-primary" data-action="save-global">Save Global</button>
          <span data-status="global" class="save-status"></span>
        </div>
      `;
    } catch (err) {
      region("game-form").innerHTML = `<div class="empty">Error: ${esc(err.message)}</div>`;
    }
  }

  container.addEventListener("click", async (e) => {
    const action = e.target.closest("[data-action]")?.dataset?.action;
    if (!action) return;

    if (action === "save-game") {
      const st = container.querySelector('[data-status="game"]');
      try {
        await apiPut(`/api/games/prompts/game/${gt}`, {
          descriptor: ctrl("descriptor").value,
          user_prompt: ctrl("user_prompt").value,
          max_tokens: parseInt(ctrl("max_tokens").value) || 200,
        });
        showStatus(st, true, "Saved");
      } catch (err) {
        showStatus(st, false, err.message);
      }
      return;
    }

    if (action === "save-global") {
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
      return;
    }

    if (action === "toggle-custom") {
      const r = region("custom-prompt");
      r.style.display = r.style.display === "none" ? "" : "none";
      return;
    }

    if (action === "generate") {
      const btn = e.target.closest("[data-action]");
      const cat = ctrl("cat").value;
      const count = parseInt(ctrl("count").value);
      const customPrompt = ctrl("custom-prompt").value.trim();

      btn.disabled = true;
      btn.textContent = "Generating...";
      region("results").innerHTML = `<div class="empty">Generating ${count} question(s)…</div>`;
      region("add-selected").style.display = "none";
      pendingResults = [];

      try {
        const body = { game_type: gt, category: cat, count };
        if (customPrompt) body.custom_prompt = customPrompt;
        const data = await apiPost("/api/games/generate", body);
        pendingResults = data.results || [];
        renderResults(pendingResults);
        if (data.error) {
          const errEl = document.createElement("div");
          errEl.className = "empty";
          errEl.style.color = "var(--red,#c00)";
          errEl.textContent = data.error;
          region("results").appendChild(errEl);
        }
      } catch (err) {
        region("results").innerHTML = `<div class="empty">Error: ${esc(err.message)}</div>`;
      } finally {
        btn.disabled = false;
        btn.textContent = "Generate";
      }
      return;
    }

    if (action === "add-selected") {
      const st = container.querySelector('[data-status="add-sel"]');
      const cat = ctrl("cat").value;
      const checkboxes = region("results").querySelectorAll("[data-check]");
      const toAdd = [];
      checkboxes.forEach((cb) => {
        if (cb.checked) {
          const idx = parseInt(cb.dataset.check);
          const ta = region("results").querySelector(`[data-result="${idx}"]`);
          const text = ta ? ta.value.trim() : (pendingResults[idx] || "").trim();
          if (text) toAdd.push(text);
        }
      });
      if (!toAdd.length) { showStatus(st, false, "Nothing selected"); return; }
      let added = 0, failed = 0;
      for (const text of toAdd) {
        try {
          await apiPost("/api/games/bank", { game_type: gt, category: cat, question_text: text });
          added++;
        } catch (_) { failed++; }
      }
      showStatus(st, failed === 0, `Added ${added}${failed ? ", " + failed + " failed" : ""}`);
    }
  });

  function renderResults(results) {
    const reg = region("results");
    if (!results.length) {
      reg.innerHTML = `<div class="empty">No results generated.</div>`;
      return;
    }
    let html = `<div style="display:flex;flex-direction:column;gap:8px;">`;
    for (let i = 0; i < results.length; i++) {
      html += `<div style="display:flex;gap:8px;align-items:flex-start;">
        <input type="checkbox" data-check="${i}" checked style="margin-top:6px;flex-shrink:0;" />
        <textarea data-result="${i}" rows="2" style="flex:1;">${esc(results[i])}</textarea>
      </div>`;
    }
    html += `</div>`;
    reg.innerHTML = html;
    region("add-selected").style.display = "";
  }

  load();
  return { unmount() {} };
}
