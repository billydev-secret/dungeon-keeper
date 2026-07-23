import { api, apiPost, esc } from "../api.js";
import { apiPut, showStatus, guardForm } from "../config-helpers.js";
import { renderLoading, renderEmpty, renderError } from "../states.js";

// All user-supplied content rendered via innerHTML uses esc() for XSS safety.

const GAME_NAMES = {
  wyr: "Would You Rather", nhie: "Never Have I Ever", mlt: "Most Likely To",
  rushmore: "Mt. Rushmore Draft", price: "Name Your Price", clapback: "Clapback", ama: "Anonymous AMA",
  photo: "Photo Challenge", pen_pals: "Pen Pals",
};

const GAME_HINTS = {
  wyr:      "Generates two-option dilemma questions.",
  nhie:     "Generates confessional statements players vote on.",
  mlt:      "Generates group-ranking prompts.",
  rushmore: "Generates topics for players to draft their top four picks.",
  price:    "Generates opinion-based value questions.",
  clapback: "Generates provocative debate topics.",
  ama:      "Generates anonymous question prompts.",
  photo:    "Generates photo-challenge prompts players answer with a picture.",
  pen_pals: "Generates getting-to-know-you conversation starters for matched pen pals.",
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
        <div class="subtitle">${esc(hint)} Edit the prompts Dungeon Keeper sends the AI, then generate a batch and keep the lines you like.</div>
      </header>

      <section>
        <div class="section-label">Game Prompt</div>
        <div class="form" data-region="game-form">
          ${renderLoading("Loading prompts…")}
        </div>
      </section>

      <section style="margin-top:20px;">
        <div class="section-label">Global Context</div>
        <div class="field-hint">Shared by every game. Editing anything here changes generation for all of them, not just this one.</div>
        <div class="form" data-region="global-form">
          ${renderLoading("Loading global context…")}
        </div>
      </section>

      <section style="margin-top:20px;">
        <div class="section-label">Test Generation</div>
        <div class="form">
          <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;">
            <div class="field m-0">
              <label>Category
                <select data-ctrl="cat">
                  <option value="sfw">SFW</option>
                  <option value="nsfw">NSFW</option>
                </select>
              </label>
            </div>
            <div class="field m-0">
              <label>How Many
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
          <div class="mt-8">
            <button class="btn" data-action="toggle-custom" style="font-size:12px;padding:2px 8px;">Try a One-Off Prompt</button>
            <div data-region="custom-prompt" style="display:none;margin-top:6px;">
              <textarea class="w-full" data-ctrl="custom-prompt" rows="3" placeholder="Optional: replace the user prompt above for this run only. Nothing is saved."></textarea>
            </div>
          </div>
        </div>
        <div data-region="results" style="margin-top:12px;"><div class="empty">Nothing generated yet. Pick a category and a count, then choose Generate — you can edit each line before adding it to the bank.</div></div>
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
            <input class="w-full" type="text" data-ctrl="descriptor" value="${esc((g.descriptor || "")).replace(/"/g, "&quot;")}" />
          </label>
          <div class="field-hint">A short phrase describing this game, dropped into the AI system prompt — for example "two-option dilemmas".</div>
        </div>
        <div class="field">
          <label>User Prompt
            <textarea class="w-full" data-ctrl="user_prompt" rows="4">${esc(g.user_prompt || "")}</textarea>
          </label>
          <div class="field-hint">The instruction the AI actually receives. One question per line is what the importer expects.</div>
        </div>
        <div class="field">
          <label>Maximum Tokens
            <input type="number" data-ctrl="max_tokens" value="${g.max_tokens || 200}" min="50" max="800" style="width:120px;" />
          </label>
          <div class="field-hint">Caps how long one generation can run. Raise it if long batches come back cut off.</div>
        </div>
        <div style="display:flex;align-items:center;gap:8px;">
          <button class="btn btn-primary" data-action="save-game">Save Game Prompt</button>
          <span data-status="game" class="save-status"></span>
        </div>
      `;

      region("global-form").innerHTML = `
        <div class="field">
          <label>Audience Description
            <textarea class="w-full" data-ctrl="audience" rows="3">${esc(cfg.audience || "")}</textarea>
          </label>
        </div>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;">
          <div class="field m-0">
            <label>SFW Tone
              <textarea class="w-full" data-ctrl="sfw_tone" rows="3">${esc(cfg.sfw_tone || "")}</textarea>
            </label>
          </div>
          <div class="field m-0">
            <label>NSFW Tone
              <textarea class="w-full" data-ctrl="nsfw_tone" rows="3">${esc(cfg.nsfw_tone || "")}</textarea>
            </label>
          </div>
        </div>
        <div style="display:flex;align-items:center;gap:8px;margin-top:4px;">
          <button class="btn btn-primary" data-action="save-global">Save Global Context</button>
          <span data-status="global" class="save-status"></span>
        </div>
      `;
      guardForm(region("game-form"));
      guardForm(region("global-form"));
    } catch (err) {
      const msg = `Couldn’t load the AI prompts — try again. (${err.message})`;
      region("game-form").innerHTML = renderError(msg);
      region("global-form").innerHTML = renderError(msg);
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
        showStatus(st, false, `Couldn’t save — ${err.message}`);
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
        showStatus(st, false, `Couldn’t save — ${err.message}`);
      }
      return;
    }

    if (action === "toggle-custom") {
      const r = region("custom-prompt");
      const opening = r.style.display === "none";
      r.style.display = opening ? "" : "none";
      const tbtn = e.target.closest("[data-action]");
      tbtn.setAttribute("aria-expanded", opening ? "true" : "false");
      tbtn.textContent = opening ? "Use the Saved Prompt" : "Try a One-Off Prompt";
      return;
    }

    if (action === "generate") {
      const btn = e.target.closest("[data-action]");
      const cat = ctrl("cat").value;
      const count = parseInt(ctrl("count").value);
      const customPrompt = ctrl("custom-prompt").value.trim();

      btn.disabled = true;
      btn.textContent = "Generating…";
      region("results").innerHTML = renderLoading(`Generating ${count} question${count === 1 ? "" : "s"}…`);
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
        region("results").innerHTML = renderError(`Generation failed — try again. (${err.message})`);
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
      if (!toAdd.length) { showStatus(st, false, "Tick at least one line first."); return; }
      let added = 0, failed = 0;
      for (const text of toAdd) {
        try {
          await apiPost("/api/games/bank", { game_type: gt, category: cat, question_text: text });
          added++;
        } catch (_) { failed++; }
      }
      showStatus(
        st,
        failed === 0,
        failed
          ? `Added ${added}, but ${failed} couldn’t be saved — try those again.`
          : `Added ${added} question${added === 1 ? "" : "s"} to the bank.`,
      );
    }
  });

  function renderResults(results) {
    const reg = region("results");
    if (!results.length) {
      reg.innerHTML = renderEmpty("The AI returned nothing. Try again, or loosen the prompt above.");
      return;
    }
    let html = `<div style="display:flex;flex-direction:column;gap:8px;">`;
    for (let i = 0; i < results.length; i++) {
      html += `<div style="display:flex;gap:8px;align-items:flex-start;">
        <input type="checkbox" data-check="${i}" checked style="margin-top:6px;flex-shrink:0;"
               aria-label="Add generated question ${i + 1} to the bank" />
        <textarea data-result="${i}" rows="2" style="flex:1;"
                  aria-label="Generated question ${i + 1}">${esc(results[i])}</textarea>
      </div>`;
    }
    html += `</div>`;
    reg.innerHTML = html;
    region("add-selected").style.display = "";
  }

  load();
  return { unmount() {} };
}
