"""Music cog - Lavalink subprocess lifecycle.

Spawns a Java child process running Lavalink.jar, polls its TCP port until
ready, and shuts it down cleanly on cog_unload / bot shutdown.

Cross-platform: Windows uses CTRL_BREAK_EVENT (requires CREATE_NEW_PROCESS_GROUP
at spawn); POSIX uses SIGTERM. Both fall back to kill() after a grace period.

All subprocess invocations use the asyncio list-argv variant (no shell), so
arguments are passed verbatim - no command-injection surface.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger("dungeonkeeper.music.lavalink")


class LavalinkStartupError(RuntimeError):
    pass


_BOT_ROOT = Path(__file__).resolve().parents[1]
_LAVALINK_DIR = _BOT_ROOT / "lavalink"
_JAR_PATH = _LAVALINK_DIR / "Lavalink.jar"
_LOG_DIR = _LAVALINK_DIR / "logs"

_PORT_POLL_INTERVAL_S = 0.5
_STARTUP_TIMEOUT_S = 30.0
_SHUTDOWN_GRACE_S = 10.0


def find_java() -> str | None:
    """Locate a usable `java` executable.

    Order: PATH, JAVA_HOME, then common Windows install dirs (Adoptium / Microsoft
    / Java / Zulu / Liberica), Linux paths, macOS Homebrew. Returns the absolute
    path or None if nothing was found. The Adoptium installer in particular does
    not add Java to PATH by default on Windows, hence this fallback.
    """
    found = shutil.which("java")
    if found:
        return found

    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        exe = Path(java_home) / "bin" / ("java.exe" if sys.platform == "win32" else "java")
        if exe.exists():
            return str(exe)

    candidates: list[Path] = []
    if sys.platform == "win32":
        program_files = [
            Path(p) for p in (
                os.environ.get("ProgramFiles", r"C:\Program Files"),
                os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                Path(os.environ.get("LOCALAPPDATA", "")) / "Programs"
                if os.environ.get("LOCALAPPDATA")
                else None,
            ) if p
        ]
        vendors = (
            "Eclipse Adoptium",
            "Microsoft",
            "Java",
            "Zulu",
            "BellSoft",
            "Amazon Corretto",
            "Semeru",
        )
        for pf in program_files:
            for vendor in vendors:
                base = pf / vendor
                if base.exists():
                    candidates.extend(base.glob("*/bin/java.exe"))
                    candidates.extend(base.glob("jdk-*/bin/java.exe"))
                    candidates.extend(base.glob("zulu-*/bin/java.exe"))
    else:
        candidates.extend(Path("/usr/lib/jvm").glob("*/bin/java"))
        candidates.extend(Path("/opt/homebrew/opt").glob("openjdk*/bin/java"))
        candidates.extend(Path("/usr/local/opt").glob("openjdk*/bin/java"))

    # Sort to prefer higher version suffix (lexicographic isn't perfect but
    # close enough for "21" > "17" > "11" > "8" cases).
    candidates = sorted({c for c in candidates if c.exists()}, reverse=True)
    return str(candidates[0]) if candidates else None


class LavalinkManager:
    def __init__(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        password: str | None = None,
        heap_mb: int | None = None,
    ) -> None:
        self.host = host or os.getenv("LAVALINK_HOST", "127.0.0.1")
        self.port = int(port if port is not None else os.getenv("LAVALINK_PORT", "2333"))
        self.password = password or os.getenv("LAVALINK_PASSWORD", "")
        self.heap_mb = int(
            heap_mb if heap_mb is not None else os.getenv("LAVALINK_HEAP_MB", "512")
        )
        self._proc: asyncio.subprocess.Process | None = None
        self._started_at: float | None = None
        self._log_handle = None

    async def start(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            log.info("Lavalink already running (pid=%s)", self._proc.pid)
            return
        if not _JAR_PATH.exists():
            raise LavalinkStartupError(
                f"{_JAR_PATH} not found. Run: python scripts/setup_lavalink.py"
            )
        if not self.password:
            raise LavalinkStartupError(
                "LAVALINK_PASSWORD is empty. Set it in .env."
            )
        # If something is already listening on the port (orphan Java from a
        # previous unclean shutdown, or a manually-started Lavalink), don't
        # spawn a duplicate -- just adopt it. Avoids the silent "wavelink
        # connects to one Lavalink, our manager owns a different one" failure.
        if await self._port_in_use():
            log.warning(
                "Port %s already in use -- assuming an existing Lavalink is "
                "running and skipping subprocess spawn. Wavelink will connect "
                "to whatever's listening.",
                self.port,
            )
            self._started_at = time.monotonic()
            return
        java_exe = find_java()
        if not java_exe:
            raise LavalinkStartupError(
                "Java 17+ not found on PATH or in standard install dirs. "
                "Install Java 17 (https://adoptium.net/temurin/releases/?version=17) "
                "or set JAVA_HOME."
            )

        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = _LOG_DIR / "lavalink.stdout.log"
        # Append; logback (configured in application.yml) handles structured
        # log rotation. This pipe captures startup chatter and JVM stderr.
        self._log_handle = log_path.open("ab", buffering=0)

        argv = [java_exe, f"-Xmx{self.heap_mb}M", "-jar", str(_JAR_PATH)]
        kwargs: dict = {
            "stdout": self._log_handle,
            "stderr": self._log_handle,
            "cwd": str(_LAVALINK_DIR),
        }
        env = os.environ.copy()
        env.setdefault("LAVALINK_HOST", self.host)
        env.setdefault("LAVALINK_PORT", str(self.port))
        env.setdefault("LAVALINK_PASSWORD", self.password)
        kwargs["env"] = env

        if sys.platform == "win32":
            # CREATE_NEW_PROCESS_GROUP lets us deliver CTRL_BREAK_EVENT later.
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        log.info("Starting Lavalink: %s (cwd=%s)", " ".join(argv), _LAVALINK_DIR)
        self._proc = await asyncio.create_subprocess_exec(*argv, **kwargs)
        self._started_at = time.monotonic()

        try:
            await self._wait_for_port()
        except LavalinkStartupError:
            await self._kill_silent()
            raise

        log.info("Lavalink up on %s:%d (pid=%s)", self.host, self.port, self._proc.pid)

    async def _port_in_use(self) -> bool:
        try:
            _r, w = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=1.0
            )
        except (OSError, asyncio.TimeoutError):
            return False
        w.close()
        try:
            await w.wait_closed()
        except Exception:
            pass
        return True

    async def _wait_for_port(self) -> None:
        deadline = time.monotonic() + _STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._proc and self._proc.returncode is not None:
                raise LavalinkStartupError(
                    f"Lavalink exited during startup (code={self._proc.returncode}); "
                    f"check {_LOG_DIR / 'lavalink.stdout.log'}"
                )
            try:
                _reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port), timeout=1.0
                )
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                return
            except (OSError, asyncio.TimeoutError):
                await asyncio.sleep(_PORT_POLL_INTERVAL_S)
        raise LavalinkStartupError(
            f"Lavalink did not bind {self.host}:{self.port} within "
            f"{_STARTUP_TIMEOUT_S:.0f}s; check {_LOG_DIR / 'lavalink.stdout.log'}"
        )

    async def stop(self) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            self._close_log()
            self._proc = None
            return

        log.info("Stopping Lavalink (pid=%s)", proc.pid)
        try:
            if sys.platform == "win32":
                # CTRL_BREAK_EVENT requires the child be in its own process group.
                try:
                    os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
                except (OSError, ValueError):
                    proc.terminate()
            else:
                proc.terminate()  # SIGTERM
        except ProcessLookupError:
            pass

        try:
            await asyncio.wait_for(proc.wait(), timeout=_SHUTDOWN_GRACE_S)
        except asyncio.TimeoutError:
            log.warning(
                "Lavalink did not exit within %.0fs; sending kill",
                _SHUTDOWN_GRACE_S,
            )
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                log.error("Lavalink still alive after kill; orphan possible")

        log.info("Lavalink exited (code=%s)", proc.returncode)
        self._close_log()
        self._proc = None

    async def _kill_silent(self) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except (ProcessLookupError, asyncio.TimeoutError):
            pass

    def _close_log(self) -> None:
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except Exception:
                pass
            self._log_handle = None

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def health_check(self) -> dict:
        return {
            "alive": self.is_alive(),
            "pid": self._proc.pid if self._proc else None,
            "uptime_s": (time.monotonic() - self._started_at)
            if self._started_at and self.is_alive()
            else 0.0,
        }
