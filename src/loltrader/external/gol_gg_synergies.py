"""Parse gol.gg Champion Synergy TSV exports + classify into our format.

gol.gg's "Copy table to clipboard" emits tab-separated rows with columns:
  CHAMPION 1, CHAMPION 2, # GAMES, WINRATE, DUO GD@15, DUO CSD@15

We read those, dedup across multiple filter passes (a pair may appear in
pairs_s16_spring.tsv AND pairs_bot_duos.tsv), classify each pair by
early/mid/late game type based on DUO GD@15, and emit feature boosts.

Boost logic:
  - Winrate < MIN_WR_THRESHOLD: skip (not strong enough)
  - n_games < MIN_GAMES: skip (not statistically reliable)
  - GD@15 > GD_EARLY_THRESHOLD: early-game pair → boost scaling_early + engage
  - GD@15 < GD_SCALING_THRESHOLD: late-game pair → boost scaling_late + teamfight
  - Otherwise: mid-game pair → boost scaling_mid + pick_threat

Conservative WR adjustment:
  Raw 81% WR doesn't mean 81% true edge — it's confounded by team strength,
  counter-picks, etc. We adjust toward 50% by ~30% to get an effective edge.
"""
from __future__ import annotations

import csv
import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from loltrader.external.schemas import GolGGSynergyRow, ExpandedSynergy

log = logging.getLogger(__name__)


# Classification thresholds
MIN_WR_THRESHOLD = 0.55             # below this, no boost
MIN_GAMES = 10                       # below this, too noisy
GD_EARLY_THRESHOLD = 300             # +GD@15 above this = early pair
GD_SCALING_THRESHOLD = 100           # +GD@15 below this (with high WR) = scaling
WR_REGRESSION_FACTOR = 0.70          # pull WR 30% toward 50%

# Champion name normalization — gol.gg uses display names ("Lee Sin"),
# our profiles use DataDragon names ("LeeSin").
DDRAGON_NAME_MAP = {
    "Lee Sin": "LeeSin",
    "Jarvan IV": "JarvanIV",
    "Master Yi": "MasterYi",
    "Miss Fortune": "MissFortune",
    "Xin Zhao": "XinZhao",
    "Tahm Kench": "TahmKench",
    "Twisted Fate": "TwistedFate",
    "Dr. Mundo": "DrMundo",
    "Aurelion Sol": "AurelionSol",
    "Cho'Gath": "Chogath",
    "K'Sante": "KSante",
    "Kai'Sa": "Kaisa",
    "Kha'Zix": "Khazix",
    "Kog'Maw": "KogMaw",
    "LeBlanc": "Leblanc",
    "Rek'Sai": "RekSai",
    "Vel'Koz": "Velkoz",
    "Wukong": "MonkeyKing",
    "Renata Glasc": "Renata",
    "Nunu & Willump": "Nunu",
    "Bel'Veth": "Belveth",
}


_ROLE_SUFFIX_RE = re.compile(
    r"\s+(?:TOP|JUNGLE|JGL?|MID|MIDDLE|BOT|BOTTOM|ADC|SUPPORT|SUPP?|SUP)\s*$",
    re.IGNORECASE,
)


def extract_role_suffix(name: str) -> tuple[str, str | None]:
    """If gol.gg appended ' JUNGLE' / ' MID' / etc., split into (champ, role)."""
    s = (name or "").strip()
    m = _ROLE_SUFFIX_RE.search(s)
    if not m:
        return s, None
    role_token = m.group(0).strip().upper()
    role_map = {
        "TOP": "top",
        "JUNGLE": "jungle", "JGL": "jungle", "JG": "jungle",
        "MID": "mid", "MIDDLE": "mid",
        "BOT": "bot", "BOTTOM": "bot", "ADC": "bot",
        "SUPPORT": "support", "SUPP": "support", "SUP": "support",
    }
    return s[:m.start()].strip(), role_map.get(role_token)


def normalize_champion(name: str) -> str:
    """Map gol.gg display name → DataDragon canonical name.

    Handles gol.gg's role suffix convention (' Pantheon JUNGLE') by stripping
    the suffix before lookup. Also normalizes leading/trailing whitespace.
    """
    base, _ = extract_role_suffix(name)
    if base in DDRAGON_NAME_MAP:
        return DDRAGON_NAME_MAP[base]
    # Strip apostrophes + spaces as a fallback
    return re.sub(r"['\s.]", "", base)


