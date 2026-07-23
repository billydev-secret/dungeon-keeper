import { api, apiPost, apiDelete, esc } from "../api.js";
import { showStatus, guardForm, mountPicker, mountRolePicker } from "../config-helpers.js";
import { toast } from "../ui.js";

// Saveable-profile fields owners may persist between sessions.
const SAVEABLE_FIELDS = [
  ["name", "Room name"],
  ["limit", "User limit"],
  ["access", "Room access"],
  ["trusted", "Trust list"],
  ["blocked", "Block list"],
];

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading Voice Master configuration…</div></div>`;

  (async () => {
    let cfg, channels, roles;
    try {
      [cfg, channels, roles] = await Promise.all([
        api("/api/voice-master/config"),
        api("/api/meta/channels?types=text,voice,category"),
        api("/api/meta/roles"),
      ]);
    } catch (err) {
      container.innerHTML = `<div class="panel"><div class="empty">Failed to load: ${esc(err.message)}</div></div>`;
      return;
    }

    const numField = (name, label, value, hint, { min = 0, max = null } = {}) => `
      <div class="field">
        <label for="vm-${name}">${esc(label)}</label>
        <input type="number" name="${name}" id="vm-${name}" required min="${min}"${max != null ? ` max="${max}"` : ""}
          step="1" value="${esc(String(value ?? 0))}" style="max-width:140px;" />
        ${hint ? `<div class="field-hint">${hint}</div>` : ""}
      </div>`;

    const toggleField = (name, label, checked, hint) => `
      <div class="field">
        <label style="display:flex; gap:6px; align-items:center;">
          <input type="checkbox" name="${name}"${checked ? " checked" : ""} /> ${esc(label)}
        </label>
        <div class="field-hint">${hint}</div>
      </div>`;

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Voice Master</h2>
          <div class="subtitle">Member-owned voice channels created by joining the Hub</div>
        </header>
        <form class="form form-cards" data-form>
          <div class="card">
            <div class="section-label">Wiring</div>
            <div class="field">
              <label>Hub Channel</label>
              <span data-picker="hub_channel_id"></span>
              <div class="field-hint">The voice channel members join to spin up their own room. "(unset)" turns Voice Master off.</div>
            </div>
            <div class="field">
              <label>Target Category</label>
              <span data-picker="category_id"></span>
              <div class="field-hint">Created rooms are placed under this category.</div>
            </div>
            <div class="field">
              <label>Control Channel</label>
              <span data-picker="control_channel_id"></span>
              <div class="field-hint">Text channel that hosts the persistent control panel and knock requests.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">New-Room Defaults</div>
            <div class="field">
              <label for="vm-default_name_template">Default Name Template</label>
              <input type="text" name="default_name_template" id="vm-default_name_template"
                value="${esc(cfg.default_name_template || "")}" />
              <div class="field-hint">Name given to a freshly created room. Tokens: {display_name}, {username}.</div>
            </div>
            ${numField("default_user_limit", "Default User Limit", cfg.default_user_limit,
              "How many members a new room admits. 0 means no cap.")}
            ${numField("default_bitrate", "Default Bitrate (bits per second)", cfg.default_bitrate,
              "Audio quality for new rooms, e.g. 64000. 0 uses the highest bitrate the server's boost tier allows.")}
            ${toggleField("post_inline_panel", "Post the control panel in each new room's chat", cfg.post_inline_panel,
              "When checked, every new room gets its own copy of the control panel in its text chat, so owners don't have to find the control channel.")}
          </div>

          <div class="card">
            <div class="section-label">Limits & Cleanup</div>
            ${numField("create_cooldown_s", "Create Cooldown (seconds)", cfg.create_cooldown_s,
              "A member must wait this long between creating rooms. 0 means no cooldown.")}
            ${numField("max_per_member", "Max Rooms per Member", cfg.max_per_member,
              "How many rooms one member can own at once.", { min: 1 })}
            ${numField("owner_grace_s", "Owner-Disconnect Grace (seconds)", cfg.owner_grace_s,
              "After the owner disconnects, the room waits this long before offering the Claim button to whoever is left.")}
            ${numField("empty_grace_s", "Empty-Room Grace (seconds)", cfg.empty_grace_s,
              "An empty room is deleted after this long, giving members a window to hop back in.")}
          </div>

          <div class="card">
            <div class="section-label">Trust & Access</div>
            ${numField("trust_cap", "Trust List Cap", cfg.trust_cap,
              "The most members a room owner can add to their trust list. 0 means no cap.")}
            ${numField("block_cap", "Block List Cap", cfg.block_cap,
              "The most members a room owner can block. 0 means no cap.")}
            ${numField("trusted_prune_days", "Trusted-Entry Expiry (days)", cfg.trusted_prune_days,
              "Trust-list entries unused for this many days are dropped automatically. 0 keeps them forever.")}
            <div class="field">
              <label>Spectator Gate Role</label>
              <span data-picker="spectator_gate_role_id"></span>
              <div class="field-hint">If set, only members with this role can join spectator-mode rooms. Others see the room exists but can't join or read its chat (Discord ties the voice text chat to the Connect permission). Leave as "(none — open to everyone)" to let anyone spectate; non-members can then still read, just not speak.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Saved Layouts</div>
            ${toggleField("saves_enabled", "Save member layouts", !cfg.disable_saves,
              "When checked, room owners can save their room setup (name, limit, access, lists) and get it back next time. Unchecked, every new room starts from the defaults above.")}
            <div class="field">
              <label>Saveable Fields</label>
              <div style="display:flex; flex-wrap:wrap; gap:8px 16px;">
                ${SAVEABLE_FIELDS.map(([value, label]) => `
                  <label style="display:flex; gap:6px; align-items:center;">
                    <input type="checkbox" name="saveable_fields" value="${value}"${(cfg.saveable_fields || []).includes(value) ? " checked" : ""} /> ${esc(label)}
                  </label>`).join("")}
              </div>
              <div class="field-hint">Which parts of a room's setup owners may persist. "Room access" covers the single open / NSFW / locked / spectator state.</div>
            </div>
          </div>

          <div style="display:flex; gap:8px; align-items:center;">
            <button type="submit" class="btn btn-primary">Save</button>
            <span data-status></span>
          </div>
        </form>

        <div class="section-label" style="margin-top:20px;">Post How-To Guide</div>
        <form class="form" data-howto-form>
          <div class="field">
            <label>Guide Channel</label>
            <span data-picker="howto_channel_id"></span>
            <div class="field-hint">Posts a member-facing "how Voice Master works" embed here (e.g. in your lobby). Safe to re-run anytime.</div>
          </div>
          <div style="display:flex; gap:8px; align-items:center;">
            <button type="submit" class="btn">Post Guide</button>
            <span data-howto-status></span>
          </div>
        </form>

        <div class="section-label" style="margin-top:20px;">Room-Name Blocklist</div>
        <div class="field-hint" style="margin-bottom:8px;">Room names containing any of these phrases are rejected. Matching ignores upper/lower case.</div>
        <form class="form" data-bl-form style="display:flex; gap:8px; align-items:center;">
          <label for="vm-bl-input" class="visually-hidden" style="position:absolute; left:-9999px;">Blocked phrase</label>
          <input type="text" id="vm-bl-input" placeholder="Phrase to block…" />
          <button type="submit" class="btn">Add</button>
        </form>
        <ul data-bl-list style="margin-top:10px;"></ul>
      </div>`;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");

    const voiceOptions = channels
      .filter((c) => c.type === "voice")
      .map((c) => ({ id: String(c.id), label: `🔊 ${c.name}` }));
    const categoryOptions = channels
      .filter((c) => c.type === "category")
      .map((c) => ({ id: String(c.id), label: `📁 ${c.name}` }));
    const textOptions = channels
      .filter((c) => c.type === "text")
      .map((c) => ({ id: String(c.id), label: `#${c.name}` }));

    const hubPicker = mountPicker(
      form.querySelector('[data-picker="hub_channel_id"]'),
      voiceOptions, String(cfg.hub_channel_id || "0"),
      { emptyValue: "0", emptyLabel: "(unset)", label: "Hub Channel" },
    );
    const categoryPicker = mountPicker(
      form.querySelector('[data-picker="category_id"]'),
      categoryOptions, String(cfg.category_id || "0"),
      { emptyValue: "0", emptyLabel: "(unset)", label: "Target Category" },
    );
    const controlPicker = mountPicker(
      form.querySelector('[data-picker="control_channel_id"]'),
      textOptions, String(cfg.control_channel_id || "0"),
      { emptyValue: "0", emptyLabel: "(unset)", label: "Control Channel" },
    );
    const spectatorPicker = mountRolePicker(
      form.querySelector('[data-picker="spectator_gate_role_id"]'),
      roles, String(cfg.spectator_gate_role_id || "0"),
      { emptyLabel: "(none — open to everyone)", label: "Spectator Gate Role" },
    );

    guardForm(form);

    // Client-side validation names the offending field (W-C5).
    const NUM_FIELDS = [
      ["default_user_limit", "Default User Limit", 0],
      ["default_bitrate", "Default Bitrate", 0],
      ["create_cooldown_s", "Create Cooldown", 0],
      ["max_per_member", "Max Rooms per Member", 1],
      ["trust_cap", "Trust List Cap", 0],
      ["block_cap", "Block List Cap", 0],
      ["owner_grace_s", "Owner-Disconnect Grace", 0],
      ["empty_grace_s", "Empty-Room Grace", 0],
      ["trusted_prune_days", "Trusted-Entry Expiry", 0],
    ];

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const nums = {};
      for (const [name, label, min] of NUM_FIELDS) {
        const v = parseInt(fd.get(name), 10);
        if (!Number.isFinite(v) || v < min) {
          showStatus(status, false, `${label} must be a number ≥ ${min}`);
          form.querySelector(`[name=${name}]`).focus();
          return;
        }
        nums[name] = v;
      }
      const saveable = [...form.querySelectorAll('input[name="saveable_fields"]:checked')].map((el) => el.value);
      const payload = {
        // Snowflakes stay strings end-to-end; "0" is the unset sentinel.
        hub_channel_id: hubPicker.getValue() || "0",
        category_id: categoryPicker.getValue() || "0",
        control_channel_id: controlPicker.getValue() || "0",
        default_name_template: String(fd.get("default_name_template") || ""),
        ...nums,
        disable_saves: !fd.has("saves_enabled"),
        saveable_fields: saveable,
        post_inline_panel: fd.has("post_inline_panel"),
        spectator_gate_role_id: spectatorPicker.getValue() || "0",
      };
      try {
        await apiPost("/api/voice-master/config", payload);
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });

    // ── How-to guide ──────────────────────────────────────────────────
    const howtoForm = container.querySelector("[data-howto-form]");
    const howtoStatus = container.querySelector("[data-howto-status]");
    const howtoPicker = mountPicker(
      howtoForm.querySelector('[data-picker="howto_channel_id"]'),
      textOptions, "0",
      { emptyValue: "0", emptyLabel: "(pick a channel)", label: "Guide Channel" },
    );

    howtoForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const channel_id = howtoPicker.getValue() || "0";
      if (channel_id === "0") {
        showStatus(howtoStatus, false, "Pick a channel first");
        return;
      }
      try {
        await apiPost("/api/voice-master/post-howto", { channel_id });
        showStatus(howtoStatus, true, "Guide posted");
      } catch (err) {
        showStatus(howtoStatus, false, err.message);
      }
    });

    // ── Name blocklist ────────────────────────────────────────────────
    const blForm = container.querySelector("[data-bl-form]");
    const blInput = container.querySelector("#vm-bl-input");
    const blList = container.querySelector("[data-bl-list]");

    function renderList(patterns) {
      blList.textContent = "";
      for (const p of patterns) {
        const li = document.createElement("li");
        li.textContent = p + " ";
        const del = document.createElement("button");
        del.type = "button";
        del.className = "btn btn-danger btn-sm";
        del.textContent = "Remove";
        del.addEventListener("click", async () => {
          try {
            await apiDelete("/api/voice-master/name-blocklist/" + encodeURIComponent(p));
            li.remove();
          } catch (err) {
            toast("Remove failed: " + err.message, "error");
          }
        });
        li.appendChild(del);
        blList.appendChild(li);
      }
    }
    renderList(cfg.name_blocklist || []);

    blForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const pattern = blInput.value.trim();
      if (!pattern) return;
      try {
        await apiPost("/api/voice-master/name-blocklist", { pattern });
        blInput.value = "";
        const fresh = await api("/api/voice-master/config");
        renderList(fresh.name_blocklist || []);
      } catch (err) {
        toast("Add failed: " + err.message, "error");
      }
    });
  })();
}
