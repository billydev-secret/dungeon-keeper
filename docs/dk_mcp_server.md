# DK MCP Server — Feature Spec (Reference)

A **read-only** MCP server that exposes Dungeon Keeper's specs, plans,
conventions and source code to Claude, so feature specs can be developed in a
claude.ai chat against how the bot actually works rather than against a
plausible guess.

Source lives at `src/dk_mcp/`; it runs from `/opt/dk-mcp` as `dk-mcp.service`,
behind a Cloudflare Tunnel at `dkmcp.billy-bots.com`. It is a development tool,
not a bot feature: no Discord surface, no dashboard panel, no user-facing copy.

## Why it exists

`docs/` is 2.7 MB across 171 markdown files. A model asked to design a DK
feature without them invents a shape that does not fit — a slash command for
admin config, a renamed dashboard route id, a per-user table with no data
register row. The output of a session here is spec text that Billy pastes into
a `/dk-feature` session, so every change still lands through the normal gate
and commit path.

## The corpus

Served:

| Source | Count | Kind |
|---|---|---|
| `docs/*.md` | 76 | `reference` / `design` / `aspirational` / `index` |
| `docs/plans/*.md` | 55 | `plan` |
| `CLAUDE.md` | 1 | `agreement` |
| `src/web_server/static/manual.html` | 1 | `manual` |
| `src/**` | — | code search and read, not classified |

Deliberately **not** served:

- **`docs/reviews/`** (43 documents). Many findings are superseded, and they
  would dilute search results with things that are no longer true.
- **`docs/testing/`** (3 checklists). Completed QA work.
- Everything else in the checkout, including `.env`, `dungeonkeeper.db`,
  `.git/`, `lavalink/` and `README.md`.

`docs/INDEX.md` links to both excluded directories. Its **Audits** and
**Testing checklists** sections are stripped from the served copy, and
requesting one of those paths returns an explicit *"deliberately outside this
server's corpus"* message rather than a confusing not-found.

## Classification — the load-bearing part

`docs/INDEX.md` classifies every spec as **Reference** (matches current
behavior), **Design** (written to implement; may lag the code) or
**Aspirational** (never fully built), and CLAUDE.md's rule is that **when a
spec and the code disagree, the code wins**. A server that hands over
`duel_minigame_flows_v2.md` unlabelled will produce confident designs for
Liar's Dice and Minesweeper, neither of which exists.

So **every document returned carries a banner naming its classification**, and
the server instructions and tool descriptions repeat the code-wins rule.

Classification is parsed from INDEX.md's section tables (`corpus_service.py`).
Details that matter:

