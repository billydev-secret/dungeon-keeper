# DM perms + no-contact + voice transcription — battery review, 2026-08-05

Three small bundles, one doc. All three: clean bills.

## DM perms (`dm_perms_cog` 1.3k, `dm_perms/` logic+embeds, service)

- Consent-pair model with full audit trail; revoked pairs hard-deleted from
  the live map while the audit log retains the transition history — the
  forensic-preserve decision is already documented (privacy_spec:111,
  dm_perms_spec:117-123). Register: **DECIDED**.
- In-memory consent set rebuilt on restart (spec'd); expiry task documented.
- No architecture/UX/docs findings. mod-dm-audit panel is mod-gated
  (authz-sweep covered).

## No-contact (`no_contact_{cog,logic,service}`, 1 live pair)

- The spec's reasoning is the best UX writing in `docs/` — it argues the
  member-self-service Discord exception, asymmetric add/remove, and the
  no-notification-to-protected-member property in terms of the abuse
  dynamics. Enforcement integration with Whisper (identical-confirmation,
  no tells) verified in the whisper review.
- Alerts carry jump link + channel + timestamp, **no message text** —
  deliberately refuses to widen content retention for one feature.
- G1 (documentation-only): `no_contact_pairs`/`no_contact_events` are
  protective records — preserve through erasure is the right call (an
  erasure request from the *restricted* party must not dissolve the
  order). Add one preserve line to privacy_spec's implicit list.
  Register: **DECIDED — deliberate preserve**.

## Voice transcription (`voice_transcription_{cog,service}`, 261 lines)

- Local CPU faster-whisper, `local_files_only=True` (no runtime downloads),
  off by default, dashboard-only config, **transcripts never stored** —
  the reply message is the only output. `voice_transcription_config` holds
  no personal data. Register: **no personal data at rest**; processor
  local. The ProtectHome/HF_HOME dance is spec-documented.
- No findings.

## Verdict

Zero findings requiring code change beyond one privacy_spec preserve line
(no-contact G1). Wave 1 is now 9 of 11 bundles done; remaining: Health/
analytics suite, Pen Pals, Bios.
