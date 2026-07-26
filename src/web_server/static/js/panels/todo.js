import { api, apiPost, apiPut, apiDelete, esc, fmtTs, fmtAge } from "../api.js";
import { makeFilterStrip } from "../tab-strip.js";
import { renderLoading, renderEmpty, renderError } from "../states.js";
import { syncHash } from "../report-helpers.js";
import { guardForm, loadChannels, mountChannelPicker, showStatus } from "../config-helpers.js";
import { confirmDialog, toast } from "../ui.js";

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/** "09:30" ⇄ minutes since local midnight — the API's time_of_day unit. */
function timeToMinutes(value) {
  const [hh, mm] = String(value || "0:0").split(":").map(Number);
  return (hh || 0) * 60 + (mm || 0);
}

function minutesToTime(minutes) {
  const hh = Math.floor((minutes || 0) / 60);
  const mm = (minutes || 0) % 60;
  return `${String(hh).padStart(2, "0")}:${String(mm).padStart(2, "0")}`;
}

function renderList(todos, activeId, filter) {
  if (!todos.length) {
    return renderEmpty(filter === "completed"
      ? "Nothing completed yet. Tasks move here once someone marks them done."
      : filter === "pending"
        ? "No pending tasks — the list is clear. Add one below, or use /todo in Discord."
        : "No tasks yet. Add one below, or use /todo in Discord.");
  }
  return todos
    .map((t) => {
      const cls = (t.completed_at ? "low" : "med") + (t.id === activeId ? " active" : "");
      const age = fmtAge(Date.now() / 1000 - t.created_at) + " ago";
      const preview = t.task.length > 80 ? t.task.slice(0, 77) + "…" : t.task;
      const chip = t.completed_at
        ? '<span class="t-chip closed" style="margin-left:4px">Done</span>'
        : '<span class="t-chip open" style="margin-left:4px">Pending</span>';
      return `
      <div class="ticket-item ${esc(cls)}" data-todo-id="${esc(t.id)}">
        <div class="pri"></div>
        <div class="body">
          <div class="subj">${esc(preview)}</div>
          <div class="row">
            <span>${esc(t.added_by_name || t.added_by)}</span>
            ${chip}
          </div>
        </div>
        <div class="right">
          <span class="id">#${esc(t.id)}</span>
          <span class="age">${esc(age)}</span>
        </div>
      </div>`;
    })
    .join("");
}

function renderDetail(t, completing) {
  if (!t) {
    return renderEmpty("Select a task from the list to view its details.");
  }
  const completedLine = t.completed_at
    ? `<div class="td-section">Completed</div>
       <div style="padding:4px 8px 8px;font-size:14px;color:var(--ink)">
         ${esc(fmtTs(t.completed_at))} by <b>${esc(t.completed_by_name || t.completed_by || "unknown")}</b>
       </div>`
    : "";
  const completeBtn = !t.completed_at
    ? `<div class="td-actions">
        <span class="act-spacer"></span>
        <button class="act-btn" data-action="complete" ${completing ? "disabled" : ""}>
          ${completing ? "Completing…" : "Mark Complete"}
        </button>
       </div>`
    : "";
  const statusChip = t.completed_at
    ? '<span class="t-chip closed">Done</span>'
    : '<span class="t-chip open">Pending</span>';
  const descriptionBlock = t.description
    ? `<div class="td-section">Description</div>
       <div style="font-size:14px;color:var(--ink);line-height:1.5;white-space:pre-wrap;word-break:break-word;padding:4px 8px 8px">${esc(t.description)}</div>`
    : "";
  const sourceBlock = t.source_message_url
    ? `<div class="td-section">Source</div>
       <div style="padding:4px 8px 8px;font-size:14px;">
         <a href="${esc(t.source_message_url)}" target="_blank" rel="noopener noreferrer" style="color:var(--accent, #5af);word-break:break-all">Jump to message ↗</a>
       </div>`
    : "";
  return `
    <div class="td-head">
      <div class="td-crumb">#${esc(t.id)} &nbsp;&middot;&nbsp; added ${esc(fmtAge(Date.now() / 1000 - t.created_at))} ago</div>
      <h3 class="td-title" style="word-break:break-word">${esc(t.task)}</h3>
      <div class="td-meta">
        <span class="pair"><span class="k">Added by</span><b>${esc(t.added_by_name || t.added_by)}</b></span>
        <span class="pair"><span class="k">Added</span><b>${esc(fmtTs(t.created_at))}</b></span>
        <span class="pair"><span class="k">Status</span>${statusChip}</span>
      </div>
    </div>
    <div class="td-body">
      <div class="td-section">Task</div>
      <div style="font-size:14px;color:var(--ink);line-height:1.5;white-space:pre-wrap;word-break:break-word;padding:4px 8px 8px">${esc(t.task)}</div>
      ${descriptionBlock}
      ${sourceBlock}
      ${completedLine}
    </div>
    ${completeBtn}`;
}