- **Only table rows count.** A link inside a Notes cell ("plan in
  `[plans/foo.md](...)`") is a cross-reference, not a filing. Counting those
  would silently mark ~21 unlisted plans as classified.
- **The Notes/Status column rides along verbatim.** It is often the real
  warning: `survey_spec.md` is filed as a *design* spec and its note says
  "**Zero code** — not started"; a plan's status may be "Proposal — nothing
  built" or "Not started, deliberately deferred".
- **A doc listed twice takes the more cautionary label.** INDEX says outright
  that a few plans also appear in the Design table. The label exists to stop a
  reader over-trusting the document, so under-warning is the failure that
  matters.
- **Plans with no INDEX row say so.** 21 of 55 have none; their banner states
  there is no recorded status and points at the plan's own dated header.
- **INDEX.md does not classify itself**, so it gets its own `index` kind rather
  than reading as drift.

The catalogue rebuilds when INDEX.md changes on disk (checked at most once a
minute). The checkout is production and features merge into it while a chat
session is open.

## Tool surface

| Tool | Purpose |
|---|---|
| `list_docs(kind, contains)` | The catalogue: every document with its classification, summary and status |
| `search_docs(query, kind, limit)` | Ranked search **per section**, so a hit names the heading breadcrumb and line to fetch next. Supports `"quoted phrases"` |
| `get_doc(path, section)` | A document or one section, with its banner. Long docs return an outline instead of their text |
| `get_conventions(topic)` | The house rules assembled from CLAUDE.md, `embed_style_guide.md`, `dashboard_ia.md`, `data_register.md`, `web_testing.md` |
| `search_code(pattern, path_glob, regex, limit)` | Search `src/` — how "the code wins" gets checked |
| `get_code(path, start, end, symbol)` | Read a file, a range, or one `def`/`class` |
| `feature_brief(feature)` | One call that grounds a session: specs with classifications, plans with status, the manual section, the source modules |

Prompts: `draft_spec` (ground → find the precedent → read the rules → offer
directions → write → list same-commit obligations) and
`check_spec_against_code`.

Ranking demotes aspirational specs but never hides them — they are still the
record of intent, and the banner is what actually protects the reader.

Search is in-memory and linear (the corpus is a few MB); code search is pure
Python rather than ripgrep, because the deploy host has no `rg` binary.

## Path safety

The endpoint is **unauthenticated** — a long random URL path is the only
credential — and the tree it reads is production. `src/dk_mcp/paths_service.py`
is an **allowlist**:

```
docs/   -> .md only
src/    -> .py .js .css .html .sql .json .txt .md
root    -> exactly CLAUDE.md
```

Order is load-bearing: reject malformed input (absolute paths, `%`-encoding,
backslashes, NUL, `~`), then `Path.resolve(strict=True)` — which collapses `..`
**and follows symlinks** — and only then check containment. Checking before
resolution is the classic hole: `docs/token.md` symlinked to `../.env` sits in
an allowed root with an allowed extension and is caught only afterwards.

On top of containment (never instead of it): no `.git`/`.venv`/`node_modules`
path component, no `.env*` basename, no `.db`/`.key`/`.pem` suffix, regular
files only, and the extension allowlist per root.

`walk()` re-validates every candidate and never follows symlinks, so a planted
symlink can neither be fetched nor surface as a search hit.

`tests/test_dk_mcp_paths_service.py` holds this boundary with 74 adversarial
cases — traversal in every dialect, absolute paths, URL-encoding, symlinks in
and out of the tree, fifos, directories, dangling links — plus an invariant
over the real checkout.

**The systemd unit enforces the same allowlist independently.**
`TemporaryFileSystem=/home/ben` replaces the home directory with an empty
tmpfs and only `docs/`, `src/` and `CLAUDE.md` are bound back in read-only, so
`.env`, the database and `.git/` do not exist in the service's mount
namespace. If the Python has a bug the kernel still refuses; if the unit is
wrong the Python still refuses.

There is **no write path and no database tool**, by design.

## Deployment

- Source `src/dk_mcp/`, deployed by `scripts/deploy_dk_mcp.sh` to `/opt/dk-mcp`
  with its own venv from `requirements-mcp.lock`.
- Unit `deploy/dk-mcp.service`; install per `deploy/README.md`.
- Config in `/opt/dk-mcp/dk-mcp.env` (mode 600): `DK_REPO_ROOT`, `DK_MCP_HOST`,
  `DK_MCP_PORT` (8322 — 8321 is the ToD server), `DK_MCP_PATH`,
  `DK_MCP_ALLOWED_HOSTS`. An `EnvironmentFile` rather than `Environment=`
  because `systemctl show` prints the latter to any local user without sudo,
  which would leak the endpoint secret.
- Cloudflare Tunnel hostname `dkmcp.billy-bots.com` → `http://127.0.0.1:8322`,
  managed in the Cloudflare dashboard.
- Transport: streamable HTTP, stateless, with `TransportSecuritySettings`
  Host-header pinning. Same shape as `/opt/tod/server.py`.

## Tests

`tests/test_dk_mcp_{paths,corpus,sections,search,code,conventions}_service.py`
— 168 tests. Beyond the path boundary, the ones worth knowing about: banners
must exist for every kind; a doc listed twice takes the cautionary label; an
unlisted plan admits it has no status; links in Notes cells are not
classifications; aspirational ranks below reference but is still returned; the
excluded corpora are unsearchable and unfetchable; and every conventions topic
names a source document that still exists in the repo.
