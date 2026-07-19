# Remote test dispatch

Run the test suite on a faster machine over SSH, falling back to local
automatically when that machine is off. Opt-in: unset `REMOTE_TEST_HOST` and
nothing changes.

The prod box is a 2-core VM (a Synology VMM guest sharing an embedded Ryzen),
where the full suite takes ~10 minutes. A spare desktop cuts that to a few.

## Why native Windows is fine

The suite is portable, which was checked rather than assumed:

- **Zero** POSIX-only stdlib imports (`fcntl`, `pwd`, `grp`, `resource`, …)
- Exactly one `open()` without an explicit `encoding=`, and it is binary mode
- `lavalink_manager.py` is already cross-platform (CTRL_BREAK_EVENT vs SIGTERM)
- `gate.py` already looks for `.venv/Scripts/python.exe`
- Every dependency resolves for cp314 on Windows — `mediapipe`, `onnxruntime`,
  `ctranslate2`, `pynacl` included. Verify with:

  ```bash
  uv pip compile requirements-dev.txt -o /tmp/win.lock -p 3.14 \
      --python-platform x86_64-pc-windows-msvc
  ```

**Nothing secret is synced, and nothing secret is needed.** The suite reads no
`.env`, no database, and no model weights — `tests/conftest.py` touches no
environment variables at all. A plain `git clone` is a complete test host.

### The known risk

Windows cannot delete an open file. With ~5,900 tests creating temp SQLite
databases, any test leaving a connection open fails at `tmp_path` teardown
rather than in the test body. Relatedly, `xdist` uses spawn rather than fork on
Windows, so fixtures relying on fork-inherited state behave differently.

Neither is knowable without running it. If teardown failures turn out to be
widespread, switch to WSL2 (below) rather than fighting them.

Remember the signal here is a **fast pre-filter**, not proof: prod is Linux, and
CI on push remains the authoritative gate. A Linux-only bug can pass here.

## Setting up the remote (native Windows)

The fastest route is to run a Claude Code session *on the Windows box* and let
it do the setup locally — installing Python, resolving pip failures, and
triaging the first run all want fast filesystem access, and SSH isn't up yet.

1. Install **Python 3.14**, **git**, and the **OpenSSH Server** optional feature
   (Settings → System → Optional features → OpenSSH Server), then
   `Start-Service sshd; Set-Service -Name sshd -StartupType Automatic`.
2. Add your public key to `C:\Users\<you>\.ssh\authorized_keys`. Key auth is
   required, not optional: the runner uses `BatchMode=yes`, so a password prompt
   makes it fall back to local instead of hanging a commit hook.
3. Clone the repo and build the venv:

   ```powershell
   git clone <repo> C:\dev\dungeon-keeper
   cd C:\dev\dungeon-keeper
   py -3.14 -m venv .venv
   .venv\Scripts\pip install -r requirements-dev.lock
   ```

   If that pin has no Windows wheel, compile a Windows-specific lock with the
   `uv` command above and use it instead.
4. Confirm it runs: `.venv\Scripts\python -m pytest -n 12 -x`

## Configuration

Set these where your shell will see them (the pre-commit hook inherits them):

| Variable | Meaning |
|---|---|
| `REMOTE_TEST_HOST` | `user@host`. **Unset ⇒ everything runs locally.** |
| `REMOTE_TEST_DIR` | Repo path on that host, e.g. `C:/dev/dungeon-keeper` |
| `REMOTE_TEST_PYTHON` | e.g. `C:/dev/dungeon-keeper/.venv/Scripts/python.exe` |
| `REMOTE_TEST_JOBS` | xdist workers, default 12 — leaves a desktop usable |
| `REMOTE_TEST_CD` | Override the `cd` template, e.g. `cd /d {dir} && {cmd}` |
| `GATE_NO_REMOTE=1` | Force local for one run |

Forward slashes work fine in Windows paths here. Use `REMOTE_TEST_CD` with
`cd /d` only if the repo sits on a different drive than the SSH session's
default.

Setting `REMOTE_TEST_HOST` **without** `DIR` and `PYTHON` is a hard error rather
than a quiet fallback — a half-configured remote that silently never dispatches
is worse than one that complains.

## How it works

`gate.py` routes all three of its pytest call sites through `run_pytest()`,
which asks `scripts/remote_test.py` first and falls back locally whenever it
returns `None`:

1. Probe with `ssh -o ConnectTimeout=3 -o BatchMode=yes`
2. Ship `src/ tests/ scripts/ pyproject.toml` via a **tar pipe** — no rsync,
   which native Windows lacks. A few MB, ~1-2s on a LAN.
3. Run `python -m pytest -n <jobs> <targets>` over SSH, streaming output
4. Return the remote exit code

Every "can't dispatch" path returns `None` → local run: host unset, host
unreachable, sync failed, `GATE_NO_REMOTE`, or an argument that cannot be
safely embedded in the remote command string. A remote *test failure* is
different — that propagates and fails the gate, as it should.

### Argument safety

Arguments containing spaces, quotes, or shell metacharacters are **refused**
rather than quoted, because correct quoting differs between cmd.exe,
PowerShell, and bash. `-k spam and eggs` falls back to local. Use
`-ktest_foo` (no spaces) to keep dispatch, or `GATE_NO_REMOTE=1`.

### Keeping the remote venv in sync

Handled automatically. Both lock files are synced, and the remote command runs
`scripts/remote_test.py --bootstrap` rather than pytest directly. On the far
side that:

1. Hashes `requirements.lock` + `requirements-dev.lock`
2. Compares against `.remote-test-stamp` in the remote checkout (gitignored)
3. Reinstalls from `requirements-dev.lock` and rewrites the stamp if they differ
4. Runs pytest

So a Dependabot bump or a local `uv pip compile` is picked up on the next
dispatch — you can't silently test new code against old dependencies.

The check lives on the remote deliberately: reading a file over SSH would need
a shell one-liner, and `cat` (bash) vs `type` (cmd.exe) diverge. Running plain
Python there sidesteps the whole quoting problem.

If the reinstall fails, the remote exits with a **sentinel code (97)** — chosen
outside pytest's 0-5 range — and the local side falls back to running the suite
locally. A broken remote venv is an environment problem, not a red suite. A
genuine test failure still propagates and fails the gate.

The first dispatch after setup will always reinstall, since no stamp exists yet.

## WSL2 fallback

If Windows-native teardown failures prove widespread, WSL2 gives an identical
Linux userland. Fedora is now an official WSL distro, so you can match prod
exactly (`wsl --install FedoraLinux-42`, then `dnf upgrade`). Two rules:

- **Keep the repo in WSL's ext4 (`~/`), never `/mnt/c`** — cross-filesystem I/O
  is roughly 10× slower and this suite is heavy on temp SQLite files. Getting
  this wrong erases the entire speedup.
- Set `networkingMode=mirrored` in `.wslconfig` so WSL shares the host IP.
  Otherwise its NAT address changes every boot. Don't use `netsh portproxy`.

The runner needs no changes for WSL — only different `REMOTE_TEST_*` values.
