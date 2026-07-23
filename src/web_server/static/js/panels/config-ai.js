import { apiPut, showStatus, guardForm } from "../config-helpers.js";
import { api, esc, apiPost, apiDelete } from "../api.js";
import { confirmDialog } from "../ui.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading AI config…</div></div>`;

  (async () => {
    let data;
    try {
      data = await api("/api/config/ai");
    } catch (err) {
      container.innerHTML = `<div class="panel"><div class="error">Couldn't load the AI settings: ${esc(err.message)}</div></div>`;
      return;
    }

    const phaseLabel = { ready: "Ready", downloading: "Downloading…", loading: "Loading model…", error: "Error", idle: "Not started" };
    const phaseClass = { ready: "chip-success", downloading: "chip-warning", loading: "chip-warning", error: "chip-danger", idle: "chip-neutral" };

    const modelOptions = (models, selected, { allowGlobal = false } = {}) => {
      let html = allowGlobal ? `<option value=""${!selected ? " selected" : ""}>(use the server-wide default)</option>` : "";
      for (const m of models) {
        html += `<option value="${m}"${m === selected ? " selected" : ""}>${m}</option>`;
      }
      return html;
    };

    let promptCards = "";
    for (const p of data.prompts) {
      const badge = p.is_override
        ? `<span class="chip chip-warning">Edited</span>`
        : `<span class="chip chip-neutral">Original</span>`;
      const modelBadge = p.model_is_override
        ? `<span class="chip chip-warning">Own model</span>`
        : `<span class="chip chip-neutral">Server default</span>`;
      const key = esc(p.key);
      promptCards += `
        <div class="ai-prompt-card" data-key="${key}">
          <div class="ai-prompt-header">
            <strong>${esc(p.label)}</strong> ${badge}
            <div class="field-hint">${esc(p.description)}</div>
          </div>
          <div class="ai-model-row" style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;">
            <label for="ai-model-${key}">Model ${modelBadge}</label>
            <select class="ai-cmd-model" id="ai-model-${key}">
              ${modelOptions(data.known_models, p.model_is_override ? p.model : "", { allowGlobal: true })}
            </select>
            <button type="button" class="btn btn-primary btn-sm" data-action="save-model">Save Model</button>
            <span class="save-status" data-model-status></span>
          </div>
          <div class="field-hint">Which model answers this command. Leave it on the server-wide default unless this one command needs something different.</div>
          <label for="ai-prompt-${key}" style="display:block;margin-top:8px;">Instructions Given to the Model</label>
          <textarea class="ai-prompt-text" id="ai-prompt-${key}" rows="8">${esc(p.text)}</textarea>
          <div class="field-hint">The standing instructions sent with every use of this command. Changing them changes how the bot answers straight away.</div>
          <div class="ai-prompt-actions" style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;">
            <button type="button" class="btn btn-primary" data-action="save">Save Instructions</button>
            <button type="button" class="btn btn-ghost" data-action="reset">Restore Original</button>
            <button type="button" class="btn" data-action="test">Try It Out</button>
            <span class="save-status" data-prompt-status></span>
          </div>
          <div class="ai-test-area" style="display:none">
            <label for="ai-test-${key}">Example Message From a Member</label>
            <textarea class="ai-test-input" id="ai-test-${key}" rows="3" placeholder="Type something a member might say, to see how the bot would answer…"></textarea>
            <div class="field-hint">Nothing is posted to your server — the answer only appears below.</div>
            <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:6px;">
              <button type="button" class="btn btn-primary btn-sm" data-action="run-test">Run</button>
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
          <h2>AI (Local LLM)</h2>
          <div class="subtitle">The language model Dungeon Keeper runs on this machine, and the instructions each AI command gives it</div>
        </header>

        <div style="margin-bottom:16px;">${apiKeyNote}</div>

        <div class="section-label">Which Model Each Job Uses</div>
        <form class="form card" data-models-form style="max-width:none;">
          <div class="field-hint" style="margin-bottom:8px;">Every command starts from these unless you give it its own model further down.</div>
          <div class="field-row">
            <div class="field">
              <label for="ai-mod-model">Moderation Model</label>
              <select name="mod_model" id="ai-mod-model">
                ${modelOptions(data.known_models, data.mod_model)}
              </select>
              <div class="field-hint">Used by <code>/ai review</code>, <code>scan</code>, <code>channel</code>, <code>query</code>, and <code>watch</code>.</div>
            </div>
            <div class="field">
              <label for="ai-wellness-model">Wellness Model</label>
              <select name="wellness_model" id="ai-wellness-model">
                ${modelOptions(data.known_models, data.wellness_model)}
              </select>
              <div class="field-hint">Used for the weekly encouragement notes sent to your team.</div>
            </div>
          </div>
          <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;">
            <button type="submit" class="btn btn-primary">Save</button><span data-status></span>
          </div>
        </form>

        <div class="section-label">AI Commands</div>
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
    msLabel.textContent = "Where the Model Comes From";
    modelSourceSection.appendChild(msLabel);

    const msForm = document.createElement("form");
    msForm.className = "form card";
    msForm.style.maxWidth = "none";

    const msHint = document.createElement("div");
    msHint.className = "field-hint";
    msHint.style.marginBottom = "12px";
    msHint.textContent =
      "Name a public Hugging Face repository and the file inside it, and Dungeon Keeper "
      + "downloads the model itself. Only public repositories work — no access token is needed. "
      + "Saving records your choice; press Download & Load to actually fetch it.";
    msForm.appendChild(msHint);

    let _msFieldSeq = 0;
    const addField = (label, name, value, placeholder, hint) => {
      const wrap = document.createElement("div");
      wrap.className = "field";
      const id = `ai-ms-${++_msFieldSeq}`;
      const lbl = document.createElement("label");
      lbl.textContent = label;
      lbl.htmlFor = id;
      const inp = document.createElement("input");
      inp.type = "text";
      inp.id = id;
      inp.name = name;
      inp.value = value || "";
      inp.className = "input";
      inp.placeholder = placeholder;
      wrap.appendChild(lbl);
      wrap.appendChild(inp);
      if (hint) {
        const h = document.createElement("div");
        h.className = "field-hint";
        h.textContent = hint;
        wrap.appendChild(h);
      }
      return wrap;
    };

    const fieldRow = document.createElement("div");
    fieldRow.className = "field-row";
    fieldRow.appendChild(addField(
      "Hugging Face Repository", "hf_repo", data.llm_hf_repo,
      "bartowski/Llama-3.2-3B-Instruct-GGUF",
      "The owner and repository name, exactly as they appear in the huggingface.co address.",
    ));
    fieldRow.appendChild(addField(
      "File Name", "hf_file", data.llm_hf_file,
      "Llama-3.2-3B-Instruct-Q4_K_M.gguf",
      "The .gguf file to download from that repository. Larger files answer better but need more memory.",
    ));
    fieldRow.appendChild(addField(
      "Save It On This Machine At", "model_path", data.llm_model_path,
      "/models/llama.gguf",
      "A file path on the computer running Dungeon Keeper — not on your own device. The folder must already exist and be writable.",
    ));
    msForm.appendChild(fieldRow);

    const msActions = document.createElement("div");
    msActions.style.display = "flex";
    msActions.style.flexWrap = "wrap";
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
    reloadBtn.textContent = "Download & Load";

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

    guardForm(msForm);

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
        showStatus(msStatus, true, "Download started");
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
    guardForm(modelsForm);
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
              modelBadge.textContent = "Own model";
            } else {
              modelBadge.className = "chip chip-neutral";
              modelBadge.textContent = "Server default";
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
            badge.textContent = "Edited";
            showStatus(status, true);
          } catch (err) {
            showStatus(status, false, err.message);
          }
        }

        if (action === "reset") {
          const ok = await confirmDialog(
            "Put back the original instructions for this command? Everything you have written here is discarded and cannot be recovered.",
            { title: "Restore Original Instructions", danger: true, confirmLabel: "Restore Original" },
          );
          if (!ok) return;
          try {
            await apiDelete(`/api/config/ai/prompts/${key}`);
            const fresh = await api("/api/config/ai");
            const p = fresh.prompts.find((x) => x.key === key);
            if (p) {
              textarea.value = p.text;
              badge.className = "chip chip-neutral";
              badge.textContent = "Original";
            }
            showStatus(status, true, "Restored");
          } catch (err) {
            showStatus(status, false, err.message);
          }
        }

        if (action === "test") {
          testArea.style.display = testArea.style.display === "none" ? "block" : "none";
        }

        if (action === "run-test") {
          if (!testInput.value.trim()) {
            testStatus.textContent = "Type an example message first.";
            testInput.focus();
            return;
          }
          testStatus.textContent = "Thinking…";
          testOutput.textContent = "";
          try {
            const result = await apiPost(`/api/config/ai/prompts/${key}/test`, { user_input: testInput.value });
            testStatus.textContent = "Done";
            testOutput.textContent = result.result;
          } catch (err) {
            testStatus.textContent = "";
            testOutput.textContent = `Couldn't run that: ${err.message}`;
          }
        }
      });
    }
  })();
}
