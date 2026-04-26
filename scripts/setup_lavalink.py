"""One-shot, idempotent installer for the Lavalink JAR + LavaSrc plugin.

Run from the repo root:

    python scripts/setup_lavalink.py

Verifies Java 17+, downloads pinned versions, and creates a templated
application.yml if one doesn't already exist. Cross-platform.
"""

from __future__ import annotations

import re
import subprocess
import sys
import urllib.request
from pathlib import Path

# Make the services/ package importable when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.lavalink_manager import find_java  # noqa: E402

# Pin versions in lockstep with lavalink/application.yml.
# Lavalink 4.2.0+ is REQUIRED -- earlier versions don't speak the DAVE
# (E2EE voice) protocol that Discord now mandates, so voice WebSockets
# get closed with code 4017 and no audio ever reaches Discord.
LAVALINK_VERSION = "4.2.2"
LAVASRC_VERSION = "4.2.0"
YOUTUBE_PLUGIN_VERSION = "1.18.0"

LAVALINK_URL = (
    f"https://github.com/lavalink-devs/Lavalink/releases/download/"
    f"{LAVALINK_VERSION}/Lavalink.jar"
)
LAVASRC_URL = (
    f"https://github.com/topi314/LavaSrc/releases/download/"
    f"{LAVASRC_VERSION}/lavasrc-plugin-{LAVASRC_VERSION}.jar"
)
YOUTUBE_PLUGIN_URL = (
    f"https://github.com/lavalink-devs/youtube-source/releases/download/"
    f"{YOUTUBE_PLUGIN_VERSION}/youtube-plugin-{YOUTUBE_PLUGIN_VERSION}.jar"
)

REPO_ROOT = Path(__file__).resolve().parents[1]
LAVALINK_DIR = REPO_ROOT / "lavalink"
PLUGINS_DIR = LAVALINK_DIR / "plugins"
LOGS_DIR = LAVALINK_DIR / "logs"
JAR_PATH = LAVALINK_DIR / "Lavalink.jar"
# Sidecar file that records which version we last downloaded -- lets us
# detect a version bump and force a re-download instead of silently keeping
# a stale JAR.
JAR_VERSION_MARKER = LAVALINK_DIR / ".lavalink-version"
LAVASRC_PATH = PLUGINS_DIR / f"lavasrc-plugin-{LAVASRC_VERSION}.jar"
YOUTUBE_PLUGIN_PATH = PLUGINS_DIR / f"youtube-plugin-{YOUTUBE_PLUGIN_VERSION}.jar"


def check_java() -> None:
    """Verify Java 17+ is available; abort with a clear message otherwise.

    Looks on PATH, then JAVA_HOME, then standard install dirs (Adoptium etc.)
    via the same logic the music cog uses at runtime, so the two paths agree.
    """
    java = find_java()
    if not java:
        sys.exit(
            "ERROR: 'java' not found on PATH or in standard install dirs.\n"
            "Install Java 17 or newer:\n"
            "  Windows: https://adoptium.net/temurin/releases/?version=17\n"
            "  macOS:   brew install --cask temurin@17\n"
            "  Linux:   apt install openjdk-17-jre-headless\n"
            "Or set JAVA_HOME to your existing JDK install."
        )
    try:
        proc = subprocess.run(
            [java, "-version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        sys.exit(f"ERROR: failed to invoke java -version: {exc}")

    blob = (proc.stderr or "") + (proc.stdout or "")
    m = re.search(r'version "(\d+)(?:\.(\d+))?', blob)
    if not m:
        print(f"WARNING: could not parse Java version from output:\n{blob}")
        print(f"[OK] using {java}")
        return
    major = int(m.group(1))
    if major < 17:
        sys.exit(
            f"ERROR: Java {major} detected at {java}; Lavalink v4 needs Java 17+."
        )
    print(f"[OK] Java {major} at {java}")


def download(url: str, dest: Path) -> None:
    print(f"[..] Downloading {url}")
    print(f"     -> {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        urllib.request.urlretrieve(url, tmp)  # noqa: S310 - pinned URLs only
        tmp.replace(dest)
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise
    print(f"[OK] {dest.name}")


def ensure_dirs() -> None:
    for d in (LAVALINK_DIR, PLUGINS_DIR, LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def cleanup_old_plugins() -> None:
    """Remove stale plugin JARs that don't match the current pin."""
    keep = {LAVASRC_PATH.name, YOUTUBE_PLUGIN_PATH.name}
    for prefix in ("lavasrc-plugin-", "youtube-plugin-"):
        for old in PLUGINS_DIR.glob(f"{prefix}*.jar"):
            if old.name in keep:
                continue
            print(f"[..] Removing stale plugin {old.name}")
            old.unlink()


def main() -> None:
    print(
        f"Lavalink setup -- target Lavalink {LAVALINK_VERSION}, "
        f"LavaSrc {LAVASRC_VERSION}, youtube-source {YOUTUBE_PLUGIN_VERSION}"
    )
    check_java()
    ensure_dirs()

    installed = (
        JAR_VERSION_MARKER.read_text(encoding="utf-8").strip()
        if JAR_VERSION_MARKER.exists()
        else None
    )
    if JAR_PATH.exists() and installed == LAVALINK_VERSION:
        print(f"[OK] Lavalink {LAVALINK_VERSION} already installed")
    else:
        if JAR_PATH.exists():
            print(
                f"[..] Replacing Lavalink {installed or 'unknown'} -> "
                f"{LAVALINK_VERSION}"
            )
            try:
                JAR_PATH.unlink()
            except OSError as exc:
                sys.exit(
                    f"ERROR: could not remove {JAR_PATH}: {exc}\n"
                    "Stop the bot first (Lavalink holds the file open)."
                )
        download(LAVALINK_URL, JAR_PATH)
        JAR_VERSION_MARKER.write_text(LAVALINK_VERSION, encoding="utf-8")

    if LAVASRC_PATH.exists():
        print(f"[OK] {LAVASRC_PATH.name} already present")
    else:
        download(LAVASRC_URL, LAVASRC_PATH)

    if YOUTUBE_PLUGIN_PATH.exists():
        print(f"[OK] {YOUTUBE_PLUGIN_PATH.name} already present")
    else:
        download(YOUTUBE_PLUGIN_URL, YOUTUBE_PLUGIN_PATH)

    cleanup_old_plugins()

    print()
    print("Done. Next steps:")
    print("  1. Set SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, LAVALINK_PASSWORD in .env")
    print("  2. Start the bot - the music cog will spawn Lavalink automatically")


if __name__ == "__main__":
    main()
