/**
 * No-contact list — the moderator surface.
 *
 * Members protect themselves with `/nocontact` in Discord; this page is for
 * the things a member can't do: acting on someone else's behalf, seeing the
 * whole list, and setting where alerts land.
 *
 * A deliberate asymmetry worth knowing before editing: moderators see every
 * entry here, including ones members set for themselves. Members see only
 * their own — an entry the other party created is invisible to them, because
 * showing it would tell someone they'd been blocked, which is the disclosure
 * the feature exists to prevent.
 */
import { api, apiPost, apiDelete, esc, fmtTs } from "../api.js";
import {
  loadChannels, loadRoles, loadMembers,
  mountChannelPicker, mountRolePicker, mountMemberPicker,
  showStatus,
} from "../config-helpers.js";
import { confirmDialog } from "../ui.js";

const KIND_LABEL = {
  attempt: "Blocked attempt",
  mention: "Mention",
  reply: "Reply",
};

// Built once per mount from the member list. A `.find()` per cell turned each
// render into a full scan of every guild member — ~200 scans for a 100-row
// event table alone, repeated on every refresh.
function nameLookup(members) {
  const byId = new Map(
    members.map((m) => [String(m.id), m.display_name || m.name]),
  );
  return (id) => {
    if (!id) return "—";
    return byId.get(String(id)) || `User ${id}`;
  };
}

function pairRow(p, memberName) {
  const a = memberName(p.user_low);
  const b = memberName(p.user_high);
  const protectedName = p.protected_user_id
    ? memberName(p.protected_user_id)
    : null;
  const protectedCell = protectedName
    ? esc(protectedName)
    : `<span class="field-hint">Mutual — neither can lift it</span>`;
  return `
    <tr>
      <td class="user-cell">${esc(a)} ⇄ ${esc(b)}</td>
      <td>${protectedCell}</td>
      <td class="reason-cell">${esc(p.reason) || "—"}</td>
      <td>${fmtTs(p.created_at)}</td>
      <td>
        <button class="btn btn-danger btn-sm" data-remove
          data-a="${esc(p.user_low)}" data-b="${esc(p.user_high)}">Remove</button>
      </td>
    </tr>`;
}

function eventRow(e, memberName) {
  const what = KIND_LABEL[e.kind] || e.kind;
  const detail = e.surface_label ? ` — ${esc(e.surface_label)}` : "";
  return `
    <tr>
      <td>${esc(what)}${detail}</td>
      <td class="user-cell">${esc(memberName(e.actor_id))}</td>
      <td class="user-cell">${esc(memberName(e.target_id))}</td>
      <td>${fmtTs(e.created_at)}</td>
    </tr>`;
}

// One table shell for both tables below — they differed only in headers, row
// builder and empty copy, so any change to the scroll wrapper or header markup
// had to be made twice.
function renderTable(el, rows, { headers, rowFn, empty }) {
  if (!rows.length) {
    el.innerHTML = `<div class="empty">${empty}</div>`;
    return;
  }
  el.innerHTML = `
    <div class="table-scroll">
      <table class="table">
        <thead><tr>${headers.map((h) => `<th>${h}</th>`).join("")}</tr></thead>
        <tbody>${rows.map(rowFn).join("")}</tbody>
      </table>
    </div>`;
}

