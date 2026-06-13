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
                col_idx = _map_columns(header)
                continue

            try:
                def _num_opt(key: str) -> Optional[float]:
                    idx = col_idx.get(key)
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

                # Resolve n_games: prefer explicit #games, else Picks, else Wins+Losses
                n_games_val = _num_opt("n_games")
                if n_games_val is None:
                    n_games_val = _num_opt("picks")
                if n_games_val is None:
                    w = _num_opt("wins")
                    losses = _num_opt("losses")
                    if w is not None and losses is not None:
                        n_games_val = w + losses
                if n_games_val is None or n_games_val < 1:
                    continue

                wr_val = _num_opt("winrate")
                if wr_val is None:
                    # Derive from wins/losses if winrate column missing
                    w = _num_opt("wins")
                    losses = _num_opt("losses")
                    if w is not None and (w + (losses or 0)) > 0:
                        wr_val = 100.0 * w / (w + losses)
                if wr_val is None:
                    continue

                picks_val = _num_opt("picks")
                bans_val = _num_opt("bans")

                row = GolGGChampionStatRow(
                    champion=champion,
                    role=role,
                    n_games=int(n_games_val),
                    winrate=wr_val,
                    picks=int(picks_val) if picks_val is not None else None,
                    bans=int(bans_val) if bans_val is not None else None,
                    prio_score=_num_opt("prio_score"),
                    blind_pick_pct=_num_opt("blind_pick_pct"),
                    avg_ban_time=_num_opt("avg_ban_time"),
                    avg_pick_round=_num_opt("avg_pick_round"),
                    kda=_num_opt("kda"),
                    csm=_num_opt("csm"),
                    dpm=_num_opt("dpm"),
                    gpm=_num_opt("gpm"),
                    avg_game_time_min=_parse_game_time(raw, col_idx.get("game_time")),
                    gd_15=_num_opt("gd_15"),
                    csd_15=_num_opt("csd_15"),
                    xpd_15=_num_opt("xpd_15"),
                    dmg_per_min=_num_opt("dmg_per_min"),
                )
                rows.append(row)
            except (ValueError, KeyError) as e:
                log.debug("failed to parse row %s: %s", raw[:4], e)
                continue

    return rows


def _map_columns(header: list[str]) -> dict[str, int]:
    """Map header strings → canonical column keys, handling both gol.gg formats.

    Order matters: check more-specific patterns before generic ones (e.g.
    'gd@15' before 'dpm', 'prioscore' before 'pickrate').
    """
    col_idx: dict[str, int] = {}
    for i, h in enumerate(header):
        h = h.strip()
        # Champion (first match wins)
        if "champion" in h and "champion" not in col_idx:
            col_idx["champion"] = i
        # Draft signals
        elif h == "picks":
            col_idx.setdefault("picks", i)
        elif h == "bans":
            col_idx.setdefault("bans", i)
        elif "prioscore" in h or "prio score" in h:
            col_idx.setdefault("prio_score", i)
        elif h == "bp%" or "blind" in h:
            col_idx.setdefault("blind_pick_pct", i)
        elif "avg bt" in h or h == "bt":
            col_idx.setdefault("avg_ban_time", i)
        elif "avg rp" in h or h == "rp":
            col_idx.setdefault("avg_pick_round", i)
        # Outcomes
        elif "# games" in h or h == "games" or "matches" in h or "nb games" in h:
            col_idx.setdefault("n_games", i)
        elif h == "wins":
            col_idx.setdefault("wins", i)
        elif h == "losses":
            col_idx.setdefault("losses", i)
        elif "winrate" in h or h == "wr" or "win rate" in h:
            col_idx.setdefault("winrate", i)
        elif h == "kda" or "kda" in h:
            col_idx.setdefault("kda", i)
        # Lane phase diffs — check @15 BEFORE perf metrics
        elif "gd@15" in h or "gd @15" in h:
            col_idx.setdefault("gd_15", i)
        elif "csd@15" in h or "csd @15" in h:
            col_idx.setdefault("csd_15", i)
        elif "xpd@15" in h or "xpd @15" in h:
            col_idx.setdefault("xpd_15", i)
        # Performance
        elif h == "csm" or ("cs" in h and "min" in h):
            col_idx.setdefault("csm", i)
        elif h == "dpm":
            col_idx.setdefault("dpm", i)
        elif h == "gpm":
            col_idx.setdefault("gpm", i)
        elif h == "gt" or "game time" in h:
            col_idx.setdefault("game_time", i)
        elif "dmg" in h and "min" in h:
            col_idx.setdefault("dmg_per_min", i)
        # Legacy
        elif "pickrate" in h or "presence" in h:
            col_idx.setdefault("pickrate", i)
        elif "banrate" in h or "ban rate" in h:
            col_idx.setdefault("banrate", i)
    return col_idx


