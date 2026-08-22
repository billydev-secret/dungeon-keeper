// Meadow Mahjong — the feature's entire admin surface (spec §8; plan stage 7).
// Card management (upload → server-side linter with inline errors → set
// active / schedule / archive), house rules, stakes with escrow preview, and
// the tables report. Members play from Discord; their card viewer is the
// /mahjong panel's button (backed by the same member-tier API this panel
// reads for its own card preview).

import { api, apiPost, apiPut } from "../api.js";
import {
  guardForm,
  loadMembers,
  memberNameLookup,
  mountAsync,
  showStatus,
} from "../config-helpers.js";
import { renderSortableTable } from "../table.js";

const esc = (s) => String(s ?? "")
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;");

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading Meadow Mahjong…</div></div>`;

  return mountAsync(container, async () => {
    const [config, report, members] = await Promise.all([
      api("/api/mahjong/config"),
      api("/api/mahjong/report"),
      loadMembers().catch(() => []),
    ]);
    const nameOf = memberNameLookup(members);
    const s = config.settings;

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Meadow Mahjong</h2>
          <div class="subtitle">Card-driven American-style mahjong — tables run in
            Discord; everything configurable lives here.</div>
        </header>

        <form class="form form-cards" data-form>
          <div class="card">
            <div class="section-label">House Rules</div>
            <div class="field">
              <label for="mj-enabled">Open for Play</label>
              <select name="enabled" id="mj-enabled">
                <option value="1" ${s.enabled ? "selected" : ""}>Yes — members can open tables</option>
                <option value="0" ${s.enabled ? "" : "selected"}>No — /mahjong says it isn't open</option>
              </select>
            </div>
            <div class="field">
              <label for="mj-turn">Turn Timer (seconds)</label>
              <input type="number" name="turn_timer" id="mj-turn" required
                min="10" max="300" step="1" value="${s.turn_timer}" style="max-width:140px;" />
              <div class="field-hint">How long a player has to discard before the
                drawn tile goes automatically (and a ⚠ strike accrues — three in a
                row folds the seat).</div>
            </div>
            <div class="field">
              <label for="mj-claim4">Claim Window — Full Table (seconds)</label>
              <input type="number" name="claim_window_4" id="mj-claim4" required
                min="3" max="60" step="1" value="${s.claim_window_4}" style="max-width:140px;" />
            </div>
            <div class="field">
              <label for="mj-claim2">Claim Window — Duel (seconds)</label>
              <input type="number" name="claim_window_2" id="mj-claim2" required
                min="3" max="60" step="1" value="${s.claim_window_2}" style="max-width:140px;" />
              <div class="field-hint">How long the table waits for Mahjong / Call /
                Pass after each discard. It closes early once everyone answers.</div>
            </div>
            <div class="field">
              <label for="mj-phase">Charleston &amp; Courtesy Timer (seconds)</label>
              <input type="number" name="phase_timer" id="mj-phase" required
                min="15" max="600" step="1" value="${s.phase_timer}" style="max-width:140px;" />
              <div class="field-hint">The clock on each simultaneous step (passes,
                the vote, courtesy). Absent players are auto-resolved with a strike.</div>
            </div>
            <div class="field">
              <label for="mj-trim">Duel Wall Trim (tiles)</label>
              <input type="number" name="duel_wall_trim" id="mj-trim" required
                min="0" max="100" step="1" value="${s.duel_wall_trim}" style="max-width:140px;" />
              <div class="field-hint">Dead tiles removed from a Duel wall at the
                deal. 0 plays the full 152-tile wall — a long head-to-head; around 60
                makes a brisk hand and more wall games.</div>
            </div>
            <div class="field">
              <label for="mj-second">Second Charleston</label>
              <select name="second_charleston" id="mj-second">
                <option value="1" ${s.second_charleston ? "selected" : ""}>Offered (unanimous vote runs it)</option>
                <option value="0" ${s.second_charleston ? "" : "selected"}>Off — straight to courtesy</option>
              </select>
            </div>
            <div class="field">
              <label for="mj-assist">Default Assistance</label>
              <select name="assist_default" id="mj-assist">
                ${[["off", "Off — pure card-reading"],
                   ["target", "Target — closest hands + distance"],
                   ["gap", "Target + gap — ...and the tiles still needed"],
                   ["coach", "Coach — ...and a suggested discard"]].map(([v, label]) =>
                  `<option value="${v}" ${s.assist_default === v ? "selected" : ""}>${label}</option>`).join("")}
              </select>
              <div class="field-hint">What a member sees before they pick their own
                level in the /mahjong panel's My Settings. A member's own choice
                always wins.</div>
            </div>
            <div class="field">
              <label for="mj-practice">Practice Tables</label>
              <select name="practice_bots" id="mj-practice">
                <option value="1" ${s.practice_bots ? "selected" : ""}>Open — solo play against house bots, no stakes</option>
                <option value="0" ${s.practice_bots ? "" : "selected"}>Closed</option>
              </select>
              <div class="field-hint">Stake-free and recorded nowhere — nothing a
                practice game does touches coins, stats, or quests.</div>
            </div>
            <div class="field">
              <label for="mj-fill">House Bots in Real Games</label>
              <select name="fill_bots" id="mj-fill">
                <option value="0" ${s.fill_bots ? "" : "selected"}>Off — humans only at staked tables</option>
                <option value="1" ${s.fill_bots ? "selected" : ""}>On — the host can seat a house-staked bot</option>
              </select>
              <div class="field-hint">The bot's escrow is house money and every coin
                of it shows in the ledger. Leave off until practice games have
                proven the bot plays well enough not to be farmed.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Stakes</div>
            <div class="field">
              <label for="mj-stakes">Allowed Stakes (coins per point)</label>
              <input type="text" name="stakes_allowed" id="mj-stakes" required
                value="${esc(s.stakes_allowed.join(", "))}" style="max-width:200px;" />
              <div class="field-hint">Comma-separated. Escrow per seat is the card
                max × 6 (Duel) or × 4 (full table) × the stake — the preview below
                uses the active card.</div>
            </div>
            <div data-escrow-preview></div>
          </div>

          <div style="display:flex; gap:8px; align-items:center;">
            <button type="submit" class="btn btn-primary">Save</button>
            <span data-status></span>
          </div>
        </form>

        <div class="card" style="margin-top:16px;">
          <div class="section-label">Meadow Cards</div>
          <div data-cards></div>
          <details style="margin-top:12px;">
            <summary>Upload a card (JSON)</summary>
            <div class="field" style="margin-top:8px;">
              <textarea data-card-json rows="8" style="width:100%; font-family:monospace;"
                placeholder='{"card_id": "...", "display_name": "...", "season": "...", "hands": [...]}'></textarea>
              <div class="field-hint">Validated server-side against the card grammar
                and linter — every problem reports at once, nothing half-saves.</div>
            </div>
            <button type="button" class="btn" data-upload>Validate &amp; Save</button>
            <span data-upload-status></span>
            <div data-upload-errors style="margin-top:8px;"></div>
          </details>
        </div>

        <div class="card" style="margin-top:16px;">
          <div class="section-label">Live Tables</div>
          <div data-tables></div>
        </div>
        <div class="card" style="margin-top:16px;">
          <div class="section-label">Recent Hands</div>
          <div data-results></div>
        </div>
        <div class="card" style="margin-top:16px;">
          <div class="section-label">Player Aggregates</div>
          <div data-aggregates></div>
        </div>
      </div>
    `;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");
    guardForm(form);

    renderEscrowPreview(container.querySelector("[data-escrow-preview]"), config);
    renderCards(container.querySelector("[data-cards]"), config.cards, status);
    renderReport(container, report, nameOf);
    wireUpload(container);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const stakes = String(fd.get("stakes_allowed") || "")
        .split(",").map((x) => parseInt(x.trim(), 10))
        .filter((n) => Number.isFinite(n) && n >= 1 && n <= 100);
      if (!stakes.length) {
        showStatus(status, false, "Allowed Stakes needs at least one number from 1 to 100");
        form.querySelector("[name=stakes_allowed]").focus();
        return;
      }
      try {
        const saved = await apiPut("/api/mahjong/config", {
          enabled: fd.get("enabled") === "1",
          claim_window_4: parseFloat(fd.get("claim_window_4")),
          claim_window_2: parseFloat(fd.get("claim_window_2")),
          turn_timer: parseFloat(fd.get("turn_timer")),
          phase_timer: parseFloat(fd.get("phase_timer")),
          duel_wall_trim: parseInt(fd.get("duel_wall_trim"), 10),
          second_charleston: fd.get("second_charleston") === "1",
          stakes_allowed: stakes,
          assist_default: fd.get("assist_default"),
          practice_bots: fd.get("practice_bots") === "1",
          fill_bots: fd.get("fill_bots") === "1",
        });
        form.querySelector("[name=stakes_allowed]").value = saved.stakes_allowed.join(", ");
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });

    function wireUpload(root) {
      const btn = root.querySelector("[data-upload]");
      const box = root.querySelector("[data-card-json]");
      const uploadStatus = root.querySelector("[data-upload-status]");
      const errBox = root.querySelector("[data-upload-errors]");
      btn.addEventListener("click", async () => {
        errBox.innerHTML = "";
        let card;
        try {
          card = JSON.parse(box.value);
        } catch {
          showStatus(uploadStatus, false, "That isn't valid JSON.");
          return;
        }
        try {
          const res = await apiPost("/api/mahjong/cards", { card });
          if (!res.ok) {
            showStatus(uploadStatus, false, `${res.errors.length} problem(s)`);
            const ul = document.createElement("ul");
            for (const msg of [...res.errors, ...(res.warnings || []).map((w) => `warning: ${w}`)]) {
              const li = document.createElement("li");
              li.textContent = msg; // textContent — card errors quote user input
              ul.appendChild(li);
            }
            errBox.appendChild(ul);
            return;
          }
          showStatus(uploadStatus, true,
            res.warnings?.length ? `Saved with ${res.warnings.length} warning(s)` : "Saved");
          await remount();
        } catch (err) {
          showStatus(uploadStatus, false, err.message);
        }
      });
    }

    function renderCards(el, cards, statusEl) {
      if (!cards.length) {
        el.innerHTML = `<div class="empty">No cards uploaded yet.</div>`;
        return;
      }
      renderSortableTable(el, {
        columns: [
          { key: "display_name", label: "Card" },
          { key: "season", label: "Season" },
          { key: "hands", label: "Hands" },
          { key: "max_value", label: "Max" },
          { key: "status", label: "Status" },
          {
            key: "row_id", label: "", html: true,
            format: (v, row) => row.status === "active"
              ? ""
              : `<button class="btn btn-sm" data-activate="${Number(v)}">Set Active</button>`,
          },
        ],
        data: cards,
        emptyMsg: "No cards.",
      });
      el.addEventListener("click", async (e) => {
        const btn = e.target.closest("[data-activate]");
        if (!btn) return;
        try {
          await apiPost(`/api/mahjong/cards/${Number(btn.dataset.activate)}/status`,
            { status: "active" });
          await remount();
        } catch (err) {
          showStatus(statusEl, false, err.message);
        }
      });
    }

    function renderEscrowPreview(el, cfg) {
      if (!cfg.escrow_preview) {
        el.innerHTML = `<div class="field-hint">No active card — activate one to see escrow.</div>`;
        return;
      }
      const stakes = cfg.settings.stakes_allowed;
      const rows = [["Mode", ...stakes.map((st) => `${st}/pt`)]];
      for (const [mode, byStake] of Object.entries(cfg.escrow_preview)) {
        rows.push([
          mode === "2" ? "Duel" : "Full Table",
          ...stakes.map((st) => `${byStake[String(st)]} coins`),
        ]);
      }
      const table = document.createElement("table");
      table.className = "data-table";
      rows.forEach(([...cells], i) => {
        const tr = document.createElement("tr");
        for (const c of cells) {
          const td = document.createElement(i === 0 ? "th" : "td");
          td.textContent = String(c);
          tr.appendChild(td);
        }
        table.appendChild(tr);
      });
      const wrap = document.createElement("div");
      wrap.style.overflowX = "auto";
      wrap.appendChild(table);
      el.replaceChildren(wrap);
    }

    function renderReport(root, rep, lookupName) {
      renderSortableTable(root.querySelector("[data-tables]"), {
        columns: [
          { key: "mode", label: "Mode", format: (v) => (v === 2 ? "Duel" : "Full Table") },
          { key: "stake", label: "Stake" },
          { key: "host_id", label: "Host", format: (v) => lookupName(v) || v },
          { key: "channel_id", label: "Channel" },
          { key: "created_at", label: "Opened", format: fmtTime },
        ],
        data: rep.tables,
        emptyMsg: "No live tables.",
      });
      renderSortableTable(root.querySelector("[data-results]"), {
        columns: [
          { key: "created_at", label: "When", format: fmtTime },
          { key: "mode", label: "Mode", format: (v) => (v === 2 ? "Duel" : "Full") },
          { key: "kind", label: "End" },
          { key: "winner_id", label: "Winner", format: (v) => (v ? lookupName(v) || v : "—") },
          { key: "line_name", label: "Line", format: (v) => v || "—" },
          { key: "base_value", label: "Pts" },
          { key: "jokerless", label: "Jokerless", format: (v) => (v ? "✓" : "") },
        ],
        data: rep.results,
        defaultSort: "created_at",
        defaultAsc: false,
        emptyMsg: "No hands settled yet.",
      });
      renderSortableTable(root.querySelector("[data-aggregates]"), {
        columns: [
          { key: "user_id", label: "Member", format: (v) => lookupName(v) || v },
          { key: "mode", label: "Mode", format: (v) => (v === 2 ? "Duel" : "Full") },
          { key: "hands_played", label: "Hands" },
          { key: "wins", label: "Wins" },
          { key: "jokerless_wins", label: "Jokerless" },
          { key: "coins_won", label: "Won" },
          { key: "coins_lost", label: "Lost" },
          { key: "biggest_win", label: "Best" },
        ],
        data: rep.aggregates,
        defaultSort: "coins_won",
        defaultAsc: false,
        emptyMsg: "Nobody on the board yet.",
      });
    }

    function fmtTime(v) {
      if (!v) return "—";
      return new Date(v * 1000).toLocaleString();
    }

    function remount() {
      return mount(container);
    }
  }, { errorMsg: "Couldn’t load the Meadow Mahjong settings." });
}
