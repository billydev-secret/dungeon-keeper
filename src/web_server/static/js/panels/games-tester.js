import { api, apiPost, esc } from "../api.js";
import { showStatus } from "../config-helpers.js";

// All user-supplied content rendered via innerHTML uses esc() for XSS safety.

const GAME_TYPES = ["wyr", "nhie", "mlt", "rushmore", "price", "clapback", "ama"];
const GAME_NAMES = {
  wyr: "Would You Rather", nhie: "Never Have I Ever", mlt: "Most Likely To",
  rushmore: "Mt. Rushmore Draft", price: "Name Your Price", clapback: "Clapback", ama: "Anonymous AMA",
};

export function mount(container, params = {}) {
  let pendingResults = [];

  const gtOptions = GAME_TYPES.map((g) => `<option value="${g}">${GAME_NAMES[g] || g}</option>`).join("");

  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>AI Tester</h2>
        <div class="subtitle">Generate questions with the current AI prompts and optionally save them to the bank.</div>
      </header>

      <section>
        <div class="form">
          <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;">
            <div class="field" style="margin:0;">
              <label>Game type
                <select data-ctrl="gt">${gtOptions}</select>
              </label>
            </div>
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
      </section>

      <section style="margin-top:16px;">
        <div data-region="results"><div class="empty">Results will appear here after generation.</div></div>
        <div data-region="add-selected" style="display:none;margin-top:10px;">
          <button class="btn btn-primary" data-action="add-selected">Add Selected to Bank</button>
          <span data-status="add-sel" class="save-status" style="margin-left:8px;"></span>
        </div>
      </section>
    </div>
  `;

  function ctrl(name) { return container.querySelector(`[data-ctrl="${name}"]`); }
  function region(name) { return container.querySelector(`[data-region="${name}"]`); }

  if (params.gt) ctrl("gt").value = params.gt;

  // Custom prompt toggle
  container.querySelector('[data-action="toggle-custom"]').addEventListener("click", () => {
    const r = region("custom-prompt");
    r.style.display = r.style.display === "none" ? "" : "none";
  });

  // Generate
  container.querySelector('[data-action="generate"]').addEventListener("click", async () => {
    const btn = container.querySelector('[data-action="generate"]');
    const gt = ctrl("gt").value;
    const cat = ctrl("cat").value;
    const count = parseInt(ctrl("count").value);
    const customPrompt = ctrl("custom-prompt").value.trim();

    btn.disabled = true;
    btn.textContent = "Generating...";
    region("results").innerHTML = `<div class="empty">Generating ${count} question(s)...</div>`;
    region("add-selected").style.display = "none";
    pendingResults = [];

    try {
      const body = { game_type: gt, category: cat, count };
      if (customPrompt) body.custom_prompt = customPrompt;
      const data = await apiPost("/api/games/generate", body);
      pendingResults = data.results || [];
      renderResults(pendingResults, gt, cat);
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
  });

  function renderResults(results, gt, cat) {
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

  // Add selected to bank
  container.querySelector('[data-action="add-selected"]').addEventListener("click", async () => {
    const st = container.querySelector('[data-status="add-sel"]');
    const gt = ctrl("gt").value;
    const cat = ctrl("cat").value;

    const checkboxes = region("results").querySelectorAll('[data-check]');
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

    let added = 0;
    let failed = 0;
    for (const text of toAdd) {
      try {
        await apiPost("/api/games/bank", { game_type: gt, category: cat, question_text: text });
        added++;
      } catch (_) { failed++; }
    }
    showStatus(st, failed === 0, `Added ${added}${failed ? ", " + failed + " failed" : ""}`);
  });

  return { unmount() {} };
}