def _parse_game_time(raw: list[str], idx: Optional[int]) -> Optional[float]:
    """Parse a 'MM:SS' game-time cell into decimal minutes."""
    if idx is None or idx >= len(raw):
        return None
    s = raw[idx].strip()
    if not s or s == "-":
        return None
    if ":" in s:
        try:
            mm, ss = s.split(":")
            return int(mm) + int(ss) / 60.0
        except (ValueError, IndexError):
            return None
    try:
        return float(s)
    except ValueError:
        return None


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


# Bayesian shrinkage for audit winrates — same machinery as synergies.
# A 64% WR over 200 games is trustworthy; over 15 games it's noise. Shrink
# toward 0.5 by sample size before comparing to the profile.
AUDIT_PRIOR_N = 40
# GD@15 thresholds for inferring a champion's game-stage identity.
GD15_EARLY = 200       # strong early lead → early-game champ
GD15_SCALING = -150    # negative lead but wins → scaling champ


def _shrink_wr(wr: float, n: int, prior_n: int = AUDIT_PRIOR_N) -> float:
    """Bayesian-shrink a winrate toward 0.5 by sample size."""
    return (n * wr + prior_n * 0.5) / (n + prior_n)


def audit_profiles(stats_by_role: dict[str, dict[str, GolGGChampionStatRow]],
                    profiles_path: Path) -> list[dict]:
    """Compare profile qualitative ratings to empirical gol.gg data.

    Improvements over the first version:
      - Bayesian shrinkage on WR so small-sample champions aren't over-flagged
      - PrioScore (pick+ban %) as a confidence multiplier — high-priority meta
        picks have been stress-tested, so flags on them are more trustworthy
      - GD@15 cross-check: validates scaling-direction independently of WR
        (strong +GD@15 = early champ; -GD@15 with high WR = scaling champ)

    Returns: list of flags sorted by (severity, confidence-weighted magnitude).
    """
    with open(profiles_path, "r", encoding="utf-8") as f:
        profiles = json.load(f)

    flags: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()

    for role, champs in stats_by_role.items():
        for champ_name, stat in champs.items():
            if stat.n_games < MIN_GAMES_FOR_AUDIT:
                continue
            # Use prio_score (preferred) or pickrate to skip niche picks
            prio = stat.prio_score if stat.prio_score is not None else stat.pickrate
            if prio is not None and prio < MIN_PICKRATE_FOR_AUDIT:
                continue

            profile = profiles.get(champ_name)
            if profile is None:
                flags.append({
                    "champion": champ_name, "role": role,
                    "severity": "missing_profile",
                    "issue": "champion not in profiles",
                    "n_games": stat.n_games, "winrate": stat.winrate,
                    "prio_score": prio,
                    "suggested": "Add profile (bootstrap missed this champion)",
                    "audited_at": now,
                })
                continue

            qual = profile.get("qualitative", {})
            conf = profile.get("confidence", 0.5)
            late = qual.get("scaling_late", 0)
            early = qual.get("scaling_early", 0)

            # Shrink the WR by sample size — this is what we compare on
            wr_shrunk = _shrink_wr(stat.winrate, stat.n_games)
            wr_delta = wr_shrunk - WINRATE_BASELINE

            # Confidence weight: high prio + high sample = trust the flag more.
            # Scales severity, not the threshold.
            prio_weight = min(1.0, (prio or 0.15) / 0.30)   # 30%+ prio = full weight
            sample_weight = min(1.0, stat.n_games / 100.0)
            trust = 0.5 * prio_weight + 0.5 * sample_weight

            def _sev(delta_abs: float) -> str:
                # High only if both the deviation AND our trust in it are high
                if delta_abs > WINRATE_DELTA_THRESHOLD and trust > 0.6:
                    return "high"
                if delta_abs > WINRATE_DELTA_THRESHOLD / 2:
                    return "medium"
                return "low"

            # Check 1: scaling_late claim vs shrunk WR
            if late >= 2 and wr_delta < -WINRATE_DELTA_THRESHOLD / 2:
                flags.append({
                    "champion": champ_name, "role": role,
                    "severity": _sev(abs(wr_delta)),
                    "issue": (f"profile scaling_late={late} but shrunk WR {wr_shrunk:.1%} "
                              f"({wr_delta:+.1%} vs baseline, raw {stat.winrate:.0%} over {stat.n_games})"),
                    "profile_scaling_late": late, "profile_confidence": conf,
                    "empirical_winrate": stat.winrate, "shrunk_winrate": wr_shrunk,
                    "n_games": stat.n_games, "prio_score": prio, "gd_15": stat.gd_15,
                    "suggested": f"consider lowering scaling_late {late}->{max(-3, late-1)}",
                    "audited_at": now,
                })
            if late <= 0 and wr_delta > WINRATE_DELTA_THRESHOLD:
                flags.append({
                    "champion": champ_name, "role": role,
                    "severity": _sev(abs(wr_delta)),
                    "issue": (f"profile scaling_late={late} but shrunk WR {wr_shrunk:.1%} "
                              f"({wr_delta:+.1%} vs baseline, raw {stat.winrate:.0%} over {stat.n_games})"),
                    "profile_scaling_late": late, "profile_confidence": conf,
                    "empirical_winrate": stat.winrate, "shrunk_winrate": wr_shrunk,
                    "n_games": stat.n_games, "prio_score": prio, "gd_15": stat.gd_15,
                    "suggested": f"consider raising scaling_late {late}->{min(3, late+1)}",
                    "audited_at": now,
                })

            # Check 2: GD@15 cross-check on scaling-direction (independent of WR)
            if stat.gd_15 is not None and stat.n_games >= 30:
                # Strong early gold lead but profile says weak early
                if stat.gd_15 > GD15_EARLY and early <= 0:
                    flags.append({
                        "champion": champ_name, "role": role,
                        "severity": "medium",
                        "issue": (f"GD@15={stat.gd_15:+.0f} (strong early) but profile "
                                  f"scaling_early={early}"),
                        "profile_scaling_early": early, "profile_confidence": conf,
                        "gd_15": stat.gd_15, "n_games": stat.n_games, "prio_score": prio,
                        "suggested": f"consider raising scaling_early {early}->{min(3, early+1)}",
                        "audited_at": now,
                    })
                # Negative early lead but wins anyway + profile says weak late
                elif stat.gd_15 < GD15_SCALING and wr_delta > 0 and late <= 0:
                    flags.append({
                        "champion": champ_name, "role": role,
                        "severity": "medium",
                        "issue": (f"GD@15={stat.gd_15:+.0f} (loses lane) yet WR {wr_shrunk:.1%} "
                                  f">baseline — classic scaling profile, but scaling_late={late}"),
                        "profile_scaling_late": late, "profile_confidence": conf,
                        "gd_15": stat.gd_15, "shrunk_winrate": wr_shrunk,
                        "n_games": stat.n_games, "prio_score": prio,
                        "suggested": f"consider raising scaling_late {late}->{min(3, late+1)}",
                        "audited_at": now,
                    })

    severity_order = {"high": 0, "missing_profile": 1, "medium": 2, "low": 3}
    flags.sort(key=lambda f: (
        severity_order.get(f["severity"], 9),
        -abs(f.get("shrunk_winrate", f.get("empirical_winrate", 0.5)) - WINRATE_BASELINE),
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
