import { api, apiPost, esc } from "../api.js";

function fmtTs(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return (
    d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) +
    " " +
    d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
  );
}

function fmtAge(ts) {
  const s = Math.round(Date.now() / 1000 - ts);
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m";
  if (s < 86400) return Math.floor(s / 3600) + "h";
  return Math.floor(s / 86400) + "d";
}

function renderList(todos, activeId) {
  if (!todos.length) {
    return '<div class="empty">No todos match this filter.</div>';
  }
  return todos
    .map((t) => {
      const cls = (t.completed_at ? "low" : "med") + (t.id === activeId ? " active" : "");
      const age = fmtAge(t.created_at) + " ago";
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
    return '<div class="empty">Select a todo to view details.</div>';
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
  return `
    <div class="td-head">
      <div class="td-crumb">#${esc(t.id)} &nbsp;&middot;&nbsp; added ${esc(fmtAge(t.created_at))} ago</div>
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
      ${completedLine}
    </div>
    ${completeBtn}`;
}

const FILTERS = {
  pending:   (t) => !t.completed_at,
  completed: (t) => !!t.completed_at,
  all:       () => true,
};

export function mount(container) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Todo List</h2>
        <div class="subtitle">Server-wide tasks added via <code>/todo</code>.</div>
      </header>

      <form class="todo-add" data-add-form style="display:flex;gap:8px;margin-bottom:12px;align-items:flex-start;">
        <input type="text" data-add-input maxlength="500" placeholder="Add a new task…"
               style="flex:1;padding:8px 10px;border:1px solid var(--rule);border-radius:4px;background:var(--bg-rail);color:var(--ink);font-size:13px;font-family:inherit;" />
        <button type="submit" class="act-btn" data-add-btn>Add</button>
        <span data-add-status style="font-size:12px;align-self:center;"></span>
      </form>

      <div class="mod-stats" data-stats>
        <div class="mod-stat open"><div class="lbl">Pending</div><div class="v">—</div></div>
        <div class="mod-stat resolved"><div class="lbl">Completed</div><div class="v">—</div></div>
      </div>

      <section class="mod-split">
        <div class="ticket-list-wrap">
          <div class="ticket-list-head">
            <h3>Tasks</h3>
            <div class="ctrl-group" role="tablist" data-filter-group>
              <button class="active" data-filter="pending">Pending</button>
              <button data-filter="completed">Completed</button>
              <button data-filter="all">All</button>
            </div>
          </div>
          <div class="ticket-list" data-list>
            <div class="empty">Loading…</div>
          </div>
        </div>
        <div class="ticket-detail" data-detail>
          <div class="empty">Loading…</div>
        </div>
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

  const state = { todos: [], filter: "pending", activeId: null, completing: false };

  function render() {
    const filtered = state.todos.filter(FILTERS[state.filter]);
    if (!filtered.find((t) => t.id === state.activeId)) {
      state.activeId = filtered[0]?.id ?? null;
    }
    listEl.innerHTML = renderList(filtered, state.activeId);
    const active = state.todos.find((t) => t.id === state.activeId) || null;
    detailEl.innerHTML = renderDetail(active, state.completing);
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
      renderStats();
      render();
    } catch (err) {
      listEl.innerHTML = `<div class="error" style="padding:20px">${esc(err.message)}</div>`;
      detailEl.innerHTML = "";
    }
  }

  addForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const task = addInput.value.trim();
    if (!task) return;
    addBtn.disabled = true;
    addStatus.textContent = "";
    addStatus.style.color = "";
    try {
      await apiPost("/api/todos", { task });
      addInput.value = "";
      state.filter = "pending";
      filterGroup.querySelectorAll("button").forEach((b) => {
        b.classList.toggle("active", b.dataset.filter === "pending");
      });
      await refresh();
    } catch (err) {
      addStatus.textContent = err.message;
      addStatus.style.color = "var(--red, #e55)";
    } finally {
      addBtn.disabled = false;
    }
  });

  filterGroup.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-filter]");
    if (!btn) return;
    filterGroup.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b === btn));
    state.filter = btn.dataset.filter;
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
        `<div class="error" style="padding:8px 16px;color:var(--red)">${esc(err.message)}</div>`
      );
    } finally {
      state.completing = false;
    }
  });

  refresh();

  return { unmount() {} };
}
