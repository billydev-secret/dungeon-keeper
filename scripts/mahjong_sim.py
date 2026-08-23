#!/usr/bin/env python
"""Play a Meadow Card headlessly and print what it measures.

Stage 2 of docs/plans/mahjong-card-generator.md; the CLI half of
``sim_logic.simulate``, the way ``validate_card.py`` is the CLI half of the
linter. Every seat is a bot, so a card can be measured before a member ever
sees it.

    scripts/mahjong_sim.py                      # First Light, 100 games
    scripts/mahjong_sim.py card.json -n 2000 -j 8
    scripts/mahjong_sim.py --seats 2 --wall-trim 60   # Duel pacing
    scripts/mahjong_sim.py --json > report.json
    scripts/mahjong_sim.py -n 2000 --remote          # on the test-runner box

A game costs seconds of bot thinking, so anything past a few hundred wants
``-j``. Results do not depend on the worker count — game *i* always runs on
the stream derived from (seed, i) — so a remote run and a local one at the
same seed are directly comparable, and `--remote` is purely a throughput
decision.

``--remote`` reuses the pytest test-runner transport (`remote_test.py`): it
syncs the tree and runs this same script over there. The whole simulation
stack imports nothing outside the standard library, so the remote needs no
install — which is why this does not go through the dependency bootstrap the
pytest path uses. If the host is not configured or not reachable, the run
happens locally instead of failing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bot_modules.games.mahjong.card_logic import (  # noqa: E402
    CardError,
    load_card_file,
    load_first_light,
    lint_card,
)
from bot_modules.games.mahjong.sim_logic import (  # noqa: E402
    format_report,
    simulate,
)

#: argparse wants a one-line summary; __doc__ is None under `python -OO`.
SUMMARY = (__doc__ or "Simulate a Meadow Card headlessly.").splitlines()[0]

def synced_roots() -> tuple[str, ...]:
    """Top-level directories `remote_test.sync` actually ships.

    Read from the transport rather than restated here: a card outside them
    would simply not exist on the far side, and a hand-copied list would go
    quietly wrong the day either end moves.
    """
    sys.path.insert(0, str(ROOT))
    from scripts import remote_test

    return tuple(
        p for p in remote_test.SYNC_PATHS if not Path(ROOT / p).is_file()
    )


def remote_card_arg(card: Path | None, root: Path = ROOT) -> str | None:
    """The card path as the remote will see it, or None for the default card.

    Raises ValueError when the file would not survive the sync.
    """
    if card is None:
        return None
    roots = synced_roots()
    where = ", ".join(f"{r}/" for r in roots)
    try:
        relative = card.resolve().relative_to(root)
    except ValueError:
        raise ValueError(
            f"{card} is outside the repo, so --remote cannot ship it. "
            f"Put the card under one of {where} — cards live in "
            f"src/bot_modules/games/mahjong/cards/."
        ) from None
    if relative.parts[0] not in roots:
        raise ValueError(
            f"{relative} is in the repo but is not synced to the test "
            f"runner. Move it under one of {where}."
        )
    return relative.as_posix()


def _remote_argv(args, card_arg: str | None) -> list[str]:
    """Rebuild this invocation for the far side, minus --remote itself."""
    argv = [card_arg] if card_arg else []
    argv += [
        "-n", str(args.games), "--seats", str(args.seats),
        "--seed", str(args.seed), "--wall-trim", str(args.wall_trim),
        "-j", str(args.workers),
    ]
    if args.no_second_charleston:
        argv.append("--no-second-charleston")
    if args.rank_by_effort:
        argv.append("--rank-by-effort")
    if args.top is not None:
        argv += ["--top", str(args.top)]
    if args.json:
        argv.append("--json")
    return argv


def main(argv: list[str] | None = None) -> int:
    # The test runner is Windows, whose stdout defaults to cp1252 when piped;
    # the report's em-dashes and warning glyph came back as replacement
    # characters over SSH until this. Best-effort: a stream that cannot be
    # reconfigured is left alone.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # a redirected stream in a test harness
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except ValueError:  # pragma: no cover - detached/closed stream
            pass

    parser = argparse.ArgumentParser(description=SUMMARY)
    parser.add_argument(
        "card", nargs="?", type=Path,
        help="card JSON to play (default: the shipped First Light)")
    parser.add_argument("-n", "--games", type=int, default=100)
    parser.add_argument("--seats", type=int, default=4, choices=(2, 4))
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--wall-trim", type=int, default=0)
    parser.add_argument(
        "--no-second-charleston", action="store_true",
        help="play with the second Charleston disabled")
    parser.add_argument(
        "-j", "--workers", type=int, default=None,
        help="parallel worker processes (default: cores - 1 locally, or the "
             "runner's configured job count with --remote)")
    parser.add_argument(
        "--top", type=int, default=None,
        help="show only the N worst lines instead of every line")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--rank-by-effort", action="store_true",
        help="rank lines by acquisition effort rather than raw tile "
             "distance (experiment; production ranks by distance)")
    parser.add_argument(
        "--remote", action="store_true",
        help="run on the configured pytest test-runner box instead "
             "(falls back to local if it is unreachable)")
    args = parser.parse_args(argv)

    if args.remote:
        code = _dispatch_remote(args)
        if code is not None:
            return code
        # None means "not available" — fall through and run it here.

    if args.workers is None:
        args.workers = parser_default_workers()

    try:
        card = load_card_file(args.card) if args.card else load_first_light()
    except CardError as e:
        for problem in e.problems:
            print(f"✗ {problem}", file=sys.stderr)
        return 2

    report = lint_card(card)
    if not report.ok:
        # Playing a card the linter rejects would measure nonsense.
        for error in report.errors:
            print(f"✗ {error}", file=sys.stderr)
        return 2

    result = simulate(
        card,
        games=args.games,
        seat_count=args.seats,
        seed=args.seed,
        wall_trim=args.wall_trim,
        second_charleston=not args.no_second_charleston,
        workers=args.workers,
        rank_by_effort=args.rank_by_effort,
    )

    if args.json:
        payload = asdict(result)
        payload["win_rate"] = result.win_rate
        payload["wall_game_rate"] = result.wall_game_rate
        payload["mean_turns"] = result.mean_turns
        payload["healthy"] = result.healthy
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(format_report(result, limit=args.top))

    # A card that never finishes is a finding, not a crash: exit 0 either
    # way, and reserve non-zero for "this run cannot be trusted".
    return 0 if result.healthy else 1


def _dispatch_remote(args) -> int | None:
    """Hand this run to the test-runner box; None ⇒ run locally after all."""
    sys.path.insert(0, str(ROOT))
    from scripts import remote_test

    try:
        card_arg = remote_card_arg(args.card)
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2

    python = remote_test.remote_python()
    if python is None:
        print("no test runner configured — running locally.", file=sys.stderr)
        return None
    if args.workers is None:
        # No -j given. This box's core count says nothing about the
        # remote's, so use what the runner is configured for. An explicit
        # -j is the caller's decision and is passed through untouched.
        args.workers = remote_test.remote_jobs() or parser_default_workers()

    forwarded = _remote_argv(args, card_arg)
    try:
        # The remote may be cmd.exe, so POSIX quoting is the wrong tool —
        # remote_test's own validator is the one that knows what survives
        # the transport. Every argument here is a number or a slash path,
        # so this only ever fires on a pathological filename.
        remote_test.check_args(forwarded)
    except remote_test.UnsafeArgument as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2

    command = " ".join([python, "scripts/mahjong_sim.py", *forwarded])
    return remote_test.exec_remote(command)


def parser_default_workers() -> int:
    return max(1, (os.cpu_count() or 2) - 1)


if __name__ == "__main__":
    raise SystemExit(main())