def parse_tsv(path: Path) -> list[GolGGSynergyRow]:
    """Read a single TSV file produced by gol.gg's clipboard copy.

    Format observed:
      Tab-separated with header row.
      Columns (in order): champion_1, champion_2, # games, winrate, gd@15, csd@15
      Numbers may be locale-formatted (commas in big numbers).
    """
    rows: list[GolGGSynergyRow] = []
    with open(path, "r", encoding="utf-8", newline="") as f:
        # Detect delimiter: usually tab, but handle CSV in case
        sample = f.read(4096)
        f.seek(0)
        delim = "\t" if "\t" in sample else ","

        reader = csv.reader(f, delimiter=delim)
        header = None
        for raw in reader:
            if not raw or not any(raw):
                continue
            # Skip headers (any row whose first column isn't a champion name)
            if header is None:
                header = [c.strip().lower() for c in raw]
                continue

            # We expect at least 6 columns
            if len(raw) < 6:
                log.debug("skipping short row: %s", raw)
                continue

            try:
                # gol.gg may include extra columns or % signs; clean each
                def _num(s: str) -> float:
                    return float(s.replace(",", "").replace("%", "").strip())

                # Extract role suffix (preserve!) — Sett SUPPORT != Sett TOP
                c1_raw, r1 = extract_role_suffix(raw[0])
                c2_raw, r2 = extract_role_suffix(raw[1])
                c1 = normalize_champion(c1_raw)
                c2 = normalize_champion(c2_raw)
                if not c1 or not c2 or (c1 == c2 and r1 == r2):
                    continue
                row = GolGGSynergyRow(
                    champion_1=c1,
                    role_1=r1,
                    champion_2=c2,
                    role_2=r2,
                    n_games=int(_num(raw[2])),
                    winrate=_num(raw[3]),
                    duo_gd_15=_num(raw[4]),
                    duo_csd_15=_num(raw[5]),
                )
                rows.append(row)
            except (ValueError, IndexError) as e:
                log.debug("failed to parse row %s: %s", raw, e)
                continue

    return rows


def _pair_key(champ_1: str, role_1: str | None,
               champ_2: str, role_2: str | None) -> tuple[str, str, str | None, str, str | None]:
    """Build a sorted (key, c1, r1, c2, r2) tuple.

    Pair key includes roles so Sett:support|Yasuo:mid != Sett:top|Yasuo:mid.
    """
    a = (champ_1, role_1)
    b = (champ_2, role_2)
    # Sort by (champ, role) so dedup is stable
    if (a[0], a[1] or "") > (b[0], b[1] or ""):
        a, b = b, a
    key = f"{a[0]}:{a[1] or '?'}|{b[0]}:{b[1] or '?'}"
    return key, a[0], a[1], b[0], b[1]


def dedup_synergies(all_rows: Iterable[GolGGSynergyRow]) -> dict[str, dict]:
    """Combine multiple file passes that contain the same (champ+role) pair.

    A pair like (Caitlyn:bot, LeeSin:jungle) may appear in:
      - pairs_s16_spring.tsv (general)
      - pairs_bot_duos.tsv (role-specific)
    We sum the games and weight-average the stats.

    Off-meta variants (Sett:support vs Sett:top) stay distinct because
    they're keyed separately.

    Returns: {pair_key: {champion_1, role_1, champion_2, role_2,
                        n_games_total, winrate, gd, csd}}
    """
    aggregator: dict[str, dict] = defaultdict(lambda: {
        "n_games_total": 0,
        "weighted_wr_sum": 0.0,
        "weighted_gd_sum": 0.0,
        "weighted_csd_sum": 0.0,
        "champion_1": "",
        "role_1": None,
        "champion_2": "",
        "role_2": None,
    })

    for row in all_rows:
        key, c1, r1, c2, r2 = _pair_key(
            row.champion_1, row.role_1, row.champion_2, row.role_2,
        )
        agg = aggregator[key]
        agg["champion_1"] = c1
        agg["role_1"] = r1
        agg["champion_2"] = c2
        agg["role_2"] = r2
        # Use n_games as the weight
        agg["n_games_total"] += row.n_games
        agg["weighted_wr_sum"] += row.winrate * row.n_games
        agg["weighted_gd_sum"] += row.duo_gd_15 * row.n_games
        agg["weighted_csd_sum"] += row.duo_csd_15 * row.n_games

    # Finalize weighted averages
    finalized: dict[str, dict] = {}
    for key, agg in aggregator.items():
        n = agg["n_games_total"]
        if n == 0:
            continue
        finalized[key] = {
            "champion_1": agg["champion_1"],
            "role_1": agg["role_1"],
            "champion_2": agg["champion_2"],
            "role_2": agg["role_2"],
            "n_games_total": n,
            "winrate": agg["weighted_wr_sum"] / n,
            "avg_duo_gd_15": agg["weighted_gd_sum"] / n,
            "avg_duo_csd_15": agg["weighted_csd_sum"] / n,
        }
    return finalized