const FILTERS = {
  pending:   (t) => !t.completed_at,
  completed: (t) => !!t.completed_at,
  all:       () => true,
};

/** One row of the recurring-task list. */
function recurringRow(item) {
  const paused = item.status === "paused";
  const next = item.next_run_at && !paused
    ? `next ${esc(fmtTs(item.next_run_at))}`
    : paused ? "paused" : "not scheduled";
  const skipped = item.last_status === "skipped_pending"
    ? ' <span class="t-chip" title="The previous one is still on the list, so no duplicate was added.">last run skipped</span>'
    : "";
  return `
    <tr data-recurring-id="${esc(item.id)}"${paused ? ' style="opacity:.6"' : ""}>
      <td>
        <div style="font-weight:600;word-break:break-word">${esc(item.task)}</div>
        ${item.description
          ? `<div style="font-size:12px;color:var(--ink-dim);word-break:break-word">${esc(item.description)}</div>`
          : ""}
      </td>
      <td style="white-space:nowrap">${esc(item.cadence)}</td>
      <td style="white-space:nowrap;font-size:12px;color:var(--ink-dim)">${next}${skipped}</td>
      <td style="white-space:nowrap;text-align:right">
        <button class="act-btn" data-act="run-now">Run now</button>
        <button class="act-btn" data-act="toggle">${paused ? "Resume" : "Pause"}</button>
        <button class="act-btn" data-act="edit">Edit</button>
        <button class="act-btn" data-act="delete">Delete</button>
      </td>
    </tr>`;
}

function recurringTable(items) {
  if (!items.length) {
    return renderEmpty(
      "No recurring tasks yet. Add one below — a reminder appears on the list " +
      "each time it comes due, like “Post QOTD” every morning."
    );
  }
  return `
    <div style="overflow-x:auto">
      <table class="table" data-recurring>
        <thead><tr><th>Task</th><th>Repeats</th><th>Status</th><th>Actions</th></tr></thead>
        <tbody>${items.map(recurringRow).join("")}</tbody>
      </table>
    </div>`;
}

