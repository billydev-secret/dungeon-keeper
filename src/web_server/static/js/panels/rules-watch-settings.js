import {
  loadConfig,
  loadChannels,
  apiPut,
  apiDelete,
  apiPost,
  esc,
  lockUnlessAdmin,
  showStatus,
  guardForm,
  renderMetaWarning,
  mountChannelPicker,
  mountAsync,
} from "../config-helpers.js";
import { api } from "../api.js";
import { confirmDialog } from "../ui.js";

// The guard's system prompt is bot-global (stored at guild 0, same as every
// AI Models prompt) and its read/write routes 403 off the primary guild —
// see ai_routes.py's _require_primary_guild. This panel itself is only
// adminOnly (not primaryOnly: enabled/channel_id are per-guild), so a
// secondary guild's admin can land here; the card below is gated client-side
// to match, with a pointer instead of a dead fetch. The PUT/DELETE refuse
// off-primary server-side regardless — this is belt and suspenders.
function isPrimaryGuild() {
  const u = window.__dk_user || {};
  return !u.primary_guild_id || u.guild_id === u.primary_guild_id;
}

const RULES_WATCH_PROMPT_KEY = "ai_prompt_rules_watch";

/**
 * Rules Watch settings — enable/disable, the alert channel, and (primary
 * guild only) the guard's system prompt (Config → Moderation & Safety, id
 * config-rules-watch, adminOnly). The alert queue lives under
 * Moderation → Rules Watch, cross-linked via `related:`. The prompt used to
 * live on the AI Models page (config-ai) alongside model plumbing; it moved
 * here to sit with the feature it drives, still reading and writing the same
 * /api/config/ai prompt routes. lockUnlessAdmin stays as defense in depth;
 * writes are refused server-side regardless.
 */
export function mount(outer) {
  outer.innerHTML = `
    <div class="panel">
      <header>
        <h2>Rules Watch</h2>
        <div class="subtitle">What the AI is told to watch for</div>
      </header>
      <section data-region="settings"></section>
    </div>
  `;
  return mountSettings(outer.querySelector('[data-region="settings"]'));
}

export function mountSettings(container) {
  container.innerHTML = `<div class="empty">Loading configuration…</div>`;

  return mountAsync(container, async () => {
    const onPrimary = isPrimaryGuild();
    // Off-primary, /api/config/ai 403s server-side (the prompt is bot-global —
    // see isPrimaryGuild above), so it's skipped rather than fetched and
    // discarded. On the primary guild a real failure here is left to reject
    // into mountAsync's own catch, same as loadConfig/loadChannels, so it
    // gets the panel's normal error state with a working retry rather than a
    // silently degraded card.
    const [config, channels, aiData] = await Promise.all([
      loadConfig(),
      loadChannels(),
      onPrimary ? api("/api/config/ai") : Promise.resolve(null),
    ]);
    const rw = config.rules_watch || { enabled: false, channel_id: "0", guard_available: false };
    const promptInfo = onPrimary ? aiData.prompts.find((p) => p.key === RULES_WATCH_PROMPT_KEY) : null;

    const guardBadge = rw.guard_available
      ? `<span class="badge badge-success">Ready</span>`
      : `<span class="badge badge-warning">Not set up</span>`;
    const guardHint = rw.guard_available
      ? "The local guard model is set up, so flagged messages are recorded as soon as monitoring is on."
      : "No local guard model is set up. Even with monitoring on, <strong>no messages will be flagged</strong> until you configure the model on the AI Models page.";

    const promptSectionBody = !onPrimary
      ? `<div class="field-hint">This prompt is shared by every server this bot is in, so it can
          only be viewed and edited from the <strong>primary server's</strong> Rules Watch settings.</div>`
      : promptInfo
        ? renderPromptCard(promptInfo)
        : `<div class="field-hint">Couldn't find the guard's instructions here — edit them from the
            <a href="#/config-ai">AI Models page</a> instead.</div>`;

    container.innerHTML = `
      <div>
        <div class="section-label">Settings</div>
        <div class="field-hint" style="margin-bottom:12px;">A quiet AI second pair of eyes — it flags messages into a review queue and never acts on its own</div>
        ${renderMetaWarning()}
        <form class="form form-cards" data-form>
          <div class="card">
            <div class="section-label">Monitoring</div>
            <div class="field">
              <label style="display:flex; gap:6px; align-items:center;">
                <input type="checkbox" name="enabled" ${rw.enabled ? "checked" : ""} />
                Screen public messages for rule breaks
              </label>
              <div class="field-hint">Flags messages into the Moderation › Rules Watch
                queue for a moderator to review — nothing is deleted or punished
                automatically. Also starts the <strong>Ledger</strong>, a separate
                consent-and-cross-platform-event log that needs no AI model.</div>
            </div>
            <div class="field">
              <label>Immediate Alert Channel</label>
              <span data-picker="channel_id"></span>
              <div class="field-hint">Optional. Only the most serious flags are posted
                here in Discord as they happen. Leave it "(disabled)" to collect
                everything quietly in the web queue instead.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Guard Model</div>
            <div class="field">
              <label>Local Guard Model: ${guardBadge}</label>
              <div class="field-hint">${guardHint}</div>
            </div>
          </div>

          <div style="display:flex; gap:8px; align-items:center;">
            <button type="submit" class="btn btn-primary">Save</button>
            <span data-status></span>
          </div>
        </form>

        <section class="form" style="margin-top:2rem;padding-top:1.5rem;border-top:1px solid var(--rule)">
          <div class="section-label">Guard Instructions</div>
          <div class="field-hint" style="margin-bottom:8px;">
            What the AI is told to look for when it screens a message. These apply
            bot-wide, the same as every other AI command's instructions.
          </div>
          ${promptSectionBody}
        </section>
      </div>
    `;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");

    const channelPicker = mountChannelPicker(
      form.querySelector('[data-picker="channel_id"]'),
      channels,
      String(rw.channel_id || "0"),
      { label: "Immediate Alert Channel" },
    );

    guardForm(form);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await apiPut("/api/config/rules-watch", {
          enabled: form.querySelector('input[name="enabled"]').checked,
          channel_id: channelPicker.getValue() || "0",
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });

    if (promptInfo) wirePromptCard(container, promptInfo.key);

    // After the channel picker (and prompt card, if rendered) mount, so
    // their inputs are covered too.
    lockUnlessAdmin(container);
  }, { errorMsg: "Couldn’t load the rules watch settings." });
}