def classify_synergy(agg: dict) -> tuple[str, dict[str, float]]:
    """Classify a synergy + assign boost values.

    Returns: (synergy_type, boost_dict).
    boost_dict keys: scaling_early/mid/late_boost, teamfight/engage/pick_threat_boost.
    """
    wr = agg["winrate"]
    gd = agg["avg_duo_gd_15"]

    # Conservative WR adjustment: pull toward 50%
    effective_wr = 0.5 + (wr - 0.5) * (1 - WR_REGRESSION_FACTOR)
    # Boost magnitude scales with effective edge above 50%
    edge = max(0, effective_wr - 0.5)
    # Map: 5% edge → +1.0 boost; 10% edge → +2.0 boost (capped)
    boost_strength = min(2.0, edge * 20)

    boosts = {k: 0.0 for k in (
        "scaling_early_boost", "scaling_mid_boost", "scaling_late_boost",
        "teamfight_boost", "engage_boost", "pick_threat_boost",
    )}

    if gd > GD_EARLY_THRESHOLD:
        synergy_type = "early_game"
        boosts["scaling_early_boost"] = boost_strength
        boosts["engage_boost"] = boost_strength * 0.5
    elif gd < GD_SCALING_THRESHOLD:
        synergy_type = "late_game"
        boosts["scaling_late_boost"] = boost_strength
        boosts["teamfight_boost"] = boost_strength * 0.5
    else:
        synergy_type = "mid_game"
        boosts["scaling_mid_boost"] = boost_strength
        boosts["pick_threat_boost"] = boost_strength * 0.5

    return synergy_type, boosts


def build_expanded_synergies(tsv_paths: list[Path]) -> list[ExpandedSynergy]:
    """End-to-end: read all TSVs, dedup, classify, return ExpandedSynergy list."""
    all_rows: list[GolGGSynergyRow] = []
    for p in tsv_paths:
        rows = parse_tsv(p)
        log.info("parsed %d rows from %s", len(rows), p.name)
        all_rows.extend(rows)

    log.info("aggregating %d raw rows across %d files", len(all_rows), len(tsv_paths))
    aggregated = dedup_synergies(all_rows)
    log.info("→ %d unique pairs after dedup", len(aggregated))

    now = datetime.now(timezone.utc).isoformat()
    expanded: list[ExpandedSynergy] = []
    for key, agg in aggregated.items():
        # Filter low-WR or low-sample pairs
        if agg["winrate"] < MIN_WR_THRESHOLD:
            continue
        if agg["n_games_total"] < MIN_GAMES:
            continue

        synergy_type, boosts = classify_synergy(agg)
        expanded.append(ExpandedSynergy(
            pair_key=key,
            champion_1=agg["champion_1"],
            role_1=agg["role_1"],
            champion_2=agg["champion_2"],
            role_2=agg["role_2"],
            n_games_total=agg["n_games_total"],
            winrate=agg["winrate"],
            avg_duo_gd_15=agg["avg_duo_gd_15"],
            avg_duo_csd_15=agg["avg_duo_csd_15"],
            synergy_type=synergy_type,
            imported_at=now,
            **boosts,
        ))

    log.info("→ %d synergies passing thresholds (WR>=%.2f, N>=%d)",
             len(expanded), MIN_WR_THRESHOLD, MIN_GAMES)
    return expanded


def save_synergies(expanded: list[ExpandedSynergy], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {s.pair_key: json.loads(s.model_dump_json()) for s in expanded}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log.info("wrote %d synergies to %s", len(expanded), path)
