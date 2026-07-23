// QA Tracker — admin board for the volunteer testing crew (Dev section).
// Board of test cards with expandable verdicts (void with clawback, archive,
// jump to the Discord card), a top-testers scoreboard, and the config knobs
// the cog reads live (role, channel, reward, daily cap, enabled).
import { api, apiPost, apiPut, esc } from "../api.js";
import { toast, confirmDialog } from "../ui.js";
import { makeFilterStrip } from "../tab-strip.js";
import { renderLoading, renderEmpty, renderError } from "../states.js";
import { syncHash } from "../report-helpers.js";
import {
  showStatus, loadRoles, loadChannels, loadMembers,
  mountRolePicker, mountChannelPicker,
} from "../config-helpers.js";

// Semantic status colors — mirror the Discord card (cards.py STATUS_COLORS).
const STATUS_COLORS = {
  pending:  "#95A5A6",
  passed:   "#2ECC71",
  failed:   "#E74C3C",
  blocked:  "#E67E22",
  archived: "#7F8C8D",
};

const VERDICT_EMOJI = { pass: "✅", fail: "❌", blocked: "🚧" };

function statusChip(status) {
  const c = STATUS_COLORS[status] || STATUS_COLORS.pending;
  return `<span class="t-chip" style="background:${c}22;color:${c}">${esc(status)}</span>`;
}

function fmtWhen(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return String(iso);
  return d.toLocaleString(undefined, {
    day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit",
  });
}

function tally(test) {
  const counts = { pass: 0, fail: 0, blocked: 0 };
  for (const v of test.verdicts) {
    if (!v.voided && counts[v.verdict] !== undefined) counts[v.verdict] += 1;
  }
  return ["pass", "fail", "blocked"]
    .map((k) => `${VERDICT_EMOJI[k]} ${counts[k]}`)
    .join(" · ");
}

