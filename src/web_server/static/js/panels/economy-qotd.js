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
        <div class="subtitle">Question of the Day — posted by a mod with <code>/qotd post</code></div>
      </header>

      <form class="form card" data-form>
        <div class="section-label">Ping</div>
        <div class="field">
          <label>Ping role</label>
          <span data-picker="qotd_ping_role_id"></span>
          <div class="field-hint">Mentioned above the question card each time a
            question is posted. Leave as <em>(none)</em> to post silently.</div>
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
          A mod runs <code>/qotd post &lt;question&gt;</code> in the channel they want
          the question in — the bot renders it as a card and posts it there. Every
          member who then posts a message in that channel earns
          <strong>${cfg.reward_qotd}</strong> ${cfg.reward_qotd === 1 ? cfg.currency_name : cfg.currency_plural},
          once per question, until the guild-local day rolls over. Change that award on
          <a href="#/economy-income-sources">Income Sources</a>. Who may post a question
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
        qotd_ping_role_id: parseInt(pingRolePicker.getValue() || "0", 10),
      });
      showStatus(status, true);
    } catch (err) {
      showStatus(status, false, err.message);
    }
  });
}
