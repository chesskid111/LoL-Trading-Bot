"""Bootstrap champion qualitative profiles using LLM curation.

Usage:
    # Top-20 prototype with Anthropic Claude
    export ANTHROPIC_API_KEY=...
    python -m loltrader.tools.bootstrap_profiles --top 20 --patch 16.1

    # Specific champions with OpenAI
    python -m loltrader.tools.bootstrap_profiles \
        --champions "Caitlyn,Lulu,Yorick" --backend openai

    # Manual mode reads JSON answers from individual files in data/llm_seed/
    # (used to bootstrap from pre-generated synthesis without API calls)
    python -m loltrader.tools.bootstrap_profiles --top 20 --backend manual \
        --manual-dir data/llm_seed/

Writes draft profiles to ``data/champion_profiles_llm_draft.json`` (NOT the
canonical file). After human review, run with ``--promote`` to copy approved
entries into ``data/champion_profiles.json``.

Spec §1.3.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from loltrader.comp.llm_curator import (
    LLMCurator,
    load_draft_profiles,
    result_to_profile,
)
from loltrader.comp.profiles import load_profiles, save_profiles


def _manual_provider_factory(directory: Path):
    """Return a callable that reads pre-generated JSON synthesis from disk.

    Each champion gets one file at ``{directory}/{Champion}.json`` whose body
    is exactly what an LLM would return. This is how we seed the initial 20
    without API calls.
    """
    def provider(champion: str, prompt: str) -> str:
        path = directory / f"{champion}.json"
        if not path.exists():
            raise FileNotFoundError(
                f"Manual provider expected {path} (champion={champion})"
            )
        return path.read_text(encoding="utf-8")
    return provider


def _select_champions(args, log) -> list[tuple[str, float | None, float | None]]:
    """Resolve the list of champions to curate, returning (name, pickrate, winrate)."""
    if args.champions:
        names = [n.strip() for n in args.champions.split(",") if n.strip()]
        return [(n, None, None) for n in names]

    if args.top:
        stats_path = Path(args.stats)
        if not stats_path.exists():
            log.error("--top requires %s; run refresh_patch_stats first", stats_path)
            sys.exit(2)
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
        # Sort by pickrate descending
        stats.sort(key=lambda s: s.get("pickrate", 0.0), reverse=True)
        sel = stats[:args.top]
        return [(s["champion"], s.get("pickrate"), s.get("winrate")) for s in sel]

    log.error("Specify either --top N or --champions name1,name2")
    sys.exit(2)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--patch", required=True, help="Patch version, e.g. 16.1")
    p.add_argument("--league", default=None, help="Region filter for prompt context")
    p.add_argument("--top", type=int, help="Curate top N most-picked from patch_stats.json")
    p.add_argument("--champions", help="Comma-separated champion names")
    p.add_argument("--backend", default="manual",
                   choices=["anthropic", "openai", "manual"])
    p.add_argument("--model", default=None, help="Override default model name")
    p.add_argument("--manual-dir", default="data/llm_seed",
                   help="Directory of {Champion}.json files for manual backend")
    p.add_argument("--stats", default="data/patch_stats.json")
    p.add_argument("--out", default="data/champion_profiles_llm_draft.json")
    p.add_argument("--promote", action="store_true",
                   help="Copy reviewed draft profiles into the canonical file")
    p.add_argument("--canonical", default="data/champion_profiles.json")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    log = logging.getLogger(__name__)

    if args.promote:
        return _promote(args, log)

    selection = _select_champions(args, log)
    log.info("Curating %d champions on patch %s via backend=%s",
             len(selection), args.patch, args.backend)

    manual_provider = None
    if args.backend == "manual":
        manual_provider = _manual_provider_factory(Path(args.manual_dir))

    curator = LLMCurator(
        backend=args.backend,
        model=args.model,
        manual_provider=manual_provider,
    )

    last_updated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    profiles: dict = {}
    errors: list[tuple[str, str]] = []

    for champion, pickrate, winrate in selection:
        try:
            result = curator.curate_one(
                champion, args.patch, args.league, pickrate, winrate,
            )
            profile = result_to_profile(result, args.patch, last_updated)
            profiles[champion] = profile
        except Exception as e:
            log.error("Failed to curate %s: %s", champion, e)
            errors.append((champion, str(e)))

    if not profiles:
        log.error("No profiles produced; aborting")
        return 1

    save_profiles(profiles, args.out)
    log.info("Wrote %d profiles to %s (total API cost $%.3f)",
             len(profiles), args.out, curator.total_cost_usd)

    if errors:
        log.warning("%d champions failed: %s", len(errors),
                    ", ".join(c for c, _ in errors))

    return 0


def _promote(args, log) -> int:
    """Copy reviewed draft profiles into the canonical profiles file.

    Merge logic: canonical takes precedence on fields you may have manually
    edited (data_sources, validation_flags); draft takes precedence on
    qualitative dimensions.
    """
    draft = load_draft_profiles(args.out)
    if not draft:
        log.error("No draft profiles found at %s", args.out)
        return 1
    canonical = load_profiles(args.canonical)

    promoted = 0
    for name, draft_profile in draft.items():
        if name in canonical:
            # Merge: preserve canonical pro_stats (from Phase 1.2)
            draft_profile.pro_stats = canonical[name].pro_stats
        canonical[name] = draft_profile
        promoted += 1

    save_profiles(canonical, args.canonical)
    log.info("Promoted %d profiles into %s", promoted, args.canonical)
    return 0


if __name__ == "__main__":
    sys.exit(main())
