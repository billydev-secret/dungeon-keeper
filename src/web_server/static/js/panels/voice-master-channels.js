async function apiGet(path) {
  const res = await fetch(path, { credentials: "same-origin" });
  if (!res.ok) throw new Error(`${res.status}: ${res.statusText}`);
  return res.json();
}

async function apiPost(path, body) {
  const res = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: body == null ? null : JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { const b = await res.json(); if (b.detail) detail = b.detail; } catch (_) {}
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json();
}

function clearChildren(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
}

function relTime(epoch) {
  const seconds = Math.floor(Date.now() / 1000) - epoch;
  if (seconds < 60) return seconds + "s ago";
  if (seconds < 3600) return Math.floor(seconds / 60) + "m ago";
  if (seconds < 86400) return Math.floor(seconds / 3600) + "h ago";
  return Math.floor(seconds / 86400) + "d ago";
}

function makeHeaderRow(labels) {
  const tr = document.createElement("tr");
  for (const label of labels) {
    const th = document.createElement("th");
    th.textContent = label;
    tr.appendChild(th);
  }
  return tr;
}

async function render(container) {
  clearChildren(container);
  const panel = document.createElement("div");
  panel.className = "panel";
  container.appendChild(panel);

  const h2 = document.createElement("h2");
  h2.textContent = "Voice Master · Active channels";
  panel.appendChild(h2);

  const refresh = document.createElement("button");
  refresh.className = "btn";
  refresh.textContent = "Refresh";
  refresh.addEventListener("click", () => render(container));
  panel.appendChild(refresh);

  let data;
  try {
    data = await apiGet("/api/voice-master/channels");
  } catch (err) {
    const e = document.createElement("div");
    e.className = "empty";
    e.textContent = "Failed to load: " + err.message;
    panel.appendChild(e);
    return;
  }

  const channels = data.channels || [];
  if (channels.length === 0) {
    const e = document.createElement("div");
    e.className = "empty";
    e.textContent = "No active member-owned voice channels.";
    panel.appendChild(e);
    return;
  }

  const tbl = document.createElement("table");
  tbl.className = "data-table";
  tbl.style.width = "100%";
  tbl.style.marginTop = "12px";
  panel.appendChild(tbl);

  const thead = document.createElement("thead");
  thead.appendChild(makeHeaderRow(
    ["Channel", "Owner", "Members", "Owner present", "Created", "Actions"]
  ));
  tbl.appendChild(thead);

  const tbody = document.createElement("tbody");
  tbl.appendChild(tbody);

  for (const ch of channels) {
    const tr = document.createElement("tr");

    const tdName = document.createElement("td");
    tdName.textContent = ch.channel_name + " (" + ch.channel_id + ")";
    tr.appendChild(tdName);

    const tdOwner = document.createElement("td");
    tdOwner.textContent = (ch.owner_name || "(unknown)") + " (" + ch.owner_id + ")";
    tr.appendChild(tdOwner);

    const tdCount = document.createElement("td");
    tdCount.textContent = String(ch.members_count);
    tr.appendChild(tdCount);

    const tdPresent = document.createElement("td");
    tdPresent.textContent = ch.owner_in_channel
      ? "yes"
      : (ch.owner_left_at ? `no (left ${relTime(ch.owner_left_at)})` : "no");
    tr.appendChild(tdPresent);

    const tdCreated = document.createElement("td");
    tdCreated.textContent = relTime(ch.created_at);
    tr.appendChild(tdCreated);

    const tdActions = document.createElement("td");

    const delBtn = document.createElement("button");
    delBtn.className = "btn btn-danger btn-sm";
    delBtn.textContent = "Force-delete";
    delBtn.addEventListener("click", async () => {
      if (!confirm(`Delete ${ch.channel_name}?`)) return;
      try {
        await apiPost(`/api/voice-master/channels/${ch.channel_id}/force-delete`);
        await render(container);
      } catch (err) {
        alert("Delete failed: " + err.message);
      }
    });
    tdActions.appendChild(delBtn);

    const transferBtn = document.createElement("button");
    transferBtn.className = "btn btn-sm";
    transferBtn.textContent = "Force-transfer";
    transferBtn.addEventListener("click", async () => {
      const newOwner = prompt("New owner user_id:");
      if (!newOwner) return;
      try {
        await apiPost(`/api/voice-master/channels/${ch.channel_id}/force-transfer`, {
          new_owner_id: parseInt(newOwner, 10),
        });
        await render(container);
      } catch (err) {
        alert("Transfer failed: " + err.message);
      }
    });
    tdActions.appendChild(transferBtn);

    tr.appendChild(tdActions);
    tbody.appendChild(tr);
  }
}

export function mount(container) {
  render(container);
}
