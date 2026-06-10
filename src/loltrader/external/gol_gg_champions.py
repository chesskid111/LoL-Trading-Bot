"""Parse gol.gg Champions ranking TSV exports + audit against our profiles.

gol.gg's Champions page has per-role rankings with columns like:
  Champion, # Games, Winrate, Pickrate, Banrate, KDA, Gold@15, CS@15, DMG/min

We extract per-role TSVs (top/jungle/mid/bot/support), parse, and then
compare empirical winrate patterns against our champion_profiles.json
qualitative ratings to surface likely mis-rated champions.

Audit logic:
  For each champion, compare:
    - Profile scaling_late vs empirical winrate by game length
      (gol.gg shows winrate but not by length — use proxy: if winrate is
       above baseline AND champion is meta, likely scales well)
    - Profile teamfight_score vs empirical KDA + KP combined
    - Profile baron_dps_tier vs damage/minute
  Flag champions where profile disagrees with data.
"""
from __future__ import annotations

import csv
import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loltrader.external.schemas import GolGGChampionStatRow
from loltrader.external.gol_gg_synergies import normalize_champion

log = logging.getLogger(__name__)


# Thresholds for audit flagging
MIN_PICKRATE_FOR_AUDIT = 0.10        # only audit picks that show up regularly
MIN_GAMES_FOR_AUDIT = 15              # minimum sample
WINRATE_DELTA_THRESHOLD = 0.06        # 6pp deviation from baseline → flag
WINRATE_BASELINE = 0.50               # pro winrate baseline


# Map gol.gg role names → our canonical lower-case
ROLE_NORM = {
    "top": "top",
    "jungle": "jungle", "jgl": "jungle", "jg": "jungle",
    "mid": "mid", "middle": "mid",
    "bot": "bot", "adc": "bot", "bottom": "bot",
    "support": "support", "sup": "support", "supp": "support",
}

# Known league codes — used for filename detection
LEAGUE_CODES = {"lck", "lpl", "lec", "lcs", "lcp", "lta", "ltan",
                 "msi", "worlds", "iem", "ewc", "msc"}


def parse_filename_metadata(filename: str) -> tuple[str | None, str | None, str | None]:
    """Parse role, league, and patch from filename.

    Conventions supported:
      top_s16.tsv           → role=top, league=None (combined), patch=s16
      top_lck_s16.tsv       → role=top, league=lck, patch=s16
      top_lck_26_11.tsv     → role=top, league=lck, patch=26_11
      mid_lpl_s16.tsv       → role=mid, league=lpl, patch=s16
    """
    stem = filename.replace(".tsv", "").lower()
    parts = stem.split("_")
    if not parts:
        return None, None, None
    role = ROLE_NORM.get(parts[0])
    league = None
    patch_parts = []
    for p in parts[1:]:
        if p in LEAGUE_CODES:
            league = p
        else:
            patch_parts.append(p)
    patch = "_".join(patch_parts) if patch_parts else None
    return role, league, patch


