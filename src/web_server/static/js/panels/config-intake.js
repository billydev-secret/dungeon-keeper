import { esc, loadConfig, loadChannels, loadRoles, channelSelect, roleSelect, apiPut, apiPost, showStatus } from "../config-helpers.js";

// Auto-kind choices for a step. "" = manual (welcomers tick a button on the
// card); the rest tick themselves from bot events.
const AUTO_KINDS = [
  ["", "Manual (button on the card)"],
  ["greeted", "Auto: greeter mentions them"],
  ["verified", "Auto: unverified role removed"],
  ["role_gained", "Auto: role gained"],
];

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading config…</div></div>`;

  (async () => {
    const [config, channels, roles] = await Promise.all([loadConfig(), loadChannels(), loadRoles()]);
    const c = config.intake || { enabled: false, channel_id: "0", completion_code: "", stale_hours: 24, steps: [] };
    // Working copy of the step list; rows re-render from this.
    let steps = (c.steps || []).map((s) => ({ ...s }));

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Intake Cards</h2>
          <div class="subtitle">Per-newcomer welcome checklist posted to greeter chat — the open cards are your intake queue</div>
        </header>
        <form class="form" data-form>
          <div class="field">
            <label><input type="checkbox" name="enabled" ${c.enabled ? "checked" : ""} /> Enable intake cards</label>
            <div class="field-hint">When on, each join posts a card (pinging the greeter role) and the legacy bare arrival ping is replaced. Off = join behavior is unchanged.</div>
          </div>
          <div class="field">
            <label>Card channel</label>
            <select name="channel_id">${channelSelect(channels, c.channel_id)}</select>
            <div class="field-hint">Where cards post. Leave on (none) to use the greeter chat channel from Welcome &amp; Leave.</div>
          </div>
          <div class="field">
            <label>Completion code</label>
            <input type="text" name="completion_code" value="${esc(c.completion_code)}" maxlength="80" placeholder="e.g. DK-7734" />
            <div class="field-hint">When a greeter or mod posts a message containing this code and @mentioning the newcomer (any channel), their card completes — unticked steps are marked skipped. Put the code in your ending welcome message. Empty = code detection off.</div>
          </div>
          <div class="field">
            <label>Stale nudge after (hours)</label>
            <input type="number" name="stale_hours" min="1" max="720" value="${Number(c.stale_hours) || 24}" />
            <div class="field-hint">A card with no progress for this long gets one greeter-role nudge reply. Any ticked step resets the clock; a card is never nudged twice.</div>
          </div>
          <div class="field">
            <label>Checklist steps</label>
            <div data-steps></div>
            <button type="button" class="btn" data-add-step>+ Add step</button>
            <div class="field-hint">Snapshotted onto each card when it posts — editing here never changes cards already in flight. Auto steps tick themselves; “role gained” fires on /grant or a manual role add of the chosen role.</div>
          </div>
          <div><button type="submit" class="btn btn-primary">Save</button><span data-status></span></div>
        </form>
      </div>
      <div class="panel" style="margin-top:16px;">
        <header>
          <h2>Procedure Reference</h2>
          <div class="subtitle">The #welcome-procedure content, bot-synced — edit here, the channel updates itself. Questions render one message each for one-tap copy-paste.</div>
        </header>
        <form class="form" data-ref-form>
          <div class="field">
            <label>Reference channel</label>
            <select name="ref_channel_id">${channelSelect(channels, c.reference_channel_id || "0")}</select>
            <div class="field-hint">The channel the bot maintains (e.g. #welcome-procedure). The bot only ever touches its own tracked messages — consider locking the channel to bot-posts once adopted.</div>
          </div>
          <div class="field">
            <label>Blocks</label>
            <div data-blocks></div>
            <button type="button" class="btn" data-add-block>+ Add block</button>
            <button type="button" class="btn" data-import>Import from channel</button>
            <div class="field-hint">Text sections post as one message; question lists post a header plus one message per line. Import reads the channel's current messages into draft text blocks (only while the editor is empty) — then split your question lists out and save.</div>
          </div>
          <div><button type="submit" class="btn btn-primary">Save &amp; sync channel</button><span data-ref-status></span></div>
        </form>
      </div>
    `;

    const stepsHost = container.querySelector("[data-steps]");
    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");

    const renderSteps = () => {
      stepsHost.innerHTML = steps
        .map(
          (s, i) => `
        <div class="row wrap" data-step-row data-key="${esc(s.key || "")}" style="gap:6px; align-items:center; margin-bottom:6px;">
          <input type="text" data-step-label value="${esc(s.label)}" maxlength="80" placeholder="Step label" style="flex:2; min-width:10rem;" />
          <select data-step-auto style="flex:2; min-width:10rem;">
            ${AUTO_KINDS.map(([v, t]) => `<option value="${v}" ${v === (s.auto || "") ? "selected" : ""}>${t}</option>`).join("")}
          </select>
          <select data-step-role style="flex:2; min-width:10rem; ${s.auto === "role_gained" ? "" : "display:none;"}">
            ${roleSelect(roles, s.role_id)}
          </select>
          <span style="white-space:nowrap;">
            <button type="button" class="btn" data-up ${i === 0 ? "disabled" : ""}>↑</button>
            <button type="button" class="btn" data-down ${i === steps.length - 1 ? "disabled" : ""}>↓</button>
            <button type="button" class="btn" data-remove>✕</button>
          </span>
        </div>`
        )
        .join("");
    };

    // Pull edited values back into the working copy before any re-render.
    const syncSteps = () => {
      stepsHost.querySelectorAll("[data-step-row]").forEach((row, i) => {
        steps[i].label = row.querySelector("[data-step-label]").value;
        steps[i].auto = row.querySelector("[data-step-auto]").value;
        steps[i].role_id = row.querySelector("[data-step-role]").value || "0";
      });
    };

    stepsHost.addEventListener("click", (e) => {
      const row = e.target.closest("[data-step-row]");
      if (!row) return;
      const i = Array.from(stepsHost.children).indexOf(row);
      if (e.target.matches("[data-remove]")) {
        syncSteps();
        steps.splice(i, 1);
        renderSteps();
      } else if (e.target.matches("[data-up]") && i > 0) {
        syncSteps();
        [steps[i - 1], steps[i]] = [steps[i], steps[i - 1]];
        renderSteps();
      } else if (e.target.matches("[data-down]") && i < steps.length - 1) {
        syncSteps();
        [steps[i], steps[i + 1]] = [steps[i + 1], steps[i]];
        renderSteps();
      }
    });

    // Show/hide the role picker as the auto kind changes.
    stepsHost.addEventListener("change", (e) => {
      if (!e.target.matches("[data-step-auto]")) return;
      const row = e.target.closest("[data-step-row]");
      row.querySelector("[data-step-role]").style.display =
        e.target.value === "role_gained" ? "" : "none";
    });

    container.querySelector("[data-add-step]").addEventListener("click", () => {
      syncSteps();
      steps.push({ key: "", label: "", auto: "", role_id: "0" });
      renderSteps();
      const rows = stepsHost.querySelectorAll("[data-step-label]");
      rows[rows.length - 1]?.focus();
    });

    renderSteps();

    // ── Procedure reference blocks ──────────────────────────────────
    let blocks = (c.reference_blocks || []).map((b) => ({ ...b }));
    const refForm = container.querySelector("[data-ref-form]");
    const refStatus = container.querySelector("[data-ref-status]");
    const blocksHost = container.querySelector("[data-blocks]");

    const renderBlocks = () => {
      blocksHost.innerHTML = blocks
        .map(
          (b, i) => `
        <div data-block-row style="border:1px solid var(--border, #444); border-radius:6px; padding:8px; margin-bottom:8px;">
          <div class="row wrap" style="gap:6px; margin-bottom:6px;">
            <select data-block-kind style="min-width:10rem;">
              <option value="text" ${b.kind !== "questions" ? "selected" : ""}>Text section</option>
              <option value="questions" ${b.kind === "questions" ? "selected" : ""}>Question list</option>
            </select>
            <input type="text" data-block-title value="${esc(b.title || "")}" maxlength="120" placeholder="Title (optional)" style="flex:2; min-width:10rem;" />
            <span style="white-space:nowrap;">
              <button type="button" class="btn" data-block-up ${i === 0 ? "disabled" : ""}>↑</button>
              <button type="button" class="btn" data-block-down ${i === blocks.length - 1 ? "disabled" : ""}>↓</button>
              <button type="button" class="btn" data-block-remove>✕</button>
            </span>
          </div>
          <textarea data-block-body rows="${b.kind === "questions" ? 6 : 4}" style="width:100%;" placeholder="${b.kind === "questions" ? "One question per line" : "Section text"}">${esc(b.body || "")}</textarea>
        </div>`
        )
        .join("");
    };

    const syncBlocks = () => {
      blocksHost.querySelectorAll("[data-block-row]").forEach((row, i) => {
        blocks[i].kind = row.querySelector("[data-block-kind]").value;
        blocks[i].title = row.querySelector("[data-block-title]").value;
        blocks[i].body = row.querySelector("[data-block-body]").value;
      });
    };

    blocksHost.addEventListener("click", (e) => {
      const row = e.target.closest("[data-block-row]");
      if (!row) return;
      const i = Array.from(blocksHost.children).indexOf(row);
      if (e.target.matches("[data-block-remove]")) {
        syncBlocks();
        blocks.splice(i, 1);
        renderBlocks();
      } else if (e.target.matches("[data-block-up]") && i > 0) {
        syncBlocks();
        [blocks[i - 1], blocks[i]] = [blocks[i], blocks[i - 1]];
        renderBlocks();
      } else if (e.target.matches("[data-block-down]") && i < blocks.length - 1) {
        syncBlocks();
        [blocks[i], blocks[i + 1]] = [blocks[i + 1], blocks[i]];
        renderBlocks();
      }
    });

    container.querySelector("[data-add-block]").addEventListener("click", () => {
      syncBlocks();
      blocks.push({ kind: "text", title: "", body: "" });
      renderBlocks();
    });

    container.querySelector("[data-import]").addEventListener("click", async () => {
      if (blocks.length) {
        showStatus(refStatus, false, "Import only works while the editor is empty");
        return;
      }
      const channelId = refForm.querySelector('select[name="ref_channel_id"]').value || "0";
      if (!(parseInt(channelId) > 0)) {
        showStatus(refStatus, false, "Pick the reference channel first");
        return;
      }
      try {
        const res = await apiPost("/api/config/intake/reference/import", { channel_id: channelId });
        blocks = (res.blocks || []).map((b) => ({ ...b }));
        renderBlocks();
        showStatus(refStatus, true, `Imported ${blocks.length} blocks — review, split the question lists, then save`);
      } catch (err) {
        showStatus(refStatus, false, err.message);
      }
    });

    renderBlocks();

    refForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      syncBlocks();
      try {
        const res = await apiPut("/api/config/intake/reference", {
          channel_id: refForm.querySelector('select[name="ref_channel_id"]').value || "0",
          blocks: blocks.map((b) => ({
            kind: b.kind || "text",
            title: (b.title || "").trim(),
            body: b.body || "",
          })),
        });
        const s = res.sync || {};
        showStatus(
          refStatus, true,
          s.synced
            ? `Saved — channel synced (${s.posted || 0} posted, ${s.edited || 0} edited, ${s.deleted || 0} deleted)`
            : `Saved — channel not synced (${s.reason || "unknown"})`
        );
      } catch (err) {
        showStatus(refStatus, false, err.message);
      }
    });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      syncSteps();
      const bad = steps.find((s) => s.auto === "role_gained" && !(parseInt(s.role_id) > 0));
      if (bad) {
        showStatus(status, false, `“${bad.label || "unnamed step"}” needs a role`);
        return;
      }
      if (steps.some((s) => !s.label.trim())) {
        showStatus(status, false, "Every step needs a label");
        return;
      }
      const fd = new FormData(form);
      try {
        await apiPut("/api/config/intake", {
          enabled: form.querySelector('input[name="enabled"]').checked,
          channel_id: fd.get("channel_id") || "0",
          completion_code: fd.get("completion_code") || "",
          stale_hours: parseFloat(fd.get("stale_hours")) || 24,
          steps: steps.map((s) => ({
            key: s.key || "",
            label: s.label.trim(),
            auto: s.auto || "",
            role_id: s.role_id || "0",
          })),
        });
        showStatus(status, true, "Saved");
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
