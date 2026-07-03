import { apiPut, showStatus } from "../config-helpers.js";
import { api, esc, apiPost, apiDelete } from "../api.js";
import { confirmDialog } from "../ui.js";

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

    const phaseLabel = { ready: "Ready", downloading: "Downloading…", loading: "Loading model…", error: "Error", idle: "Not started" };
    const phaseClass = { ready: "chip-success", downloading: "chip-warning", loading: "chip-warning", error: "chip-danger", idle: "chip-neutral" };

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

    const llm = data.llm_status;
    const llmChipClass = phaseClass[llm.phase] || "chip-neutral";
    const llmChipText = llm.phase === "ready"
      ? `LLM ready — ${llm.model}`
      : llm.phase === "error"
        ? `LLM error — ${llm.error}`
        : phaseLabel[llm.phase] || llm.phase;
    const apiKeyNote = `<span class="chip ${llmChipClass}" id="llm-status-chip">${esc(llmChipText)}</span>`;

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

    // ── Model source section (DOM-built to avoid innerHTML for dynamic values) ──
    const modelSourceSection = document.createElement("div");
    modelSourceSection.style.cssText = "margin-bottom:24px;";

    const msLabel = document.createElement("div");
    msLabel.className = "section-label";
    msLabel.textContent = "Local LLM Model";
    modelSourceSection.appendChild(msLabel);

    const msHint = document.createElement("div");
    msHint.className = "field-hint";
    msHint.style.marginBottom = "12px";
    msHint.textContent = "Set the HuggingFace repo and filename to have the bot download the model automatically. Public repos only — no token needed.";
    modelSourceSection.appendChild(msHint);

    const msForm = document.createElement("form");
    msForm.className = "form";
    msForm.style.maxWidth = "none";

    const addField = (label, name, value, hint) => {
      const wrap = document.createElement("div");
      wrap.className = "field";
      const lbl = document.createElement("label");
      lbl.textContent = label;
      const inp = document.createElement("input");
      inp.type = "text";
      inp.name = name;
      inp.value = value || "";
      inp.className = "input";
      inp.placeholder = hint;
      wrap.appendChild(lbl);
      wrap.appendChild(inp);
      return wrap;
    };

    const fieldRow = document.createElement("div");
    fieldRow.className = "field-row";
    fieldRow.appendChild(addField("HuggingFace Repo", "hf_repo", data.llm_hf_repo, "e.g. bartowski/Llama-3.2-3B-Instruct-GGUF"));
    fieldRow.appendChild(addField("Model File", "hf_file", data.llm_hf_file, "e.g. Llama-3.2-3B-Instruct-Q4_K_M.gguf"));
    fieldRow.appendChild(addField("Local Save Path", "model_path", data.llm_model_path, "e.g. /models/llama.gguf"));
    msForm.appendChild(fieldRow);

    const msActions = document.createElement("div");
    msActions.style.display = "flex";
    msActions.style.gap = "8px";
    msActions.style.alignItems = "center";
    msActions.style.marginTop = "8px";

    const saveBtn = document.createElement("button");
    saveBtn.type = "submit";
    saveBtn.className = "btn btn-primary";
    saveBtn.textContent = "Save";

    const reloadBtn = document.createElement("button");
    reloadBtn.type = "button";
    reloadBtn.className = "btn";
    reloadBtn.textContent = "Download & Reload";

    const msStatus = document.createElement("span");
    msStatus.className = "save-status";

    msActions.appendChild(saveBtn);
    msActions.appendChild(reloadBtn);
    msActions.appendChild(msStatus);
    msForm.appendChild(msActions);
    modelSourceSection.appendChild(msForm);

    const panelEl = container.querySelector(".panel");
    const firstSectionLabel = panelEl.querySelector(".section-label");
    panelEl.insertBefore(modelSourceSection, firstSectionLabel);

    msForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(msForm);
      try {
        await apiPut("/api/config/ai/model-source", {
          model_path: fd.get("model_path"),
          hf_repo:    fd.get("hf_repo"),
          hf_file:    fd.get("hf_file"),
        });
        showStatus(msStatus, true);
      } catch (err) {
        showStatus(msStatus, false, err.message);
      }
    });

    // Poll status while downloading or loading.
    let _pollInterval = null;
    const statusChip = container.querySelector("#llm-status-chip");

    const _updateChip = (s) => {
      if (!statusChip) return;
      statusChip.className = `chip ${phaseClass[s.phase] || "chip-neutral"}`;
      if (s.phase === "ready") {
        statusChip.textContent = `LLM ready — ${s.model}`;
      } else if (s.phase === "error") {
        statusChip.textContent = `LLM error — ${s.error}`;
      } else {
        statusChip.textContent = phaseLabel[s.phase] || s.phase;
      }
    };

    const _startPolling = () => {
      if (_pollInterval) return;
      _pollInterval = setInterval(async () => {
        try {
          const s = await api("/api/config/ai/model-status");
          _updateChip(s);
          if (s.phase === "ready" || s.phase === "error" || s.phase === "idle") {
            clearInterval(_pollInterval);
            _pollInterval = null;
          }
        } catch (_) { /* ignore */ }
      }, 2000);
    };

    reloadBtn.addEventListener("click", async () => {
      reloadBtn.disabled = true;
      try {
        await apiPost("/api/config/ai/model-reload");
        _startPolling();
        showStatus(msStatus, true, "Reload started");
      } catch (err) {
        showStatus(msStatus, false, err.message);
      } finally {
        reloadBtn.disabled = false;
      }
    });

    if (llm.phase === "downloading" || llm.phase === "loading") {
      _startPolling();
    }

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
          if (!(await confirmDialog("Reset this prompt to the default text? Any custom prompt will be discarded.", { danger: true, confirmLabel: "Reset" }))) return;
          try {
            await apiDelete(`/api/config/ai/prompts/${key}`);
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
            const result = await apiPost(`/api/config/ai/prompts/${key}/test`, { user_input: testInput.value });
            testStatus.textContent = "Done";
            testOutput.textContent = result.result;
          } catch (err) {
            testStatus.textContent = "";
            testOutput.textContent = `Error: ${err.message}`;
          }
        }
      });
    }
  })();
}
