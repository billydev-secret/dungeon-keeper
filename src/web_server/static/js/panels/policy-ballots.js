import { api, esc, fmtTs } from "../api.js";
import { mountAsync } from "../config-helpers.js";
import { renderSortableTable } from "../table.js";
import { renderEmpty, renderError } from "../states.js";

/**
 * The community-ballot half of the Policy Tickets page, mounted into a region
 * by panels/policy-tickets.js beneath the proposal queue. Read-only: a ballot
 * is opened, voted in and closed in Discord, and this page is where the result
 * and the roll call are legible afterwards.
 *
 * Nothing here is privileged information. A ballot's tally card names every
 * voter in the channel while it runs — fully public was Billy's 2026-09-03
 * decision — so this shows moderators what the room already sees, laid out
 * so it can be read rather than scrolled.
 */

const OUTCOME_BADGE = {
  passed:    '<span class="badge badge-success">Passed</span>',
  failed:    '<span class="badge badge-danger">Failed</span>',
  cancelled: '<span class="badge badge-dim">Cancelled</span>',
};

const CHOICE_LABEL = { yes: "Yes", no: "No", abstain: "Abstain" };

function outcomeCell(b) {
  if (!b.closed_at) return '<span class="badge badge-warning">Open</span>';
  return OUTCOME_BADGE[b.outcome] || esc(b.outcome || "Closed");
}

function voterNames(votes, choice) {
  return votes
    .filter((v) => v.choice === choice)
    .map((v) => v.user_name || `User ${v.user_id}`);
}

export function mountBallots(container) {
  container.innerHTML = `<div class="empty">Loading community ballots…</div>`;

  // The first load goes through mountAsync with no inner catch, so a failed
  // fetch renders a real error with a working Retry instead of a permanent
  // spinner. The 45s poll below has its own guard: a failed *refresh* must not
  // wipe a table a moderator is reading.
  const async_ = mountAsync(container, async () => {
    container.innerHTML = `
      <div>
        <div class="section-label">Community Ballots</div>
        <div class="field-hint" style="margin-bottom:10px;">
          Questions put to everyone who could see the channel they were launched in.
          A ballot passes on a simple majority — abstentions count for neither side and
          ties fail — and a passed ballot is <strong>recorded, not enacted</strong>:
          turning one into a policy is still a moderator's decision.
        </div>
        <div class="table-scroll" data-ballot-table></div>
        <div data-ballot-detail></div>
      </div>
    `;

    const tableWrap = container.querySelector("[data-ballot-table]");
    const detailWrap = container.querySelector("[data-ballot-detail]");
    let ballots = [];
    let selectedId = null;

    function renderDetail() {
      const ballot = ballots.find((b) => b.id === selectedId);
      if (!ballot) {
        detailWrap.innerHTML = "";
        return;
      }
      const sections = ["yes", "no", "abstain"].map((choice) => {
        const names = voterNames(ballot.votes || [], choice);
        return `
          <div class="field">
            <label>${CHOICE_LABEL[choice]} (${names.length})</label>
            <div class="field-hint">${names.length ? esc(names.join(", ")) : "Nobody"}</div>
          </div>
        `;
      }).join("");
      detailWrap.innerHTML = `
        <div class="card" style="margin-top:12px;">
          <div class="section-label">Roll call</div>
          <div class="field-hint" style="margin-bottom:10px;">${esc(ballot.question)}</div>
          ${sections}
        </div>
      `;
    }

    function renderTable() {
      if (!ballots.length) {
        tableWrap.innerHTML = renderEmpty(
          "No community ballots yet. An admin opens one with the policy ballot "
          + "command in the channel whose members should vote.",
        );
        detailWrap.innerHTML = "";
        return;
      }
      renderSortableTable(tableWrap, {
        columns: [
          { key: "closed_at", label: "Result", html: true, format: (_v, row) => outcomeCell(row) },
          { key: "id", label: "ID", format: (v) => `#${v}` },
          { key: "question", label: "Question" },
          { key: "opened_by_name", label: "Opened by", format: (v, row) => v || row.opened_by },
          { key: "opened_at", label: "Opened", format: (v) => fmtTs(v) },
          { key: "yes_count", label: "Yes" },
          { key: "no_count", label: "No" },
          { key: "abstain_count", label: "Abstain" },
          // html:true, and the only thing interpolated is the row's own
          // integer id — never a name or anything else a member controls.
          {
            key: "id", label: "Voters", html: true,
            format: (v) => `<button type="button" class="btn btn-sm" data-rollcall="${Number(v)}">Show</button>`,
          },
        ],
        data: ballots,
        defaultSort: "opened_at",
        defaultAsc: false,
        maxRows: 200,
      });
      renderDetail();
    }

    // One delegated listener on the wrapper, not one per row: the sortable
    // table replaces its tbody on every re-sort and every poll, and per-row
    // listeners would stack up behind those replacements.
    tableWrap.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-rollcall]");
      if (!btn) return;
      const id = parseInt(btn.dataset.rollcall, 10);
      selectedId = selectedId === id ? null : id;
      renderDetail();
    });

    async function load() {
      const data = await api("/api/moderation/policy-ballots");
      ballots = data.ballots || [];
      // A refresh that recovers clears the note the failed one left, so the
      // page never carries a stale "couldn't refresh" over live figures.
      container.querySelector("[data-refresh-note]")?.remove();
      renderTable();
    }

    await load();

    const poll = setInterval(() => {
      if (document.hidden) return;
      load().catch((err) => {
        // A failed refresh leaves the table it already drew in place and says
        // so, rather than replacing real figures with an error or — worse —
        // leaving stale ones there silently.
        const note = container.querySelector("[data-refresh-note]");
        const html = renderError(`Couldn’t refresh the ballots — ${err.message}`);
        if (note) note.innerHTML = html;
        else tableWrap.insertAdjacentHTML("beforebegin", `<div data-refresh-note>${html}</div>`);
      });
    }, 45000);

    return { unmount() { clearInterval(poll); } };
  }, { errorMsg: "Couldn’t load the community ballots." });

  return async_;
}