export function mount(container, initialParams = {}) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>QA Tracker</h2>
        <div class="subtitle">Verdict cards in the testing queue — void bogus verdicts, archive retired entries, tune the crew's knobs.</div>
      </header>

      <section class="card">
        <div class="section-label" style="display:flex;align-items:center;justify-content:space-between;gap:12px;">
          <span>Board</span>
          <div class="ctrl-group" role="group" aria-label="Filter tests" data-filter-group>
            <button class="active" data-filter="all">All</button>
            <button data-filter="pending">Pending</button>
            <button data-filter="passed">Passed</button>
            <button data-filter="failed">Failed</button>
            <button data-filter="blocked">Blocked</button>
            <button data-filter="archived">Archived</button>
          </div>
        </div>
        <div data-board>${renderLoading("Loading the board…")}</div>
      </section>

      <section class="card">
        <div class="section-label">Top Testers</div>
        <div data-testers>${renderLoading("Loading top testers…")}</div>
      </section>

      <section class="card">
        <div class="section-label">Config</div>
        <form class="form" data-form>${renderLoading("Loading settings…")}</form>
      </section>
    </div>
  `;

  const boardEl = container.querySelector("[data-board]");
  const testersEl = container.querySelector("[data-testers]");
  const formEl = container.querySelector("[data-form]");
  const filterGroup = container.querySelector("[data-filter-group]");

  const FILTER_VALUES = ["all", "pending", "passed", "failed", "blocked", "archived"];
  const state = {
    tests: [],
    members: [],
    filter: FILTER_VALUES.includes(initialParams.filter) ? initialParams.filter : "all",
    expandedId: initialParams.test ? Number(initialParams.test) : null,
  };
  for (const btn of filterGroup.querySelectorAll("[data-filter]")) {
    const on = btn.dataset.filter === state.filter;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
  }

  /** Mirror the board filter and the expanded card into the URL (W-D9). */
  function pushHash() {
    syncHash("qa-tracker", {
      filter: state.filter === "all" ? "" : state.filter,
      test: state.expandedId || "",
    });
  }

  function memberLabel(id) {
    const m = state.members.find((x) => String(x.id) === String(id));
    return m ? (m.display_name || m.name) : `@${id}`;
  }

  // ── board ───────────────────────────────────────────────────────────

  function verdictRow(v) {
    const who = esc(memberLabel(v.user_id));
    const label = `${VERDICT_EMOJI[v.verdict] || ""} ${esc(v.verdict)}`;
    const note = v.note ? ` — <span style="color:var(--ink-dim)">${esc(v.note)}</span>` : "";
    const paid = v.paid_amount ? `paid ${v.paid_amount}` : "unpaid";
    if (v.voided) {
      const by = esc(memberLabel(v.voided_by));
      return `
        <div class="qa-verdict" style="padding:4px 0;color:var(--ink-mute);">
          <s>${who} · ${label}${note} · ${paid}</s>
          <span style="margin-left:8px;font-size:12px;">voided by ${by} · ${esc(fmtWhen(v.voided_at))}</span>
        </div>`;
    }
    return `
      <div class="qa-verdict" style="display:flex;align-items:center;gap:10px;padding:4px 0;">
        <span>${who} · ${label}${note} · ${paid} · ${esc(fmtWhen(v.created_at))}</span>
        <button type="button" class="btn btn-ghost btn-sm" data-void="${v.id}" data-paid="${v.paid_amount}">Void</button>
      </div>`;
  }

  function expandedRow(t) {
    const verdicts = t.verdicts.length
      ? t.verdicts.map(verdictRow).join("")
      : `<div style="color:var(--ink-mute);padding:4px 0;">No verdicts yet.</div>`;
    const jump = t.jump_url
      ? `<a class="btn btn-ghost btn-sm" href="${esc(t.jump_url)}" target="_blank" rel="noopener">Open in Discord</a>`
      : "";
    const archive = t.status !== "archived"
      ? `<button type="button" class="btn btn-ghost btn-sm" data-archive="${t.id}">Archive</button>`
      : "";
    return `
      <tr class="qa-expand" data-expand-for="${t.id}">
        <td colspan="5" style="background:var(--bg-floor);padding:8px 14px;">
          ${verdicts}
          <div style="display:flex;gap:8px;margin-top:6px;">${jump}${archive}</div>
        </td>
      </tr>`;
  }

  function renderBoard() {
    const tests = state.filter === "all"
      ? state.tests
      : state.tests.filter((t) => t.status === state.filter);
    pushHash();
    if (!tests.length) {
      boardEl.innerHTML = renderEmpty(state.filter === "all"
        ? "No test cards yet. The post-commit hook posts one for every commit that ships a Testing checklist."
        : `No ${state.filter} test cards. Pick All to see the rest of the board.`);
      return;
    }
    const rows = tests.map((t) => {
      const sha = t.commit_sha ? String(t.commit_sha).slice(0, 7) : "—";
      const main = `
        <tr class="qa-row" data-test-id="${t.id}" style="cursor:pointer;"
            tabindex="0" role="button" aria-expanded="${t.id === state.expandedId}">
          <td>${esc(t.title)}</td>
          <td>${statusChip(t.status)}</td>
          <td style="font-family:var(--mono);font-size:12px;">${esc(sha)}</td>
          <td>${tally(t)}</td>
          <td>${esc(fmtWhen(t.updated_at))}</td>
        </tr>`;
      return t.id === state.expandedId ? main + expandedRow(t) : main;
    }).join("");
    boardEl.innerHTML = `
      <div style="overflow-x:auto;">
        <table class="data-table">
          <thead><tr><th>Title</th><th>Status</th><th>Commit</th><th>Verdicts</th><th>Updated</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  }

  async function refreshBoard() {
    try {
      const data = await api("/api/qa/tests");
      state.tests = data.tests || [];
      renderBoard();
    } catch (err) {
      boardEl.innerHTML = renderError(`Couldn't load the QA board — ${err.message}. Reload the page to try again.`);
    }
  }

  boardEl.addEventListener("click", async (e) => {
    const voidBtn = e.target.closest("[data-void]");
    if (voidBtn) {
      const paid = Number(voidBtn.dataset.paid) || 0;
      const note = paid ? ` This claws back up to ${paid} coins.` : "";
      if (!(await confirmDialog(`Void this verdict?${note}`, { danger: true, confirmLabel: "Void" }))) return;
      voidBtn.disabled = true;
      try {
        const out = await apiPost(`/api/qa/verdicts/${voidBtn.dataset.void}/void`, {});
        toast(out.clawed ? `Verdict voided — clawed back ${out.clawed} coins` : "Verdict voided");
        await Promise.all([refreshBoard(), refreshTesters()]);
      } catch (err) {
        toast(err.message, "error");
        voidBtn.disabled = false;
      }
      return;
    }

    const archiveBtn = e.target.closest("[data-archive]");
    if (archiveBtn) {
      if (!(await confirmDialog("Archive this test? The card keeps its history but the buttons are removed.", { confirmLabel: "Archive" }))) return;
      archiveBtn.disabled = true;
      try {
        await apiPost(`/api/qa/tests/${archiveBtn.dataset.archive}/archive`, {});
        toast("Test archived");
        await refreshBoard();
      } catch (err) {
        toast(err.message, "error");
        archiveBtn.disabled = false;
      }
      return;
    }

    if (e.target.closest("a")) return; // let the Discord link through
    const row = e.target.closest(".qa-row");
    if (!row) return;
    toggleRow(row);
  });

  // Rows are role="button" tabindex="0" — activate with Enter/Space too.
  boardEl.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    const row = e.target.closest(".qa-row");
    if (!row) return;
    e.preventDefault();
    toggleRow(row, { keepFocus: true });
  });

  function toggleRow(row, { keepFocus = false } = {}) {
    const id = Number(row.dataset.testId);
    state.expandedId = state.expandedId === id ? null : id;
    renderBoard();
    // renderBoard() rebuilds the table, so a keyboard user's focus would be
    // dropped onto <body>; put it back on the row they just activated.
    if (keepFocus) boardEl.querySelector(`.qa-row[data-test-id="${id}"]`)?.focus();
  }

  makeFilterStrip(filterGroup, (value) => {
    state.filter = value;
    state.expandedId = null;
    renderBoard();
  });

  // ── top testers ─────────────────────────────────────────────────────

  async function refreshTesters() {
    try {
      const data = await api("/api/qa/top-testers");
      const testers = data.testers || [];
      if (!testers.length) {
        testersEl.innerHTML = renderEmpty("No verdicts recorded yet. Testers appear here once they start clicking Pass, Fail, or Blocked on the cards.");
        return;
      }
      const rows = testers.map((t) => `
        <tr>
          <td>${esc(memberLabel(t.user_id))}</td>
          <td style="text-align:right;">${t.verdicts}</td>
          <td style="text-align:right;">${t.coins}</td>
        </tr>`).join("");
      testersEl.innerHTML = `
        <div style="overflow-x:auto;">
          <table class="data-table">
            <thead><tr><th>Member</th><th style="text-align:right;">Verdicts</th><th style="text-align:right;">Coins earned</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>`;
    } catch (err) {
      testersEl.innerHTML = renderError(`Couldn't load top testers — ${err.message}. Reload the page to try again.`);
    }
  }

  // ── config ──────────────────────────────────────────────────────────

  async function initConfig() {
    let settings, roles, channels;
    try {
      [settings, roles, channels] = await Promise.all([
        api("/api/qa/settings"), loadRoles(), loadChannels(),
      ]);
    } catch (err) {
      formEl.innerHTML = renderError(`Couldn't load QA settings — ${err.message}. Reload the page to try again.`);
      return;
    }

    formEl.innerHTML = `
      <div class="field">
        <label><input type="checkbox" name="enabled" ${settings.enabled ? "checked" : ""} /> Enabled</label>
        <div class="field-hint">Off pauses verdict buttons; existing cards stay put.</div>
      </div>
      <div class="field-row">
        <div class="field"><label>QA Crew Role</label>
          <span data-picker="role"></span>
          <div class="field-hint">Members allowed to click verdict buttons. (none) = admins only.</div></div>
        <div class="field"><label>Cards Channel</label>
          <span data-picker="channel"></span>
          <div class="field-hint">Where the post-commit hook posts new cards.</div></div>
      </div>
      <div class="field-row">
        <div class="field"><label for="qa-reward">Reward Per Verdict</label>
          <input id="qa-reward" type="number" name="reward" min="0" max="10000" step="1" value="${Number(settings.reward) || 0}" style="max-width:120px;" /></div>
        <div class="field"><label for="qa-daily-cap">Daily Paid Cap</label>
          <input id="qa-daily-cap" type="number" name="daily_cap" min="0" max="1000" step="1" value="${Number(settings.daily_cap) || 0}" style="max-width:120px;" />
          <div class="field-hint">Paid verdicts per tester per day; later verdicts still record, unpaid.</div></div>
      </div>
      <div><button type="submit" class="btn btn-primary">Save</button><span data-status></span></div>
    `;

    const rolePicker = mountRolePicker(
      formEl.querySelector('[data-picker="role"]'), roles, settings.role_id,
      { label: "QA crew role" });
    const channelPicker = mountChannelPicker(
      formEl.querySelector('[data-picker="channel"]'), channels, settings.channel_id,
      { label: "Cards channel" });
    const status = formEl.querySelector("[data-status]");

    formEl.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(formEl);
      try {
        await apiPut("/api/qa/settings", {
          enabled: fd.get("enabled") !== null,
          // Snowflakes exceed Number.MAX_SAFE_INTEGER — they must travel as
          // strings or the low digits round off (Pydantic coerces server-side).
          role_id: rolePicker.getValue() || "0",
          channel_id: channelPicker.getValue() || "0",
          reward: parseInt(fd.get("reward"), 10) || 0,
          daily_cap: parseInt(fd.get("daily_cap"), 10) || 0,
        });
        showStatus(status, true);
        toast("QA settings saved");
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  }

  (async () => {
    state.members = await loadMembers().catch(() => []);
    await Promise.all([refreshBoard(), refreshTesters(), initConfig()]);
  })();

  return { unmount() {} };
}
