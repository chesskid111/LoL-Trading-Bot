"""Comp evaluation engine.

See docs/superpowers/specs/2026-06-06-comp-evaluation-engine-design.md for the
full design. This package owns layers 1-3 of the engine:

    profiles.py       — Layer 1: champion profile loading + schema
    pro_stats.py      — Layer 1: gol.gg + Oracle's Elixir ETL
    llm_curator.py    — Layer 1: LLM-aggregated qualitative dimensions
    aggregator.py     — Layer 2: per-team comp evaluation
    matchup.py        — Layer 3: lane + comp matchup
    matchup_data.py   — Layer 3: gol.gg matchup data ETL

Layer 4 (live state integration) and Layer 5 (win-prob model) live under
loltrader.winprob.
"""
