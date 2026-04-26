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

function row(label, value) {
  const div = document.createElement("div");
  div.className = "field";
  const lbl = document.createElement("label");
  lbl.textContent = label;
  div.appendChild(lbl);
  const val = document.createElement("div");
  if (Array.isArray(value)) {
    if (value.length === 0) {
      val.textContent = "(empty)";
    } else {
      val.textContent = value.join(", ");
    }
  } else if (value === null || value === undefined) {
    val.textContent = "(unset)";
  } else if (typeof value === "boolean") {
    val.textContent = value ? "yes" : "no";
  } else {
    val.textContent = String(value);
  }
  div.appendChild(val);
  return div;
}

async function loadAndRender(container, panel, output, userId) {
  clearChildren(output);
  let data;
  try {
    data = await apiGet(`/api/voice-master/profiles/${userId}`);
  } catch (err) {
    output.textContent = "Failed to load: " + err.message;
    return;
  }
  const sub = document.createElement("div");
  sub.className = "subtitle";
  sub.textContent = `Profile for user ${data.user_id}`;
  output.appendChild(sub);

  if (data.profile === null) {
    const e = document.createElement("div");
    e.className = "empty";
    e.textContent = "No saved profile.";
    output.appendChild(e);
  } else {
    output.appendChild(row("Saved name", data.profile.saved_name));
    output.appendChild(row("Saved limit", data.profile.saved_limit || "no cap"));
    output.appendChild(row("Locked", !!data.profile.locked));
    output.appendChild(row("Hidden", !!data.profile.hidden));
    output.appendChild(row("Bitrate", data.profile.bitrate || "guild default"));
  }
  output.appendChild(row(`Trusted (${data.trusted.length})`, data.trusted));
  output.appendChild(row(`Blocked (${data.blocked.length})`, data.blocked));

  const clearBtn = document.createElement("button");
  clearBtn.className = "btn btn-danger";
  clearBtn.textContent = "Force-clear profile";
  clearBtn.addEventListener("click", async () => {
    if (!confirm(`Clear saved profile for user ${userId}? This is logged.`)) return;
    try {
      await apiPost(`/api/voice-master/profiles/${userId}/clear`);
      alert("Cleared.");
      await loadAndRender(container, panel, output, userId);
    } catch (err) {
      alert("Clear failed: " + err.message);
    }
  });
  output.appendChild(clearBtn);
}

export function mount(container) {
  clearChildren(container);
  const panel = document.createElement("div");
  panel.className = "panel";
  container.appendChild(panel);

  const h2 = document.createElement("h2");
  h2.textContent = "Voice Master · Profiles";
  panel.appendChild(h2);
  const sub = document.createElement("div");
  sub.className = "subtitle";
  sub.textContent = "Inspect any member's saved profile. Every view is audit-logged.";
  panel.appendChild(sub);

  const form = document.createElement("form");
  form.className = "form";
  panel.appendChild(form);
  const input = document.createElement("input");
  input.type = "text";
  input.name = "user_id";
  input.placeholder = "user_id (numeric)";
  form.appendChild(input);
  const btn = document.createElement("button");
  btn.type = "submit";
  btn.className = "btn btn-primary";
  btn.textContent = "Inspect";
  form.appendChild(btn);

  const output = document.createElement("div");
  panel.appendChild(output);

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const userId = input.value.trim();
    if (!/^\d+$/.test(userId)) {
      output.textContent = "Enter a numeric user ID.";
      return;
    }
    loadAndRender(container, panel, output, userId);
  });
}
