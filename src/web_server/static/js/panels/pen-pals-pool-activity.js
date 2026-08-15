import { api } from "../api.js";
import { loadMembers, memberNameLookup, mountAsync } from "../config-helpers.js";
import { renderSortableTable } from "../table.js";

/**
 * Recent movement in and out of the Pen Pals matching pool.
 *
 * The pool itself is current-state only — a row per waiting member, gone the
 * moment they match or leave — so "is this pool healthy?" was unanswerable
 * from the dashboard. The Golden Meadow's sat at one member for five days with
 * nothing to show it had ever been fuller. This is that history.
 *
 * Deliberately a plain log rather than a chart: the useful reading is the most
 * recent dozen rows and what caused them, not a trend line.
 */

// reason → how it reads to a moderator who did not write the code.
const REASONS = {
  panel: "used the signup panel",
  command: "ran /penpals",
  dm: "used the button in their DM",
  requeue_expired: "chat ended — back in the pool",
  requeue_abnormal: "chat ended early — back in the pool",
  backfill: "seeded from an earlier chat",
  matched: "matched with a pen pal",
  departed: "left the server",
  inactive: "never posted — left out of the pool",
};

const ACTIONS = {
  join: "Joined",
  leave: "Left",
  skip: "Left out",
};

export function mountPoolActivity(container) {
  container.innerHTML = `<div class="empty">Loading pool activity…</div>`;

  return mountAsync(container, async () => {
    const [{ events }, members] = await Promise.all([
      api("/api/config/pen-pals/pool-events"),
      loadMembers(),
    ]);
    const nameOf = memberNameLookup(members);

    container.innerHTML = `
      <div class="section-label">Pool Activity</div>
      <div class="field-hint" style="margin-bottom:12px;">
        The last ${events.length} change${events.length === 1 ? "" : "s"} to who is waiting
        to be matched. A pool that stops moving is a pool that has stopped making pairs.
      </div>
      <div data-region="table"></div>
    `;

    renderSortableTable(container.querySelector('[data-region="table"]'), {
      columns: [
        {
          key: "at",
          label: "When",
          format: (v) => new Date(v * 1000).toLocaleString(),
        },
        { key: "user_id", label: "Member", format: (v) => nameOf(v) },
        { key: "action", label: "What", format: (v) => ACTIONS[v] || v },
        { key: "reason", label: "Why", format: (v) => REASONS[v] || v },
      ],
      data: events,
      defaultSort: "at",
      defaultAsc: false,
      emptyMsg:
        "Nothing yet. Joins, matches and departures show up here as they happen.",
      maxRows: 50,
    });
  });
}
