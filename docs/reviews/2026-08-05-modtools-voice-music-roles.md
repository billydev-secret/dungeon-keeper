# Jail/tickets/mod + Voice Master + music + roles — review, 2026-08-05

W4 batch 1. Specs: dungeon_keeper_jail_ticket_spec, voice_master_spec
(Reference since 07-15), music_spec, auto_role_spec.

## Jail / tickets / mod

- Sanction tables (`jails`, `warnings`, `tickets`, `policy_tickets`) store
  mod-authored reasons/descriptions + ids — **no transcripts** (ticket
  conversations live in Discord channels; `source_message_url` is a
  pointer). `jails.stored_roles` enables role restoration; warnings have a
  full revocation lifecycle. Register: **DECIDED — preserve** (sanction
  history is the canonical moderation record; privacy_spec already names
  jail as deliberate).
- Known gap already tracked: member-leaves-while-jailed handling lives on
  the unmerged `jail-member-left` branch (loose-ends §7). No new findings
  duplicated here.

## Voice Master

- The access dial CLAUDE.md cites as the collapse-controls exemplar.
  `voice_master_profiles` is member self-service prefs (name/limit/lock/
  bitrate/age_gated) + `voice_master_trusted` allow-lists. Register:
  purge-able — add both to the purge batch (prefs have no audit role).
  No other findings.

## Music

- **Processors**: Lavalink defaults to 127.0.0.1 (local audio server,
  password-gated) ✓; **Spotify Web API (cloud)** via client-credentials —
  outbound data is track/playlist *queries* only (member-typed song
  names), no member identity attached. Register processor inventory
  updated: second cloud processor, trivially scoped. Spotify OAuth route
  exists for user-linked flows — the security sweep (W4.5) should confirm
  token storage location and scope.
- No storage of listening history found (`music_channel_settings` is
  config). ✓

## Roles

- `role_events` purged ✓; `role_grant_audit_service` + grant-audit panel
  are mod-action records → preserve with the sanction family.
  `role_menus`/`booster_roles`/`auto_role`: config + self-service, no
  personal data beyond grants already audited. No findings.

## Verdict

Register decisions all round; one W4.5 handoff (Spotify token storage).
No code findings. Fixtures batch next, then the horizontal sweeps.