def parse_tsv(path: Path, role_hint: str | None = None) -> list[GolGGChampionStatRow]:
    """Parse a per-role gol.gg Champions TSV.

    Args:
        path: TSV file path
        role_hint: filename-derived role (e.g. 'top_s16.tsv' → 'top')

    The format observed has many possible columns; we extract a stable subset.
    """
    if role_hint is None:
        # Try to infer from filename
        m = re.match(r"(top|jungle|mid|bot|support)_", path.stem.lower())
        if m:
            role_hint = m.group(1)
    role = ROLE_NORM.get((role_hint or "").lower(), "top")

    rows: list[GolGGChampionStatRow] = []
    with open(path, "r", encoding="utf-8", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        delim = "\t" if "\t" in sample else ","

        reader = csv.reader(f, delimiter=delim)
        header: Optional[list[str]] = None
        col_idx: dict[str, int] = {}

        for raw in reader:
            if not raw or not any(raw):
                continue

            if header is None:
                header = [c.strip().lower() for c in raw]
                # Find canonical columns
                for i, h in enumerate(header):
                    if "champion" in h and "champion" not in col_idx:
                        col_idx["champion"] = i
                    elif "games" in h or "matches" in h:
                        col_idx.setdefault("n_games", i)
                    elif "winrate" in h or h == "wr":
                        col_idx.setdefault("winrate", i)
                    elif "pickrate" in h or "presence" in h:
                        col_idx.setdefault("pickrate", i)
                    elif "banrate" in h or "ban rate" in h:
                        col_idx.setdefault("banrate", i)
                    elif h == "kda" or "kda" in h:
                        col_idx.setdefault("kda", i)
                    elif "gold @15" in h or "gold@15" in h or "gd@15" in h:
                        col_idx.setdefault("gold_at_15", i)
                    elif "cs @15" in h or "cs@15" in h or "csd@15" in h:
                        col_idx.setdefault("cs_at_15", i)
                    elif "dmg" in h and "min" in h:
                        col_idx.setdefault("dmg_per_min", i)
                continue

            try:
                def _num_opt(idx: Optional[int]) -> Optional[float]:
                    if idx is None or idx >= len(raw):
                        return None
                    s = raw[idx].strip().replace(",", "").replace("%", "")
                    if not s or s == "-":
                        return None
                    try:
                        return float(s)
                    except ValueError:
                        return None

                champ_raw = raw[col_idx.get("champion", 0)].strip() if "champion" in col_idx else raw[0].strip()
                if not champ_raw:
                    continue
                champion = normalize_champion(champ_raw)

                n_games_val = _num_opt(col_idx.get("n_games"))
                if n_games_val is None or n_games_val < 1:
                    continue
                wr_val = _num_opt(col_idx.get("winrate"))
                if wr_val is None:
                    continue

                row = GolGGChampionStatRow(
                    champion=champion,
                    role=role,
                    n_games=int(n_games_val),
                    winrate=wr_val,
                    pickrate=_num_opt(col_idx.get("pickrate")),
                    banrate=_num_opt(col_idx.get("banrate")),
                    kda=_num_opt(col_idx.get("kda")),
                    gold_at_15=_num_opt(col_idx.get("gold_at_15")),
                    cs_at_15=_num_opt(col_idx.get("cs_at_15")),
                    dmg_per_min=_num_opt(col_idx.get("dmg_per_min")),
                )
                rows.append(row)
            except (ValueError, KeyError) as e:
                log.debug("failed to parse row %s: %s", raw[:4], e)
                continue

    return rows


def parse_all_tsvs(input_dir: Path) -> dict[str, dict[str, GolGGChampionStatRow]]:
    """Read all TSVs in input_dir, group by (role, champion).

    When the same (role, champion) appears in multiple files (e.g., season
    vs current-patch), keep the row with the largest sample size.

    Returns: {role: {champion: row}}
    """
    by_role_champ: dict[str, dict[str, GolGGChampionStatRow]] = defaultdict(dict)
    for tsv in sorted(input_dir.glob("*.tsv")):
        rows = parse_tsv(tsv)
        log.info("parsed %d rows from %s", len(rows), tsv.name)
        for r in rows:
            existing = by_role_champ[r.role].get(r.champion)
            if existing is None or r.n_games > existing.n_games:
                by_role_champ[r.role][r.champion] = r
    return dict(by_role_champ)


def parse_per_league_tsvs(input_dir: Path) -> dict[tuple[str, str | None], dict[str, GolGGChampionStatRow]]:
    """Read all TSVs and group by (role, league) → {champion: row}.

    When filename has a league code (top_lck_s16.tsv), the data is
    tracked per-league. Otherwise it's marked as combined (league=None).

    Returns: {(role, league): {champion: largest-N row}}
    """
    by_role_league: dict[tuple[str, str | None], dict[str, GolGGChampionStatRow]] = defaultdict(dict)
    for tsv in sorted(input_dir.glob("*.tsv")):
        role, league, _patch = parse_filename_metadata(tsv.name)
        rows = parse_tsv(tsv, role_hint=role)
        log.info("parsed %d rows from %s (role=%s, league=%s)",
                 len(rows), tsv.name, role, league or "combined")
        for r in rows:
            key = (r.role, league)
            existing = by_role_league[key].get(r.champion)
            if existing is None or r.n_games > existing.n_games:
                by_role_league[key][r.champion] = r
    return dict(by_role_league)


def detect_regional_divergence(
    per_league_data: dict[tuple[str, str | None], dict[str, GolGGChampionStatRow]],
    min_games_per_league: int = 20,
    divergence_threshold_pp: float = 10.0,
) -> list[dict]:
    """Flag champions where regional WRs differ by >= divergence_threshold_pp.

    Example: 'Ashe ADC wins 75% in LEC, 57.5% in LCK' (17.5pp swing).

    Returns: list of {champion, role, regions: {league: (n, wr)}, max_swing_pp}
    sorted by max_swing_pp descending.
    """
    # Index by (role, champion) → {league: row}
    by_champion: dict[tuple[str, str], dict[str | None, GolGGChampionStatRow]] = defaultdict(dict)
    for (role, league), champ_rows in per_league_data.items():
        if league is None:
            continue  # only consider per-league data for divergence
        for champ, row in champ_rows.items():
            by_champion[(role, champ)][league] = row

    flags: list[dict] = []
    for (role, champ), by_league in by_champion.items():
        # Only consider champions with sufficient sample in ≥2 leagues
        big_samples = {lg: r for lg, r in by_league.items()
                       if r.n_games >= min_games_per_league}
        if len(big_samples) < 2:
            continue
        wrs = [(lg, r.n_games, r.winrate) for lg, r in big_samples.items()]
        max_wr = max(w for _, _, w in wrs)
        min_wr = min(w for _, _, w in wrs)
        swing_pp = (max_wr - min_wr) * 100
        if swing_pp >= divergence_threshold_pp:
            flags.append({
                "champion": champ,
                "role": role,
                "regions": {lg: {"n_games": n, "winrate": w} for lg, n, w in wrs},
                "max_swing_pp": swing_pp,
                "issue": f"WR varies {swing_pp:.1f}pp across regions",
                "suggestion": "consider region-specific profile override OR add region-aware feature",
            })

    flags.sort(key=lambda f: -f["max_swing_pp"])
    return flags


def audit_profiles(stats_by_role: dict[str, dict[str, GolGGChampionStatRow]],
                    profiles_path: Path) -> list[dict]:
    """Compare profile qualitative ratings to empirical gol.gg data.

    Returns: list of audit flags sorted by suspected error magnitude.
    Each flag includes: champion, profile values, empirical signals, suggestion.
    """
    with open(profiles_path, "r", encoding="utf-8") as f:
        profiles = json.load(f)

    flags: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()

    for role, champs in stats_by_role.items():
        for champ_name, stat in champs.items():
            # Filter low-sample
            if stat.n_games < MIN_GAMES_FOR_AUDIT:
                continue
            if stat.pickrate is not None and stat.pickrate < MIN_PICKRATE_FOR_AUDIT:
                continue

            profile = profiles.get(champ_name)
            if profile is None:
                flags.append({
                    "champion": champ_name,
                    "role": role,
                    "severity": "missing_profile",
                    "issue": "champion not in profiles",
                    "n_games": stat.n_games,
                    "winrate": stat.winrate,
                    "suggested": "Add profile (Phase 1.4 missed this champion)",
                    "audited_at": now,
                })
                continue

            qual = profile.get("qualitative", {})
            conf = profile.get("confidence", 0.5)

            # Check 1: Empirical winrate vs implied scaling
            # If a champion has +3 scaling_late, we'd expect above-baseline winrate
            # If a champion has -2 scaling_late, we'd expect below-baseline winrate
            late = qual.get("scaling_late", 0)
            wr_delta = stat.winrate - WINRATE_BASELINE

            # Flag if profile claims strong scaling but empirical is mediocre
            if late >= 2 and stat.winrate < WINRATE_BASELINE - WINRATE_DELTA_THRESHOLD/2:
                flags.append({
                    "champion": champ_name,
                    "role": role,
                    "severity": "high" if abs(wr_delta) > WINRATE_DELTA_THRESHOLD else "medium",
                    "issue": f"profile claims scaling_late={late} but empirical WR is {stat.winrate:.1%} ({wr_delta:+.1%} vs baseline)",
                    "profile_scaling_late": late,
                    "profile_confidence": conf,
                    "empirical_winrate": stat.winrate,
                    "n_games": stat.n_games,
                    "suggested": f"consider lowering scaling_late from {late} to {max(0, late-1)}",
                    "audited_at": now,
                })

            # Flag if profile claims weak late but empirical strong
            if late <= 0 and stat.winrate > WINRATE_BASELINE + WINRATE_DELTA_THRESHOLD:
                flags.append({
                    "champion": champ_name,
                    "role": role,
                    "severity": "high" if abs(wr_delta) > WINRATE_DELTA_THRESHOLD else "medium",
                    "issue": f"profile claims scaling_late={late} but empirical WR is {stat.winrate:.1%} ({wr_delta:+.1%} vs baseline)",
                    "profile_scaling_late": late,
                    "profile_confidence": conf,
                    "empirical_winrate": stat.winrate,
                    "n_games": stat.n_games,
                    "suggested": f"consider raising scaling_late from {late} to {min(3, late+1)}",
                    "audited_at": now,
                })

    # Sort: high severity first, then by absolute winrate delta
    severity_order = {"high": 0, "missing_profile": 1, "medium": 2}
    flags.sort(key=lambda f: (
        severity_order.get(f["severity"], 9),
        -abs(f.get("empirical_winrate", 0.5) - WINRATE_BASELINE),
    ))

    return flags


def save_profile_validation(flags: list[dict], path: Path) -> None:
    """Write the audit report as JSON for programmatic review."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "audited_at": datetime.now(timezone.utc).isoformat(),
        "total_flags": len(flags),
        "by_severity": {},
        "flags": flags,
    }
    for f in flags:
        sev = f.get("severity", "unknown")
        payload["by_severity"][sev] = payload["by_severity"].get(sev, 0) + 1
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log.info("wrote %d audit flags to %s", len(flags), path)
