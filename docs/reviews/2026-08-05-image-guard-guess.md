# Image Guard / NSFW classifier + Guess — battery review, 2026-08-05

Bundle: `nsfw_classifier_service.py` (847), `marqo_nsfw.py`, `guess_cog.py`
(2,054), `guess_{pipeline,repo,models,nudenet,face_detector,pose_detector,
crop_renderer}.py`, panels `config-guess`, `guess-audit`, `nsfw-*` (3),
`config-spoiler`. Specs: `nsfw_classifier_spec.md`, `guess_spec.md`.
Prod: 355 rounds (24 open, 4 confessions), guess_cache 5 MB / 4 originals.

## Architecture

- **Image Guard side: no findings.** The classifier service was rebuilt last
  week (Marqo swap) and the spec documents every load-bearing decision with
  its test (DoS-safe preprocessing order, three-valued verdict, task-holding
  LRU, per-consumer failure direction). The test lists in spec §Tests match
  real files. Re-reviewing freshly-reviewed code is where this battery adds
  nothing — verified claims spot-check clean, moving on.
- **A1 (guess)** — dead reuse columns: `allow_reuse`, `is_reuse`,
  `original_round_id`, `reuse_blocked` persist in `guess_rounds` though the
  reuse feature was removed (spec non-goals). Harmless; fold into a future
  migration, don't burn one on it. — **done, migration 184** (folded in with
  the `games_consent` drop).
- **A2 (guess)** — stale `original_path` rows: pre-rename rows still point
  at `veil_cache/orig/*.jpg` which no longer exists. Solve-time deletion
  no-ops on missing files (fine), but any future "purge originals" tooling
  should treat path-missing as already-clean. Note only.
- Tests: classifier suite is exemplary; guess has pipeline/repo coverage.
  No new-test demands.

## UX

- **U1 — Guess consent is far below the Whisper bar, for the more intimate
  feature.** `/guess optin` grants the role instantly — no confirmation
  view, no disclosure that submissions (including NSFW self-images) are
  cached on disk until solve, that confession text is stored beside the
  submitter's id, or that there's **no self-service opt-out** (a mod must
  remove the role; there is no `/guess optout`). Whisper shows the house
  pattern: consent embed + Confirm button + `/whisper forget-me`.
  Recommendation: (a) add the consent view with retention disclosure,
  (b) add `/guess optout` (role removal + the existing `answer_optout`
  flagging already handles open rounds), (c) consider `/guess forget-me`
  (soft-delete own rounds + delete cached files). Spec:129 already admits
  the disclosure gap — this review just says: fix it, pattern exists.
  **Priority: high** (consent quality on the most sensitive user content
  in the bot).
- U2 — the 2026-07-27 removal of the age-gate on the guess channel is a
  recorded owner decision (moderator-policed placement) — noted, not
  contested. But CLAUDE.md's stated default is "NSFW gates on
  `channel.is_nsfw()`, never a bot-side toggle"; guess is now the one
  feature outside that rule. The synthesis should record it as a standing
  exception so future reviews stop re-flagging it.

## Docs

- Both specs current, honest, and INDEX-classified correctly. The NSFW spec
  is the best document in `docs/` — cite as the template in synthesis.
- D1: manual.html — verify the Guess section tells members the retention
  facts once U1 lands (same commit, per working agreement).

## GDPR

- **Image Guard register entries** (decisions already documented in spec —
  recording only in age-gated channels, no author_id on classification
  tables, admin-gated panels, indefinite retention deliberate):
  `nsfw_classifications`, `nsfw_detections` (**most sensitive table in the
  bot** — labelled body-part inventory, minimized by design),
  `nsfw_blocks` (has author_id, justified in spec:134). Register marks all
  three **DECIDED — deliberate preserve**, with one open question:
  indefinite retention of `nsfw_detections` should get a TTL review in 6
  months (tuning value decays; sensitivity doesn't).
- **G1 (guess)** — `guess_rounds.confession_text` + `submitter_id`:
  anonymous confessions deanonymizable by admins, retained indefinitely,
  disclosure absent (U1). Fold disclosure in; register: undecided → needs
  the U1 package.
- **G2 (guess)** — cached original images are member-submitted intimate
  content on plain disk (`guess_cache/orig/`), outside the DB and thus
  outside any DB-side purge or backup story. Small in prod (4 files) and
  short-lived by design, but unsolved rounds keep originals indefinitely
  (19 recorded, most already vanished with veil_cache). Recommendation:
  age-out originals for rounds open > 90 days (delete file, keep round —
  crop still posted on Discord).
- G3 — `nsfw_observe_age_gated=1` is ON in prod: every image in age-gated
  channels is now recorded. That's the documented opt-in doing its job, but
  it's also the "table grows to cover compliant posts" mode — pair it with
  the G-register's TTL question above. Both log channels still 0
  (loose-ends §2) — enforcement without audit trail.
- G4 — processors: all inference is local (ONNX in-process). No image
  bytes leave the host. ✓

## Verdict

Image Guard: clean bill, exemplary docs. Guess: one high-priority package —
bring consent/opt-out/disclosure up to the Whisper bar (U1+G1+G2); the
mechanics to do it all exist in-repo already.