function recurringEditor(editing) {
  const weekly = (editing?.recurrence || "daily") === "weekly";
  const days = new Set(editing?.recur_days || []);
  return `
    <form data-recurring-form class="form" style="margin-top:12px">
      <div class="field">
        <label for="rec-task">Task</label>
        <input id="rec-task" type="text" data-rec="task" maxlength="500" required
               placeholder="Post QOTD" value="${esc(editing?.task || "")}" />
        <div class="field-hint">What a moderator needs to do. This is the text that lands on the list.</div>
      </div>
      <div class="field">
        <label for="rec-desc">Notes (optional)</label>
        <input id="rec-desc" type="text" data-rec="description" maxlength="1000"
               placeholder="Use the sponsored question queue if there is one."
               value="${esc(editing?.description || "")}" />
      </div>
      <div class="field-row" style="display:flex;gap:12px;flex-wrap:wrap">
        <div class="field">
          <label for="rec-recurrence">Repeats</label>
          <select id="rec-recurrence" data-rec="recurrence">
            <option value="daily"${weekly ? "" : " selected"}>Daily</option>
            <option value="weekly"${weekly ? " selected" : ""}>Weekly</option>
          </select>
        </div>
        <div class="field">
          <label for="rec-time">At</label>
          <input id="rec-time" type="time" data-rec="time"
                 value="${esc(minutesToTime(editing?.time_of_day ?? 540))}" />
          <div class="field-hint">Server local time.</div>
        </div>
      </div>
      <div class="field" data-rec-days style="${weekly ? "" : "display:none"}">
        <label>On these days</label>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          ${WEEKDAYS.map((d, i) => `
            <label style="display:flex;align-items:center;gap:4px;font-weight:400">
              <input type="checkbox" data-rec-day="${i}"${days.has(i) ? " checked" : ""} /> ${d}
            </label>`).join("")}
        </div>
      </div>
      <div class="td-actions">
        <button type="submit" class="btn btn-primary" data-rec-save>
          ${editing ? "Save Changes" : "Add Recurring Task"}
        </button>
        ${editing ? '<button type="button" class="act-btn" data-rec-cancel>Cancel</button>' : ""}
        <span data-rec-status style="font-size:12px"></span>
      </div>
    </form>`;
}

