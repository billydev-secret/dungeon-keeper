# W4.5 sweep: security + dependencies — 2026-08-05/06

Whole-dashboard + outbound-surface pass. (`/security-review` skill targets
branch diffs, so this was a manual targeted sweep.)

## Authorization

- Route-by-route guard census across all 34 route files: every file with
  routes carries auth dependencies; the two apparent gaps resolved clean —
  `oauth.py` is the login flow itself (public by design, PUBLIC_PATHS) and
  `no_contact.py` binds a module-level `_MOD = Depends(require_perms({"moderator"}))`
  applied on all four routes. Wellness member/admin split verified in its
  bundle (session-derived identity; party-checked deletes).
- Privilege tiers spot-checked: gender (read=moderator, write=admin),
  whisper audit (admin), economy manager (admin-or-manager-role),
  no-contact (moderator). No inversion found.

## Injection

- Repo-wide f-string-SQL scan: every dynamic fragment is an internal
  placeholder list / fixed column set; the single ALTER-with-f-string is
  an internal migration helper (branding). **No user-tainted SQL.**

## Outbound / SSRF

- Fetchers inventoried: Discord/Spotify OAuth (fixed endpoints), Ollama
  (localhost + local-suffix allowlist), emoji stealer (constructed CDN
  URLs only), Lavalink (127.0.0.1). The one caller-supplied-URL fetch —
  avatar download (`config.py:_download_avatar_bytes`) — has **exemplary
  SSRF defense**: manual redirect following with per-hop private/multicast
  address rejection and an 8 MB stream cap. No SSRF findings.

## Sessions / secrets

- Cookies: `httponly`, `samesite=lax`, `secure` when HTTPS ✓. Spotify
  OAuth uses `secrets.token_urlsafe(32)` state (CSRF ✓); the refresh
  token persists **plaintext in the config KV table** — acceptable for a
  self-hosted single-box deployment (S1, informational: DB compromise
  also yields the token; scope is one Spotify app).
- No hardcoded secrets in the tree (scan clean; everything via env).

## Dependencies

- **D1 — `aiohttp 3.14.1` has 3 known vulns (PYSEC-2026-3545/-3546/-3547),
  fixed in 3.14.2/3.14.3.** aiohttp is discord.py's HTTP layer — this is
  the one actionable item. Fix: bump in requirements, recompile locks
  (`uv pip compile … --universal -p 3.14`), let CI prove it. Dependabot
  would have caught it on its weekly cycle; this beats it by a few days.
  **Priority: medium-high.**
- Python: everything else clean. npm (dashboard dev tooling): 0 vulns.
- Audit artifacts cleaned from the prod checkout (pip-audit uninstalled,
  transient package-lock removed).

## Verdict

One dependency action (D1 aiohttp bump), one informational (S1). The
dashboard's security posture is strong — the avatar SSRF defense and the
ollama host allowlist are worth citing as house patterns.
