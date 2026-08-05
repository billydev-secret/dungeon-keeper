# Health / analytics suite — battery review, 2026-08-05

Bundle: `health_{metrics,service}.py`, `channel_health(_logic).py`,
`activity_graphs.py`, `interaction_graph.py`, `graph_metrics.py`,
`member_quality_score.py`, `attention_report.py`, `gender_service.py`;
panels health-×9, quality-score, one-sided-attention, retention,
gender-admin, nsfw-gender, interaction-graph. Spec: `post_monitoring_spec.md`.

## Architecture / UX / Docs

- Derived-metrics layer over the ingest tables; no independent collection
  paths. Quality score is report-only ("members never see scores", mod-only)
  and drives **no automated action** — inactivity prune is plain
  days-inactive + exception list, unrelated to scores ✓.
- `attention_report.py`'s docstring is a design document: flag-not-verdict,
  evidence-over-score (COMPAS anchoring called out), explicitly
  gender-neutral, escalation-after-silence as the primary cue, never
  automated. **Cite in synthesis as the standard for analytics features.**
- No architecture findings. Panels admin/mod-gated (spot-checked gender
  routes: read=moderator, write=admin).

## GDPR

- **G1 — `member_gender` is mod-assigned, and this is the most legally
  exposed practice in the bot.** 377 members tagged (237 m / 119 f / 21 nb)
  by 3 moderators, for NSFW-posting-by-gender analytics. The subject is not
  involved: no self-declaration, no notice, no visibility that the tag
  exists. Gender (esp. a nonbinary tag) is sensitive-category-adjacent;
  a mod's guess can also simply be wrong, and 21 nonbinary tags assigned
  *about* people is exactly where a wrong guess is harmful.
  Mitigations already present: hard-erasure covers it, admin/mod-gated,
  never member-visible, gender-neutral design in attention_report.
  **Options for Ben** (pick one, document in spec either way):
  1. Derive from self-declared pronoun/identity roles where they exist,
     and only fall back to mod assignment with a documented basis;
  2. Keep mod assignment but add transparency (a line in the server's
     privacy/rules post that NSFW analytics use mod-recorded gender);
  3. Drop the dimension from analytics and delete the table.
  Doing none of these leaves undisclosed sensitive-attribute profiling.
  **Priority: high (policy, not code).**
- G2 — `quality_score_leaves` keeps departed members' final scores
  (retention analysis). Purged on erasure ✓; add a register preserve line.
- G3 — interaction graph / reaction_log / voice_follow_log power the
  attention report. `reaction_log` (183k rows) remains the largest
  unpurged table (register) — it's in the *evidence* path for attention
  reports, so a preserve decision is defensible; make it explicitly.
- G4 — retention/heatmap/DAU-MAU/sentiment panels are aggregate; no
  per-member exposure beyond the gated reports. ✓

## Verdict

One high policy finding (G1 — mod-assigned gender needs a basis/
transparency decision), two register decisions (G2, G3). Code itself is
in good shape and the attention report is a model of restraint.