export function mount(container, initialParams = {}) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Todo List</h2>
        <div class="subtitle">Server-wide tasks added via <code>/todo</code>.</div>
      </header>

      <div class="mod-stats" data-stats>
        <div class="mod-stat open"><div class="lbl">Pending</div><div class="v">—</div></div>
        <div class="mod-stat resolved"><div class="lbl">Completed</div><div class="v">—</div></div>
      </div>

      <section class="mod-split">
        <div class="ticket-list-wrap">
          <div class="ticket-list-head">
            <h3>Tasks</h3>
            <div class="ctrl-group" role="group" aria-label="Filter tasks" data-filter-group>
              <button class="active" data-filter="pending">Pending</button>
              <button data-filter="completed">Completed</button>
              <button data-filter="all">All</button>
            </div>
          </div>
          <div class="ticket-list" data-list>
            ${renderLoading("Loading tasks…")}
          </div>
        </div>
        <div class="ticket-detail" data-detail>
          ${renderLoading("Loading tasks…")}
        </div>
      </section>

      <form class="todo-add" data-add-form style="display:flex;gap:8px;margin-top:12px;align-items:flex-start;">
        <input type="text" data-add-input maxlength="500" placeholder="Add a new task…" aria-label="New task"
               style="flex:1;padding:8px 10px;border:1px solid var(--rule);border-radius:4px;background:var(--bg-rail);color:var(--ink);font-size:13px;font-family:inherit;" />
        <button type="submit" class="act-btn" data-add-btn>Add</button>
        <span data-add-status style="font-size:12px;align-self:center;"></span>
      </form>

      <section class="card" data-board-card style="margin-top:20px">
        <div class="section-label">Discord Board</div>
        <div class="field-hint" style="margin-bottom:8px">
          A live board pinned to the bottom of a channel, with ➕ Add and ✅ Complete
          buttons. It updates itself as tasks come and go, and hops back down when
          people chat. The buttons are moderator-only.
        </div>
        <div data-board-body>${renderLoading("Loading board…")}</div>
      </section>

      <section class="card" style="margin-top:20px">
        <div class="section-label">Recurring Tasks</div>
        <div class="field-hint" style="margin-bottom:8px">
          Chores that land on the list on a schedule — “Post QOTD” every morning,
          “Photo challenge prompt” every Monday. These are <b>reminders</b>: the bot
          adds the task, a moderator does it and ticks it off. If the last one is
          still outstanding no duplicate is added, so a missed day shows as one
          ageing task rather than a pile.
        </div>
        <div data-recurring-body>${renderLoading("Loading recurring tasks…")}</div>
      </section>
    </div>
  `;

  const statsEl = container.querySelector("[data-stats]");
  const listEl = container.querySelector("[data-list]");
  const detailEl = container.querySelector("[data-detail]");
  const filterGroup = container.querySelector("[data-filter-group]");
  const addForm = container.querySelector("[data-add-form]");
  const addInput = container.querySelector("[data-add-input]");
  const addBtn = container.querySelector("[data-add-btn]");
  const addStatus = container.querySelector("[data-add-status]");

  const boardBody = container.querySelector("[data-board-body]");
  const recurringBody = container.querySelector("[data-recurring-body]");

  const state = {
    todos: [],
    filter: Object.keys(FILTERS).includes(initialParams.filter) ? initialParams.filter : "pending",
    activeId: initialParams.task ? Number(initialParams.task) : null,
    completing: false,
    board: null,
    canManageBoard: false,
    channels: [],
    recurring: [],
    editingRecurringId: null,
    busy: false,
  };
  for (const btn of filterGroup.querySelectorAll("[data-filter]")) {
    const on = btn.dataset.filter === state.filter;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
  }

  /** Mirror the tab and selected task into the URL (W-D9). */
  function pushHash() {
    syncHash("todo", {
      filter: state.filter === "pending" ? "" : state.filter,
      task: state.activeId || "",
    });
  }

  function render() {
    const filtered = state.todos.filter(FILTERS[state.filter]);
    if (!filtered.find((t) => t.id === state.activeId)) {
      state.activeId = filtered[0]?.id ?? null;
    }
    listEl.innerHTML = renderList(filtered, state.activeId, state.filter);
    const active = state.todos.find((t) => t.id === state.activeId) || null;
    detailEl.innerHTML = renderDetail(active, state.completing);
    pushHash();
  }

  // ── board card ──────────────────────────────────────────────────────

  let boardPicker = null;

  function renderBoard() {
    const board = state.board || { posted: false, channel_id: "0" };
    const locked = !state.canManageBoard;
    // Keep a half-made selection across re-renders — refresh() also runs when
    // a task is added or completed, and resetting the picker under the admin
    // then reports "Pick a channel first" on a channel they had just chosen.
    const selected = boardPicker?.getValue?.() ?? String(board.channel_id || "0");
    const where = board.posted && board.jump_url
      ? `Posted — <a href="${esc(board.jump_url)}" target="_blank" rel="noopener noreferrer"
           style="color:var(--accent,#5af)">jump to the board ↗</a>`
      : "Not posted yet.";
    boardBody.innerHTML = `
      <div class="field">
        <label>Board Channel</label>
        <span data-picker="board-channel"></span>
      </div>
      <div class="td-actions">
        <button class="btn btn-primary" data-act="board-save" ${locked ? "disabled" : ""}>
          ${board.posted ? "Move / Repost Board" : "Post Board"}
        </button>
        ${board.posted
          ? `<button class="act-btn" data-act="board-remove" ${locked ? "disabled" : ""}>Remove Board</button>`
          : ""}
        <span data-board-status style="font-size:12px"></span>
      </div>
      <div class="field-hint" style="margin-top:6px">
        ${locked ? "Only administrators can post or move the board." : where}
      </div>`;

    boardPicker = mountChannelPicker(
      boardBody.querySelector('[data-picker="board-channel"]'),
      state.channels,
      selected,
      { label: "Board Channel" }
    );
    if (locked) {
      boardBody.querySelectorAll("input,select,button").forEach((el) => { el.disabled = true; });
    }
  }

  boardBody.addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-act]");
    if (!btn || btn.disabled || state.busy) return;
    const status = boardBody.querySelector("[data-board-status]");
    const remove = btn.dataset.act === "board-remove";

    if (remove) {
      const ok = await confirmDialog(
        "The board message will be deleted from its channel. Tasks themselves are untouched.",
        { danger: true, title: "Remove the board?", confirmLabel: "Remove" }
      );
      if (!ok) return;
    }
    const channelId = remove ? "0" : (boardPicker?.getValue?.() || "0");
    if (!remove && channelId === "0") {
      showStatus(status, false, "Pick a channel first.");
      return;
    }
    state.busy = true;
    btn.disabled = true;
    try {
      await apiPut("/api/todos/board", { channel_id: channelId });
      toast(remove ? "Board removed." : "Board posted.", "success");
      await refresh();
    } catch (err) {
      showStatus(status, false, err.message);
    } finally {
      state.busy = false;
      // The success path re-renders this button away; the error path doesn't,
      // so re-enable or a failed post leaves it dead until a page reload.
      btn.disabled = false;
    }
  });

  // ── recurring card ──────────────────────────────────────────────────

  function renderRecurring() {
    const editing = state.editingRecurringId
      ? state.recurring.find((r) => r.id === state.editingRecurringId) || null
      : null;
    recurringBody.innerHTML = recurringTable(state.recurring) + recurringEditor(editing);
    wireRecurringForm();
  }

  function readRecurringForm(form) {
    const recurrence = form.querySelector('[data-rec="recurrence"]').value;
    return {
      task: form.querySelector('[data-rec="task"]').value.trim(),
      description: form.querySelector('[data-rec="description"]').value.trim() || null,
      recurrence,
      time_of_day: timeToMinutes(form.querySelector('[data-rec="time"]').value),
      recur_days: recurrence === "weekly"
        ? [...form.querySelectorAll("[data-rec-day]")]
            .filter((cb) => cb.checked)
            .map((cb) => Number(cb.dataset.recDay))
        : [],
    };
  }

  function wireRecurringForm() {
    const form = recurringBody.querySelector("[data-recurring-form]");
    if (!form) return;
    const status = form.querySelector("[data-rec-status]");

    // Weekday pickers only mean something for a weekly cadence.
    form.querySelector('[data-rec="recurrence"]').addEventListener("change", (e) => {
      form.querySelector("[data-rec-days]").style.display =
        e.target.value === "weekly" ? "" : "none";
    });

    // Unsaved-edits guard, same as every other config form — a half-filled
    // recurring task shouldn't vanish on a sidebar click.
    guardForm(form);

    form.querySelector("[data-rec-cancel]")?.addEventListener("click", () => {
      state.editingRecurringId = null;
      renderRecurring();
    });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (state.busy) return;
      const body = readRecurringForm(form);
      if (!body.task) {
        showStatus(status, false, "Give the task a name.");
        return;
      }
      if (body.recurrence === "weekly" && !body.recur_days.length) {
        showStatus(status, false, "Pick at least one day of the week.");
        return;
      }
      state.busy = true;
      form.querySelector("[data-rec-save]").disabled = true;
      try {
        if (state.editingRecurringId) {
          await apiPut(`/api/todos/recurring/${state.editingRecurringId}`, body);
        } else {
          await apiPost("/api/todos/recurring", body);
        }
        state.editingRecurringId = null;
        await refreshRecurring();
      } catch (err) {
        showStatus(status, false, err.message);
        form.querySelector("[data-rec-save]").disabled = false;
      } finally {
        state.busy = false;
      }
    });
  }

  recurringBody.addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-act]");
    if (!btn || state.busy) return;
    const row = btn.closest("tr[data-recurring-id]");
    if (!row) return;
    const id = Number(row.dataset.recurringId);
    const item = state.recurring.find((r) => r.id === id);
    const act = btn.dataset.act;

    if (act === "edit") {
      state.editingRecurringId = id;
      renderRecurring();
      recurringBody.querySelector('[data-rec="task"]')?.focus();
      return;
    }

    if (act === "delete") {
      const ok = await confirmDialog(
        `“${item?.task ?? "This task"}” will stop repeating. Any copy already on ` +
        "the list stays there — it's real outstanding work.",
        { danger: true, title: "Delete recurring task?", confirmLabel: "Delete" }
      );
      if (!ok) return;
    }

    state.busy = true;
    try {
      if (act === "delete") {
        await apiDelete(`/api/todos/recurring/${id}`);
      } else if (act === "toggle") {
        const action = item?.status === "paused" ? "resume" : "pause";
        await apiPost(`/api/todos/recurring/${id}/${action}`, {});
      } else if (act === "run-now") {
        const res = await apiPost(`/api/todos/recurring/${id}/run-now`, {});
        const already = res?.spawned === false;
        toast(
          already ? "Already on the list — nothing new added." : "Added to the list.",
          already ? "info" : "success"
        );
        await Promise.all([refresh(), refreshRecurring()]);
        return;
      }
      await refreshRecurring();
    } catch (err) {
      toast(`Couldn't do that — ${err.message}`, "error");
    } finally {
      state.busy = false;
    }
  });

  async function refreshRecurring() {
    try {
      const data = await api("/api/todos/recurring");
      state.recurring = data.items || [];
      renderRecurring();
    } catch (err) {
      recurringBody.innerHTML = renderError(
        `Couldn't load recurring tasks — ${err.message}. Reload the page to try again.`
      );
    }
  }

  function renderStats() {
    const pending = state.todos.filter((t) => !t.completed_at).length;
    const completed = state.todos.length - pending;
    statsEl.innerHTML = `
      <div class="mod-stat open"><div class="lbl">Pending</div><div class="v">${pending}</div></div>
      <div class="mod-stat resolved"><div class="lbl">Completed</div><div class="v">${completed}</div></div>`;
  }

  async function refresh() {
    try {
      const data = await api("/api/todos");
      state.todos = data.todos || [];
      state.board = data.board || null;
      state.canManageBoard = !!data.can_manage_board;
      renderStats();
      render();
      renderBoard();
    } catch (err) {
      listEl.innerHTML = renderError(`Couldn't load the todo list — ${err.message}. Reload the page to try again.`);
      detailEl.innerHTML = "";
    }
  }

  addForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const task = addInput.value.trim();
    if (!task) return;
    addBtn.disabled = true;
    addStatus.textContent = "";
    try {
      await apiPost("/api/todos", { task });
      addInput.value = "";
      state.filter = "pending";
      filterGroup.querySelectorAll("button").forEach((b) => {
        b.classList.toggle("active", b.dataset.filter === "pending");
      });
      await refresh();
    } catch (err) {
      showStatus(addStatus, false, `Couldn't add that task — ${err.message}`);
    } finally {
      addBtn.disabled = false;
    }
  });

  makeFilterStrip(filterGroup, (value) => {
    state.filter = value;
    state.activeId = null;
    render();
  });

  listEl.addEventListener("click", (e) => {
    const row = e.target.closest(".ticket-item");
    if (!row) return;
    state.activeId = Number(row.dataset.todoId);
    render();
  });

  detailEl.addEventListener("click", async (e) => {
    const btn = e.target.closest(".act-btn[data-action='complete']");
    if (!btn || btn.disabled || state.completing || !state.activeId) return;
    state.completing = true;
    render();
    try {
      await apiPost(`/api/todos/${state.activeId}/complete`, {});
      await refresh();
    } catch (err) {
      state.completing = false;
      render();
      detailEl.insertAdjacentHTML(
        "beforeend",
        renderError(`Couldn't mark that task complete — ${err.message}. Try again.`)
      );
    } finally {
      state.completing = false;
    }
  });

  (async () => {
    // Channels first: the board card can't render its picker without them, and
    // a channel-load failure shouldn't block the task list from appearing.
    try {
      // Text channels only. /api/meta/channels also returns threads, but the
      // board lives by delete-and-repost and a thread can archive out from
      // under it — and guild.get_channel() doesn't resolve threads anyway, so
      // offering one would just 400 with "That channel doesn't exist here."
      state.channels = (await loadChannels()).filter((c) => c.type === "text");
    } catch {
      state.channels = [];
    }
    await Promise.all([refresh(), refreshRecurring()]);
  })();

  return { unmount() {} };
}