export function mount(container) {
  container.innerHTML = `<div class="empty">Loading no-contact list…</div>`;

  (async () => {
    const [data, channels, roles, members] = await Promise.all([
      api("/api/no-contact/overview"),
      loadChannels(), loadRoles(), loadMembers(),
    ]);

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>🚫 No-Contact List</h2>
          <div class="subtitle">Pairs of members the bot will never put in contact</div>
        </header>

        <div class="field-hint" style="border:1px solid var(--rule); border-radius:6px; padding:10px; margin-bottom:18px; line-height:1.5;">
          An entry blocks both directions across <strong>every</strong> feature that can carry
          contact — whispers, AMA questions, confession replies, Guess Who, Pen Pals matching,
          voice rooms, and DM requests.
          <strong>Neither member is told an entry exists</strong>, and the blocked party
          cannot tell: refused actions look exactly like ordinary ones. Members can add
          their own entries with <code>/nocontact</code> in Discord.
        </div>

        <section style="margin-bottom:28px;">
          <div class="section-label">Alerts</div>
          <div class="field-hint" style="margin-bottom:10px;">
            Where to report it when one member of a pair mentions or replies to the other.
            Enforcement works whether or not this is set — only the notification is optional.
            The protected member is never notified.
          </div>
          <form class="form" data-settings-form>
            <div class="field">
              <label>Alert Channel</label>
              <span data-picker="alert_channel_id"></span>
            </div>
            <div class="field">
              <label>Ping Role</label>
              <span data-picker="alert_role_id"></span>
              <div class="field-hint">Pinged on each alert. Choose "(none)" to post without a ping.</div>
            </div>
            <button type="submit" class="btn btn-primary">Save alerts</button>
            <span data-settings-status></span>
          </form>
        </section>

        <section style="margin-bottom:28px;">
          <div class="section-label">Add an entry</div>
          <div class="field-hint" style="margin-bottom:10px;">
            For acting on a member's behalf — a third-party report, or someone who
            won't file it themselves.
          </div>
          <form class="form" data-add-form>
            <div class="field">
              <label>Member A</label>
              <span data-picker="user_a"></span>
            </div>
            <div class="field">
              <label>Member B</label>
              <span data-picker="user_b"></span>
            </div>
            <div class="field">
              <label for="nc-protect">Who can lift this?</label>
              <select name="protect" id="nc-protect">
                <option value="a">Member A only</option>
                <option value="b">Member B only</option>
                <option value="mutual">Neither — moderators only</option>
              </select>
              <div class="field-hint">
                The protected member is the only one who can remove it, so that the other
                party can't undo it — including by pressuring them into it. Choose
                "neither" for a mutual separation. Moderators can always remove any entry.
              </div>
            </div>
            <div class="field">
              <label for="nc-reason">Reason</label>
              <input type="text" name="reason" id="nc-reason" maxlength="500"
                placeholder="Only moderators ever see this." />
            </div>
            <button type="submit" class="btn btn-primary">Add entry</button>
            <span data-add-status></span>
          </form>
        </section>

        <section style="margin-bottom:28px;">
          <div class="section-label">Current entries</div>
          <div data-pairs></div>
        </section>

        <section>
          <div class="section-label">Recent activity</div>
          <div class="field-hint" style="margin-bottom:10px;">
            Blocked attempts and mention/reply alerts. Visible to moderators only.
          </div>
          <div data-events></div>
        </section>
      </div>
    `;

    const settingsForm = container.querySelector("[data-settings-form]");
    const channelPicker = mountChannelPicker(
      settingsForm.querySelector('[data-picker="alert_channel_id"]'),
      channels, data.settings.alert_channel_id, { label: "Alert Channel" },
    );
    const rolePicker = mountRolePicker(
      settingsForm.querySelector('[data-picker="alert_role_id"]'),
      roles, data.settings.alert_role_id, { label: "Ping Role" },
    );

    settingsForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const status = settingsForm.querySelector("[data-settings-status]");
      try {
        // Send ids as STRINGS. A Discord snowflake exceeds 2^53, so Number()
        // silently rounds it — "1420895763219492864" becomes …900 — and the
        // saved channel would then resolve to nothing. Pydantic coerces the
        // string to int server-side, losing nothing.
        await apiPost("/api/no-contact/settings", {
          alert_channel_id: channelPicker.getValue() || "0",
          alert_role_id: rolePicker.getValue() || "0",
        });
        showStatus(status, true, "Saved.");
      } catch (err) {
        showStatus(status, false, String(err.message || err));
      }
    });

    const addForm = container.querySelector("[data-add-form]");
    const pickA = mountMemberPicker(
      addForm.querySelector('[data-picker="user_a"]'), members, "0",
      { label: "Member A" },
    );
    const pickB = mountMemberPicker(
      addForm.querySelector('[data-picker="user_b"]'), members, "0",
      { label: "Member B" },
    );

    const pairsEl = container.querySelector("[data-pairs]");
    const eventsEl = container.querySelector("[data-events]");

    const memberName = nameLookup(members);

    function renderPairs(pairs) {
      renderTable(pairsEl, pairs, {
        headers: ["Pair", "Protects", "Reason", "Added", ""],
        rowFn: (p) => pairRow(p, memberName),
        empty: "No entries. Members can add their own with <code>/nocontact</code>.",
      });
    }

    function renderEvents(events) {
      renderTable(eventsEl, events, {
        headers: ["What", "Who", "Toward", "When"],
        rowFn: (e) => eventRow(e, memberName),
        empty: "Nothing recorded yet.",
      });
    }

    async function refresh() {
      const fresh = await api("/api/no-contact/overview");
      renderPairs(fresh.pairs);
      renderEvents(fresh.events);
    }

    renderPairs(data.pairs);
    renderEvents(data.events);

    addForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const status = addForm.querySelector("[data-add-status]");
      const a = pickA.getValue();
      const b = pickB.getValue();
      if (!a || a === "0" || !b || b === "0") {
        showStatus(status, false, "Pick two members.");
        return;
      }
      if (a === b) {
        showStatus(status, false, "Pick two different members.");
        return;
      }
      const choice = addForm.querySelector('[name="protect"]').value;
      const protectedId = choice === "a" ? a : choice === "b" ? b : null;
      try {
        // Strings, not Number() — see the settings save above. Rounding a
        // member id here would write a pair naming two users who don't exist,
        // so the entry would list in this panel while enforcing nothing.
        await apiPost("/api/no-contact/pairs", {
          user_a: a,
          user_b: b,
          protected_user_id: protectedId == null ? null : protectedId,
          reason: addForm.querySelector('[name="reason"]').value || "",
        });
        addForm.querySelector('[name="reason"]').value = "";
        showStatus(status, true, "Added.");
        await refresh();
      } catch (err) {
        showStatus(status, false, String(err.message || err));
      }
    });

    pairsEl.addEventListener("click", async (ev) => {
      const btn = ev.target.closest("[data-remove]");
      if (!btn) return;
      const ok = await confirmDialog(
        "These two members will be able to reach each other through the bot again. Neither is notified.",
        { title: "Remove this no-contact entry?", confirmLabel: "Remove", danger: true },
      );
      if (!ok) return;
      await apiDelete(`/api/no-contact/pairs/${btn.dataset.a}/${btn.dataset.b}`);
      await refresh();
    });
  })().catch((err) => {
    container.innerHTML = `<div class="empty">Couldn't load the no-contact list: ${esc(String(err.message || err))}</div>`;
  });
}
