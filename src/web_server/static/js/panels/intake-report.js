import { api, esc, fmtAge } from "../api.js";
import { rangePicker, withLoading } from "../report-helpers.js";
import { renderSortableTable } from "../table.js";

export function mount(container, initialParams) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Intake Queue</h2>
        <div class="subtitle">Open intake cards and how the welcome procedure is actually going</div>
      </header>
      <div class="controls"></div>
      <div data-stats class="subtitle" style="margin-bottom:8px;"></div>
      <div data-open></div>
      <h3 style="margin-top:16px;">Welcomers</h3>
      <div data-welcomers></div>
      <h3 style="margin-top:16px;">Skipped steps</h3>
      <div class="subtitle">Steps marked skipped when the completion code closed a card — the procedure’s own feedback about what the team doesn’t run.</div>
      <div data-skipped></div>
    </div>
  `;

  const rangeEl = rangePicker({ value: initialParams.days || 30, label: "Range" });
  container.querySelector(".controls").appendChild(rangeEl);
  const daysEl = rangeEl.querySelector("select");
  const statsEl = container.querySelector("[data-stats]");
  const openWrap = container.querySelector("[data-open]");
  const welcomersWrap = container.querySelector("[data-welcomers]");
  const skippedWrap = container.querySelector("[data-skipped]");

  const bar = (done, total) => {
    const width = 10;
    const filled = total ? Math.round((done / total) * width) : 0;
    return `${"▰".repeat(filled)}${"▱".repeat(width - filled)} ${done}/${total}`;
  };

  async function refresh() {
    const days = parseInt(daysEl.value) || 30;
    history.replaceState(null, "", `#/intake-report?days=${days}`);
    try {
      const data = await withLoading(openWrap, api("/api/reports/intake-report", { days }));

      if (!data.enabled) {
        statsEl.textContent = "Intake cards are disabled — enable them under Config → Intake Cards.";
      } else {
        const c = data.counts || {};
        const parts = [
          `${data.open_cards.length} open now`,
          `${c.completed || 0} completed`,
          `${c.dismissed || 0} dismissed`,
          `${(c.left || 0) + (c.banned || 0)} left/banned`,
        ];
        if (c.completed) {
          parts.push(`median time to complete: ${fmtAge(data.median_seconds)}`);
          parts.push(`mean: ${fmtAge(data.mean_seconds)}`);
        }
        statsEl.textContent = `${data.window_label}  ·  ${parts.join("  ·  ")}`;
      }

      if (!data.open_cards.length) {
        openWrap.innerHTML = `<div class="empty">No open intake cards — the queue is clear. 🎉</div>`;
      } else {
        renderSortableTable(openWrap, {
          columns: [
            { key: "user_name", label: "Newcomer", format: (v, r) => esc(v || r.user_id) },
            { key: "created_at", label: "Waiting", format: (v) => fmtAge(Date.now() / 1000 - v) },
            { key: "done", label: "Progress", format: (v, r) => bar(r.done, r.total) },
            { key: "pending", label: "Still to do", format: (v) => esc((v || []).join(", ")) || "—" },
            { key: "nudged", label: "Nudged", format: (v) => (v ? "yes" : "") },
          ],
          data: data.open_cards,
          defaultSort: "created_at",
          defaultAsc: true,
        });
      }

      welcomersWrap.innerHTML = "";
      if (data.welcomers.length) {
        renderSortableTable(welcomersWrap, {
          columns: [
            { key: "user_name", label: "Welcomer", format: (v, r) => esc(v || r.user_id) },
            { key: "completions", label: "Intakes completed" },
            { key: "ticks", label: "Steps ticked" },
          ],
          data: data.welcomers,
          defaultSort: "completions",
        });
      } else {
        welcomersWrap.innerHTML = `<div class="empty">No completed intakes in this window yet.</div>`;
      }

      skippedWrap.innerHTML = "";
      const skipped = (data.skipped_steps || []).filter((s) => s.appeared > 0);
      if (skipped.length) {
        renderSortableTable(skippedWrap, {
          columns: [
            { key: "label", label: "Step", format: (v) => esc(v) },
            { key: "appeared", label: "On completed cards" },
            { key: "skipped", label: "Skipped" },
            {
              key: "key", label: "Skip rate",
              format: (v, r) => `${Math.round((r.skipped / r.appeared) * 100)}%`,
            },
          ],
          data: skipped,
          defaultSort: "label",
        });
      } else {
        skippedWrap.innerHTML = `<div class="empty">Nothing to show yet.</div>`;
      }
    } catch (err) {
      openWrap.innerHTML = `<div class="empty">Failed to load: ${esc(err.message)}</div>`;
    }
  }

  daysEl.addEventListener("change", refresh);
  refresh();
}
