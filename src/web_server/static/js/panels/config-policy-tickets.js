import { loadConfig, apiPut, showStatus, buildField } from "../config-helpers.js";

export function mount(container) {
  container.textContent = "";
  const loading = document.createElement("div");
  loading.className = "panel";
  loading.innerHTML = '<div class="empty">Loading config…</div>';
  container.appendChild(loading);

  (async () => {
    const config = await loadConfig();
    const p = config.policy || {};
    const currentHours = Number.isInteger(p.vote_timeout_hours) ? p.vote_timeout_hours : 72;

    container.textContent = "";
    const panel = document.createElement("div");
    panel.className = "panel";

    const header = document.createElement("header");
    const h2 = document.createElement("h2");
    h2.textContent = "Policy Tickets";
    const sub = document.createElement("div");
    sub.className = "subtitle";
    sub.textContent = "Voting rules for policy proposals";
    header.appendChild(h2);
    header.appendChild(sub);
    panel.appendChild(header);

    const form = document.createElement("form");
    form.className = "form";

    const input = document.createElement("input");
    input.type = "number";
    input.name = "vote_timeout_hours";
    input.min = "1";
    input.max = "720";
    input.value = String(currentHours);
    form.appendChild(
      buildField(
        "Vote Timeout (hours)",
        input,
        "After this many hours, mods who haven't voted are treated as absent and ignored. A 'no' from anyone still rejects; if nobody has voted, the policy fails.",
      ),
    );

    const actions = document.createElement("div");
    const submitBtn = document.createElement("button");
    submitBtn.type = "submit";
    submitBtn.className = "btn btn-primary";
    submitBtn.textContent = "Save";
    const status = document.createElement("span");
    actions.appendChild(submitBtn);
    actions.appendChild(status);
    form.appendChild(actions);

    panel.appendChild(form);
    container.appendChild(panel);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const hours = parseInt(input.value, 10);
      if (!hours || hours < 1) {
        showStatus(status, false, "Hours must be at least 1");
        return;
      }
      try {
        await apiPut("/api/config/policy", { vote_timeout_hours: hours });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
