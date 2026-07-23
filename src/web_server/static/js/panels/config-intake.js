import { esc, loadConfig, loadChannels, loadRoles, channelSelect, roleSelect, apiPut, showStatus } from "../config-helpers.js";

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
