"""Import gol.gg Champion Synergy TSV exports into synergies_expanded.json.

Usage:
    # After dumping TSVs into data/external/gol_gg/synergies/
    python -m loltrader.tools.import_gol_gg_synergies

    # Override input/output paths
    python -m loltrader.tools.import_gol_gg_synergies \\
        --input data/external/gol_gg/synergies/ \\
        --output data/processed/synergies_expanded.json

Spec: gol.gg Champion Synergy → data/processed/synergies_expanded.json
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from loltrader.external.gol_gg_synergies import (
    build_expanded_synergies,
    build_expanded_triples,
    save_synergies,
    save_triples,
    is_triple_tsv,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", default="data/external/gol_gg/synergies/",
                   help="Directory containing TSV files from gol.gg clipboard copy")
    p.add_argument("--output", default="data/processed/synergies_expanded.json",
                   help="Where to write the consolidated synergies JSON")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    log = logging.getLogger(__name__)

    input_dir = Path(args.input)
    if not input_dir.exists():
        log.error("input directory does not exist: %s", input_dir)
        return 1

    tsv_paths = sorted(input_dir.glob("*.tsv"))
    if not tsv_paths:
        log.error("no .tsv files found in %s — did you save the gol.gg clipboard output?",
                  input_dir)
        return 1

    # Auto-route: triples have 3 champion columns, pairs have 2
    pair_paths = [p for p in tsv_paths if not is_triple_tsv(p)]
    triple_paths = [p for p in tsv_paths if is_triple_tsv(p)]
    log.info("found %d TSV files: %d pair files, %d triple files",
             len(tsv_paths), len(pair_paths), len(triple_paths))

    # Process pairs
    if pair_paths:
        log.info("--- PAIRS ---")
        for p in pair_paths:
            log.info("  - %s", p.name)
        expanded = build_expanded_synergies(pair_paths)
        if expanded:
            by_type: dict[str, int] = {}
            for s in expanded:
                by_type[s.synergy_type] = by_type.get(s.synergy_type, 0) + 1
            log.info("=== Pair classification ===")
            for t, n in sorted(by_type.items()):
                log.info("  %-12s %d pairs", t, n)
            log.info("=== Top 5 pairs by winrate ===")
            for s in sorted(expanded, key=lambda s: -s.winrate)[:5]:
                log.info(f"  {s.champion_1:<14} + {s.champion_2:<14}  "
                         f"WR={s.winrate:.1%}  N={s.n_games_total:<3}  "
                         f"GD@15={s.avg_duo_gd_15:+.0f}  type={s.synergy_type}")
            save_synergies(expanded, Path(args.output))

    # Process triples
    if triple_paths:
        log.info("--- TRIPLES ---")
        for p in triple_paths:
            log.info("  - %s", p.name)
        triples = build_expanded_triples(triple_paths)
        if triples:
            by_type = {}
            for t in triples:
                by_type[t.synergy_type] = by_type.get(t.synergy_type, 0) + 1
            log.info("=== Triple classification ===")
            for t, n in sorted(by_type.items()):
                log.info("  %-12s %d triples", t, n)
            log.info("=== Top 5 triples by winrate ===")
            for t in sorted(triples, key=lambda t: -t.winrate)[:5]:
                log.info(f"  {t.champion_1:<10} + {t.champion_2:<10} + {t.champion_3:<10}  "
                         f"WR={t.winrate:.1%}  N={t.n_games_total:<3}  "
                         f"GD@15={t.avg_duo_gd_15:+.0f}  type={t.synergy_type}")
            triples_output = Path(args.output).parent / "triples_expanded.json"
            save_triples(triples, triples_output)

    log.info("DONE.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
