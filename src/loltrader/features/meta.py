"""Meta features: patch context, league, format, playoff status."""
from __future__ import annotations

import sqlite3
from datetime import datetime

# Leagues we one-hot encode. Anything else collapses into "other".
LEAGUE_BUCKETS: tuple[str, ...] = (
    "LCK", "LPL", "LEC", "LCS", "LTA",
    "MSI", "Worlds", "WLDs", "First Stand", "FS",
    "EWC", "ENC",
)


def meta_features(
    conn: sqlite3.Connection,
    league: str | None,
    bo_format: int | None,
    playoffs: int | None,
    patch_id: int | None,
    as_of_date: str,
) -> dict[str, float]:
    out: dict[str, float] = {}

    # League one-hot
    norm_league = (league or "").strip()
    for b in LEAGUE_BUCKETS:
        out[f"league_{b.replace(' ', '_').lower()}"] = float(norm_league == b)
    out["league_other"] = float(norm_league not in LEAGUE_BUCKETS and norm_league != "")

    # Format
    out["bo_format"] = float(bo_format or 1)
    out["is_bo3"] = float(bo_format == 3)
    out["is_bo5"] = float(bo_format == 5)

    # Playoff flag
    out["is_playoffs"] = float(bool(playoffs))

    # Patch context: days into the current patch
    if patch_id is not None:
        patch_row = conn.execute(
            "SELECT first_seen FROM patches WHERE patch_id = ?", (patch_id,)
        ).fetchone()
        if patch_row and patch_row["first_seen"]:
            try:
                first_seen = datetime.strptime(patch_row["first_seen"], "%Y-%m-%d")
                as_of = datetime.strptime(as_of_date, "%Y-%m-%d")
                days_in_patch = max(0, (as_of - first_seen).days)
            except ValueError:
                days_in_patch = 0
        else:
            days_in_patch = 0
    else:
        days_in_patch = 0
    out["days_into_patch"] = float(days_in_patch)
    out["is_first_week_of_patch"] = float(days_in_patch < 7)

    return out
