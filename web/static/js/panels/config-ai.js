import { apiPut, showStatus } from "../config-helpers.js";
import { api } from "../api.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading AI config…</div></div>`;

  (async () => {
    let data;
    try {
      data = await api("/api/config/ai");
    } catch (err) {
      container.innerHTML = `<div class="panel"><div class="error">Failed to load AI config: ${esc(err.message)}</div></div>`;
      return;
    }

    const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

    const modelOptions = (models, selected, { allowGlobal = false } = {}) => {
      let html = allowGlobal ? `<option value=""${!selected ? " selected" : ""}>(use global default)</option>` : "";
      for (const m of models) {
        html += `<option value="${m}"${m === selected ? " selected" : ""}>${m}</option>`;
      }
      return html;
    };

    let promptCards = "";
    for (const p of data.prompts) {
      const badge = p.is_override
        ? `<span class="chip chip-warning">custom</span>`
        : `<span class="chip chip-neutral">default</span>`;
      const modelBadge = p.model_is_override
        ? `<span class="chip chip-warning">custom</span>`
        : `<span class="chip chip-neutral">global</span>`;
      promptCards += `
        <div class="ai-prompt-card" data-key="${p.key}">
          <div class="ai-prompt-header">
            <strong>${esc(p.label)}</strong> ${badge}
            <div class="field-hint">${esc(p.description)}</div>
          </div>
          <div class="ai-model-row">
            <label>Model ${modelBadge}</label>
            <select class="ai-cmd-model">
              ${modelOptions(data.known_models, p.model_is_override ? p.model : "", { allowGlobal: true })}
            </select>
            <button type="button" class="btn btn-primary btn-sm" data-action="save-model">Save</button>
            <span class="save-status" data-model-status></span>
          </div>
          <textarea class="ai-prompt-text" rows="8">${esc(p.text)}</textarea>
          <div class="ai-prompt-actions">
            <button type="button" class="btn btn-primary" data-action="save">Save Prompt</button>
            <button type="button" class="btn btn-ghost" data-action="reset">Reset to Default</button>
            <button type="button" class="btn" data-action="test">Test</button>
            <span class="save-status" data-prompt-status></span>
          </div>
          <div class="ai-test-area" style="display:none">
            <label>Test input (user message)</label>
            <textarea class="ai-test-input" rows="3" placeholder="Type a sample user message to send with this system prompt…"></textarea>
            <div style="display:flex;gap:8px;align-items:center;margin-top:6px;">
              <button type="button" class="btn btn-primary btn-sm" data-action="run-test">Run Test</button>
              <span class="ai-test-status" style="font-size:12px;color:var(--ink-dim)"></span>
            </div>
            <pre class="ai-test-output"></pre>
          </div>
        </div>`;
    }

    const apiKeyNote = data.has_api_key
      ? `<span class="chip chip-success">ANTHROPIC_API_KEY is set</span>`
      : `<span class="chip chip-danger">ANTHROPIC_API_KEY is not set — AI features are disabled</span>`;

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>AI Commands</h2>
          <div class="subtitle">Model selection, system prompts, and prompt testing</div>
        </header>

        <div style="margin-bottom:16px;">${apiKeyNote}</div>

        <div class="section-label">Global Default Models</div>
        <form class="form" data-models-form style="max-width:none;">
          <div class="field-hint" style="margin-bottom:8px;">Commands use these unless overridden per-command below</div>
          <div class="field-row">
            <div class="field">
              <label>Moderation Model</label>
              <select name="mod_model">
                ${modelOptions(data.known_models, data.mod_model)}
              </select>
              <div class="field-hint">Default for /ai review, scan, channel, query, and watch</div>
            </div>
            <div class="field">
              <label>Wellness Model</label>
              <select name="wellness_model">
                ${modelOptions(data.known_models, data.wellness_model)}
              </select>
              <div class="field-hint">Default for weekly wellness encouragement notes</div>
            </div>
          </div>
          <div><button type="submit" class="btn btn-primary">Save Defaults</button><span data-status></span></div>
        </form>

        <div class="section-label">Commands</div>
        <div class="ai-prompts-list">
          ${promptCards}
        </div>
      </div>
    `;

    // ── Models form ────────────────────────────────────────────────
    const modelsForm = container.querySelector("[data-models-form]");
    const modelsStatus = modelsForm.querySelector("[data-status]");
    modelsForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(modelsForm);
      try {
        await apiPut("/api/config/ai/models", {
          mod_model: fd.get("mod_model"),
          wellness_model: fd.get("wellness_model"),
        });
        showStatus(modelsStatus, true);
      } catch (err) {
        showStatus(modelsStatus, false, err.message);
      }
    });

    // ── Prompt cards ───────────────────────────────────────────────
    for (const card of container.querySelectorAll(".ai-prompt-card")) {
      const key = card.dataset.key;
      const textarea = card.querySelector(".ai-prompt-text");
      const status = card.querySelector("[data-prompt-status]");
      const badge = card.querySelector(".ai-prompt-header .chip");
      const modelSelect = card.querySelector(".ai-cmd-model");
      const modelStatus = card.querySelector("[data-model-status]");
      const modelBadge = card.querySelector(".ai-model-row .chip");
      const testArea = card.querySelector(".ai-test-area");
      const testInput = card.querySelector(".ai-test-input");
      const testOutput = card.querySelector(".ai-test-output");
      const testStatus = card.querySelector(".ai-test-status");

      card.addEventListener("click", async (e) => {
        const action = e.target.dataset?.action;
        if (!action) return;

        if (action === "save-model") {
          try {
            await apiPut(`/api/config/ai/prompts/${key}/model`, { model: modelSelect.value });
            if (modelSelect.value) {
              modelBadge.className = "chip chip-warning";
              modelBadge.textContent = "custom";
            } else {
              modelBadge.className = "chip chip-neutral";
              modelBadge.textContent = "global";
            }
            showStatus(modelStatus, true);
          } catch (err) {
            showStatus(modelStatus, false, err.message);
          }
        }

        if (action === "save") {
          try {
            await apiPut(`/api/config/ai/prompts/${key}`, { text: textarea.value });
            badge.className = "chip chip-warning";
            badge.textContent = "custom";
            showStatus(status, true);
          } catch (err) {
            showStatus(status, false, err.message);
          }
        }

        if (action === "reset") {
          try {
            const res = await fetch(`/api/config/ai/prompts/${key}`, {
              method: "DELETE",
              credentials: "same-origin",
            });
            if (!res.ok) throw new Error(`${res.status}`);
            const fresh = await api("/api/config/ai");
            const p = fresh.prompts.find((x) => x.key === key);
            if (p) {
              textarea.value = p.text;
              badge.className = "chip chip-neutral";
              badge.textContent = "default";
            }
            showStatus(status, true, "Reset");
          } catch (err) {
            showStatus(status, false, err.message);
          }
        }

        if (action === "test") {
          testArea.style.display = testArea.style.display === "none" ? "block" : "none";
        }

        if (action === "run-test") {
          if (!testInput.value.trim()) return;
          testStatus.textContent = "Running…";
          testOutput.textContent = "";
          try {
            const model = modelSelect.value || undefined;
            const res = await fetch("/api/config/ai/test", {
              method: "POST",
              credentials: "same-origin",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                system: textarea.value,
                user_input: testInput.value,
                model,
              }),
            });
            const result = await res.json();
            if (result.ok) {
              testStatus.textContent = `${result.model} | ${result.input_tokens} in / ${result.output_tokens} out`;
              testOutput.textContent = result.response;
            } else {
              testStatus.textContent = "";
              testOutput.textContent = `Error: ${result.detail}`;
            }
          } catch (err) {
            testStatus.textContent = "";
            testOutput.textContent = `Error: ${esc(err.message)}`;
          }
        }
      });
    }
  })();
}
