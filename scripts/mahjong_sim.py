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

A game costs seconds of bot thinking, so anything past a few hundred wants
``-j``. Results do not depend on the worker count — game *i* always runs on
the stream derived from (seed, i).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
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
        "-j", "--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1),
        help="parallel worker processes (default: cores - 1)")
    parser.add_argument(
        "--top", type=int, default=None,
        help="show only the N worst lines instead of every line")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

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


if __name__ == "__main__":
    raise SystemExit(main())
