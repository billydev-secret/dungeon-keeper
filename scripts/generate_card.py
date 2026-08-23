#!/usr/bin/env python
"""Generate a candidate Meadow Card and report what it looks like.

Stage 3 of docs/plans/mahjong-card-generator.md. Enumerates hands from the
motif grammar, keeps only what the real linter accepts, and selects a card
for section balance, demand spread and pivot paths.

    scripts/generate_card.py --season 2026-winter -o card.json
    scripts/generate_card.py --per-section 5 --seed 7
    scripts/generate_card.py --pool-only          # just count the candidates

The values it writes are **provisional** — placeholders so the card can be
played at all. Price it for real by running `scripts/mahjong_sim.py` over
the output and setting each line from its measured completion rate, then
name the hands (stage 4). This script is not the card; it is the shortlist.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bot_modules.games.mahjong.card_gen import (  # noqa: E402
    build_card,
    candidates,
    pivot_report,
    select,
)
from bot_modules.games.mahjong.card_logic import (  # noqa: E402
    lint_card_data,
    load_card,
)

#: argparse wants a one-line summary; __doc__ is None under `python -OO`.
SUMMARY = (__doc__ or "Generate a candidate Meadow Card.").splitlines()[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=SUMMARY)
    parser.add_argument("--year", default="2026", help="digits for the year section")
    parser.add_argument("--season", default="2026-winter")
    parser.add_argument("--card-id", default="meadow-generated")
    parser.add_argument("--display-name", default="Meadow Card — Generated")
    parser.add_argument("--per-section", type=int, default=7)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("-o", "--out", type=Path, help="write the card JSON here")
    parser.add_argument(
        "--pool-only", action="store_true",
        help="report the candidate pool and stop")
    args = parser.parse_args(argv)

    pool = candidates(year=args.year)
    by_section = Counter(c.section for c in pool)
    print(f"pool: {len(pool)} lint-clean candidates", file=sys.stderr)
    for section, n in by_section.most_common():
        print(f"  {section:<20}{n:>6}", file=sys.stderr)
    if args.pool_only:
        return 0

    hands = select(pool, per_section=args.per_section, seed=args.seed)
    data = build_card(
        hands,
        card_id=args.card_id,
        display_name=args.display_name,
        season=args.season,
    )

    report = lint_card_data(data)
    for error in report.errors:
        print(f"✗ {error}", file=sys.stderr)
    for warning in report.warnings:
        print(f"! {warning}", file=sys.stderr)
    if not report.ok:
        # The generator is supposed to be structurally incapable of this.
        print("generated card does not lint — this is a bug", file=sys.stderr)
        return 1

    card = load_card(data)
    pivots = pivot_report(card)
    stranded = sorted(k for k, v in pivots.items() if v < 2)
    print(
        f"card: {len(card.hands)} hands across "
        f"{len(card.sections())} sections; "
        f"values {min(h.value for h in card.hands)}–"
        f"{max(h.value for h in card.hands)}",
        file=sys.stderr,
    )
    if stranded:
        print(
            f"! {len(stranded)} line(s) with fewer than 2 pivot neighbours: "
            + ", ".join(stranded),
            file=sys.stderr,
        )

    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
