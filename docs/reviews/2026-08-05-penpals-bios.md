# Pen Pals + Bios — battery review, 2026-08-05 (closes Wave 1)

## Pen Pals (`pen_pals_cog.py` 2.1k, panels ×2, spec current)

- **No letter content is ever stored** — `pen_pals_sessions` is pairing
  metadata (ids, timing, close reason); conversation lives in the Discord
  channel only. Spec's Stored-data section matches schema exactly ✓.
- Pool rows deleted on leave; member and admin blocks deletable; blocks
  treated symmetrically in matching (spec:202) ✓.
- G1 (register): sessions retained indefinitely for the no-repeat check —
  legitimate matchmaking memory; mark **DECIDED — preserve** with that one
  sentence. `pen_pals_blocks` are protective (never-match) — preserve,
  same reasoning as no-contact.
- Loose-ends §7 note: `pen-pals-reply-reminder` branch still unmerged;
  current code healthy without it.
- No architecture/UX findings. (2k-line cog is view-heavy — wizard/panels —
  with db funneled through helpers; acceptable shape.)

## Bios (`bios/` 2.7k — db/logic/views/wizard/resurrect, spec current)

- Strong self-service lifecycle: wizard in a throwaway private channel,
  `delete_user_bio` = permanent user-facing delete of row + snapshots,
  edit-in-place, question snapshots per answer. Config all dashboard-side.
- **G2 — archive-on-leave retention**: `archive_user_bio` keeps the full
  snapshotted bio content of departed members indefinitely so a rejoin
  auto-resurrects it (welcome_service's `{member_bio_link}`). Nice UX;
  unbounded retention of self-disclosed content for people who *left*.
  Recommendation: purge archived bios after 12 months post-leave (they can
  rebuild via the wizard). Register: needs-decision, medium.
- G3 — `bios`/`bio_answers`/`bio_field_values` not in `purge_user_data`;
  self-service delete covers the living, but a legal-erasure run should
  sweep them too — add to the purge list alongside the whisper-style
  per-feature paths. Low effort, clear win.
- No architecture findings; spec's 254-char field-name cap note (bios spec:
  221) shows the level of care.

## Wave 1 verdict

All 11 bundles done. High-priority items across the wave:
1. privacy-core A1 (purge placeholder blowout) + U1 (disclosure vs
   content retention)
2. whisper A1 (cross-guild forget-me over-delete)
3. guess consent package (U1/G1/G2)
4. health-analytics G1 (mod-assigned gender — policy)
Plus ~a dozen register decisions and low-priority GCs. W2 (economy) next.
