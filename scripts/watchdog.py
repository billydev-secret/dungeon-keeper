#!/usr/bin/env python3
"""Watchdog — DMs the owner on Discord when the bot's systemd unit goes down.

Runs as its own systemd service (see deploy/dungeon-keeper-watchdog.service)
so it survives bot crashes. Deliberately stdlib-only and run with the system
python3, independent of the repo venv: a broken deploy can't kill it.

Reads the repo .env for:
    BOT_ENV                  "prod" (default) or "dev" — picks the token
    DISCORD_TOKEN_PROD/_DEV  bot token used to send the DM (REST only,
                             works even while the bot process is down)
    WATCHDOG_USER_ID         Discord user to DM (falls back to SUPPORT_USER_ID)
    WATCHDOG_UNIT            systemd unit to watch (default dungeon-keeper.service)
    WATCHDOG_POLL_SECONDS    poll interval (default 30)

Alert logic: a non-active state must survive a 10 s recheck before it DMs
(so a fast auto-restart doesn't page), then one DM per outage and one on
recovery — never a DM per poll.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = "https://discord.com/api/v10"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("watchdog")


def parse_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #")[0].strip().strip("'\"")
        env[key.strip()] = value
    return env


def unit_state(unit: str) -> str:
    """Return the systemd ActiveState: active/inactive/failed/activating/..."""
    result = subprocess.run(
        ["systemctl", "is-active", unit],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result.stdout.strip() or "unknown"


def _post(token: str, path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "dungeon-keeper-watchdog (self-hosted, 1.0)",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def send_dm(token: str, user_id: str, content: str) -> bool:
    try:
        channel = _post(token, "/users/@me/channels", {"recipient_id": user_id})
        _post(token, f"/channels/{channel['id']}/messages", {"content": content})
        return True
    except urllib.error.HTTPError as exc:
        log.error("Discord API rejected DM (%s): %s", exc.code, exc.read().decode()[:300])
    except Exception:
        log.exception("Failed to send DM")
    return False


def main() -> int:
    env = parse_env(ROOT / ".env")
    bot_env = env.get("BOT_ENV", "prod").lower()
    token = env.get(f"DISCORD_TOKEN_{bot_env.upper()}", "")
    user_id = env.get("WATCHDOG_USER_ID") or env.get("SUPPORT_USER_ID", "")
    unit = env.get("WATCHDOG_UNIT", "dungeon-keeper.service")
    poll = int(env.get("WATCHDOG_POLL_SECONDS", "30"))

    if not token or not user_id:
        log.error(
            "Missing DISCORD_TOKEN_%s or WATCHDOG_USER_ID/SUPPORT_USER_ID in %s",
            bot_env.upper(),
            ROOT / ".env",
        )
        return 1

    if "--test" in sys.argv[1:]:
        ok = send_dm(token, user_id, f"🧪 Watchdog test — I can reach you. Watching **{unit}**.")
        log.info("Test DM %s", "sent" if ok else "FAILED")
        return 0 if ok else 1

    log.info("Watching %s every %ss; will DM user %s", unit, poll, user_id)
    down_since: float | None = None
    alerted = False

    while True:
        state = unit_state(unit)

        if state == "active":
            if alerted:
                mins = (time.time() - (down_since or time.time())) / 60
                send_dm(
                    token,
                    user_id,
                    f"🟢 **{unit}** is back up (was down ~{mins:.0f} min).",
                )
                log.info("Recovered; DM sent")
            down_since = None
            alerted = False
        else:
            if down_since is None:
                down_since = time.time()
                time.sleep(10)  # debounce: fast auto-restarts shouldn't page
                continue
            if not alerted:
                send_dm(
                    token,
                    user_id,
                    f"🔴 **{unit}** is **{state}** and has not come back after 10s. "
                    f"Check: `systemctl status {unit}` / `journalctl -u {unit} -n 50`",
                )
                alerted = True
                log.warning("Unit %s is %s; DM sent", unit, state)

        time.sleep(poll)


if __name__ == "__main__":
    sys.exit(main())
