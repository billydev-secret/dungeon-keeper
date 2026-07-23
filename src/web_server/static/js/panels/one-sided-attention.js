import { api, esc } from "../api.js";
import { rangePicker, withLoading } from "../report-helpers.js";

// Moderator-review report of lopsided attention between member pairs.
// Deliberately NOT a score or a verdict: it surfaces the *shape* of one-sided
// attention with the underlying evidence, for a human to glance at and judge.

export function mount(container, initialParams) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>One-Sided Attention</h2>
        <div class="subtitle">Members receiving persistent, unreturned attention — for your review, not a verdict</div>
      </header>
      <div class="osa-note" style="border:1px solid var(--gold-solid,#E6B84C);background:var(--gold-soft,rgba(230,184,76,0.12));border-radius:8px;padding:10px 12px;margin:4px 0 12px;font-size:12.5px;line-height:1.5;color:var(--ink,#dbdee1);">
        <strong>Read this as a tip, not a conclusion.</strong> These pairs show a lopsided
        <em>shape</em> of interaction — one person directing far more attention than they get
        back. That shape also fits new friendships, fandom energy, and crushes that just
        haven't been reciprocated <em>yet</em>. It is a prompt to look, quietly and with context —
        never grounds to message, warn, or act against anyone automatically. Nothing here is
        shown to the members involved. Attention is measured regardless of anyone's gender.
      </div>
      <div class="controls"></div>
      <div data-summary class="subtitle" style="margin-bottom:10px;"></div>
      <div data-list></div>
    </div>
  `;

  const rangeEl = rangePicker({ value: initialParams.window_days || 30, allowAll: false, label: "Window (days)" });
  container.querySelector(".controls").appendChild(rangeEl);
  const daysEl = rangeEl.querySelector("select");
  const summaryEl = container.querySelector("[data-summary]");
  const listEl = container.querySelector("[data-list]");

  function who(id, name) {
    return esc(name || `User ${id}`);
  }

  function card(c) {
    const chips = (c.reasons || [])
      .map((r) => `<span class="chip chip-warning">${esc(r)}</span>`)
      .join(" ");
    const cautions = (c.cautions || [])
      .map((r) => `<span class="chip chip-neutral">${esc(r)}</span>`)
      .join(" ");
    const back = c.weight_back > 0
      ? `${c.weight_back} back`
      : `<strong>nothing back</strong>`;
    const parts = [];
    if (c.text_out) parts.push(`${c.text_out} replies/mentions`);
    if (c.react_out) parts.push(`${c.react_out} reactions`);
    if (c.voice_follow_out) parts.push(`${c.voice_follow_out} voice-follows`);
    const breakdown = parts.length ? parts.join(" · ") : "—";

    return `
      <div style="border:1px solid var(--rule,#3a3d44);border-radius:8px;padding:12px 14px;margin-bottom:10px;background:var(--surface-2,#2b2d31);">
        <div style="display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap;">
          <div style="font-size:14px;">
            <strong>${who(c.from_id, c.from_name)}</strong>
            <span style="color:var(--ink-dim,#949ba4);"> → </span>
            <strong>${who(c.to_id, c.to_name)}</strong>
          </div>
          <div style="font-size:12px;color:var(--ink-dim,#949ba4);">
            ${c.weight_out} directed · ${back}
          </div>
        </div>
        <div style="font-size:12px;color:var(--ink-dim,#949ba4);margin:4px 0 8px;">${breakdown}</div>
        <div style="display:flex;flex-wrap:wrap;gap:6px;">${chips}</div>
        ${cautions ? `<div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:6px;">${cautions}</div>` : ""}
      </div>
    `;
  }

  async function refresh() {
    const params = {};
    const d = parseInt(daysEl.value);
    if (!isNaN(d) && d > 0) params.window_days = d;

    const qs = new URLSearchParams();
    if (params.window_days) qs.set("window_days", params.window_days);
    history.replaceState(null, "", `#/one-sided-attention?${qs}`);

    try {
      const data = await withLoading(listEl, api("/api/reports/one-sided-attention", params));
      const n = data.candidates.length;
      summaryEl.textContent = n
        ? `${n} pair${n === 1 ? "" : "s"} worth a look over the last ${data.window_days} days.`
        : "";
      listEl.innerHTML = n
        ? data.candidates.map(card).join("")
        : `<div class="empty">No lopsided pairs cleared the threshold in this window. That's the expected result most of the time.</div>`;
    } catch (err) {
      summaryEl.textContent = "";
      listEl.innerHTML = `<div class="error">${esc(err.message)}</div>`;
    }
  }

  daysEl.addEventListener("change", refresh);
  refresh();

  return { unmount() {} };
}
