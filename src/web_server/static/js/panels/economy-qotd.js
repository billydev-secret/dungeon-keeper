// QOTD — the knobs for the question of the day a mod posts with /qotd post.
// Reads and writes the shared econ_ settings via /api/economy/config, so this
// page is admin-gated like Economy Settings. The reward itself stays on
// Income Sources with the other faucets; this page owns the ping.
import { api } from "../api.js";
import { apiPut, showStatus, loadRoles, mountRolePicker } from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading QOTD config…</div></div>`;

  (async () => {
    const [cfg, roles] = await Promise.all([api("/api/economy/config"), loadRoles()]);
    render(container, cfg, roles);
  })();

  return null;
}

function render(container, cfg, roles) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>QOTD</h2>
        <div class="subtitle">Question of the Day — a mod tags the QOTD role, members reply</div>
      </header>

      <form class="form card" data-form>
        <div class="section-label">The QOTD role</div>
        <div class="field">
          <label>QOTD role</label>
          <span data-picker="qotd_ping_role_id"></span>
          <div class="field-hint">Does two jobs. The bot mentions it when a mod runs
            <code>/qotd post</code>, <strong>and</strong> any message from a mod that
            tags it becomes that day's question — so a mod can just ask in their own
            words. Leave as <em>(none)</em> to post silently and turn tag-to-ask off.</div>
          <div class="field-hint">Restrict who may mention it in Discord's role
            settings — a member tagging it does nothing here either way, since only
            admins and the manager role can open a question.</div>
          <div class="field-hint">The role must be <strong>mentionable</strong> in
            Discord's role settings — otherwise the mention posts as plain text and
            nobody is notified. (Granting the bot “Mention @everyone, @here, and All
            Roles” also works.)</div>
        </div>

        <div style="display:flex; gap:8px; align-items:center; margin-top:16px;">
          <button type="submit" class="btn btn-primary">Save</button>
          <span data-status></span>
        </div>
      </form>

      <section class="card">
        <div class="section-label">How it works</div>
        <div class="field-hint">
          A mod asks the question two ways: type it normally and <strong>tag the QOTD
          role</strong> in the message, or run <code>/qotd post &lt;question&gt;</code>
          to have the bot render it as a card (that path also posts the queued
          sponsored questions). Either way, every member who <strong>replies to that
          message</strong> earns
          <strong>${cfg.reward_qotd}</strong> ${cfg.reward_qotd === 1 ? cfg.currency_name : cfg.currency_plural},
          once per question. Replies stop paying once the guild-local day rolls over,
          so yesterday's question can't be farmed. Change that award on
          <a href="#/economy-income-sources">Income Sources</a>. Who may open a question
          is the manager role on <a href="#/economy-config">Settings</a>.
        </div>
      </section>
    </div>`;

  const form = container.querySelector("[data-form]");
  const status = form.querySelector("[data-status]");

  const pingRolePicker = mountRolePicker(
    form.querySelector('[data-picker="qotd_ping_role_id"]'),
    roles,
    String(cfg.qotd_ping_role_id),
  );

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await apiPut("/api/economy/config", {
        // String, not parseInt: a 19-digit snowflake loses its low digits as a
        // JS number. Pydantic coerces it back to int losslessly.
        qotd_ping_role_id: pingRolePicker.getValue() || "0",
      });
      showStatus(status, true);
    } catch (err) {
      showStatus(status, false, err.message);
    }
  });
}