// Markup mirrors the prompt cards on the AI Models page (same underlying
// /api/config/ai/prompts/{key} routes) — copy edited for a single,
// already-named prompt rather than a generic list entry.
function renderPromptCard(p) {
  const badge = p.is_override
    ? `<span class="chip chip-warning">Edited</span>`
    : `<span class="chip chip-neutral">Original</span>`;
  const key = esc(p.key);
  return `
    <div class="ai-prompts-list">
      <div class="ai-prompt-card" data-key="${key}">
        <div class="ai-prompt-header">
          <strong>${esc(p.label)}</strong> ${badge}
          <div class="field-hint">${esc(p.description)}</div>
        </div>
        <label for="rw-prompt-${key}" style="display:block;margin-top:8px;">Instructions Given to the Model</label>
        <textarea class="ai-prompt-text" id="rw-prompt-${key}" rows="8">${esc(p.text)}</textarea>
        <div class="field-hint">The standing instructions sent with every message this guard screens. Changing them changes what gets flagged straight away.</div>
        <div class="ai-prompt-actions" style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;">
          <button type="button" class="btn btn-primary" data-action="save">Save Instructions</button>
          <button type="button" class="btn btn-ghost" data-action="reset">Restore Original</button>
          <button type="button" class="btn" data-action="test">Try It Out</button>
          <span class="save-status" data-prompt-status></span>
        </div>
        <div class="ai-test-area" style="display:none">
          <label for="rw-test-${key}">Example Message From a Member</label>
          <textarea class="ai-test-input" id="rw-test-${key}" rows="3" placeholder="Type something a member might say, to see how the guard would judge it…"></textarea>
          <div class="field-hint">Nothing is posted to your server or the review queue — the answer only appears below.</div>
          <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:6px;">
            <button type="button" class="btn btn-primary btn-sm" data-action="run-test">Run</button>
            <span class="ai-test-status" style="font-size:12px;color:var(--ink-dim)"></span>
          </div>
          <pre class="ai-test-output"></pre>
        </div>
      </div>
    </div>`;
}

function wirePromptCard(container, key) {
  const card = container.querySelector(`.ai-prompt-card[data-key="${CSS.escape(key)}"]`);
  if (!card) return;
  const textarea = card.querySelector(".ai-prompt-text");
  const status = card.querySelector("[data-prompt-status]");
  const badge = card.querySelector(".ai-prompt-header .chip");
  const testArea = card.querySelector(".ai-test-area");
  const testInput = card.querySelector(".ai-test-input");
  const testOutput = card.querySelector(".ai-test-output");
  const testStatus = card.querySelector(".ai-test-status");

  card.addEventListener("click", async (e) => {
    const action = e.target.dataset?.action;
    if (!action) return;

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
        "Put back the original instructions for this guard? Everything you have written here is discarded and cannot be recovered.",
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
