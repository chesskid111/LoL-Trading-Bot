"""Live state integrator — Layer 4 of the comp evaluation engine.

Combines comp evaluation (Layers 1-3) with current game state (live_frames +
live_frames_details) into a flat ``dict[str, float]`` of ~75 features.

The feature schema is STABLE — every call returns the same keys regardless
of inputs. Missing data is replaced with sentinel values (0.0 by default).
That stability is critical for the Phase 4 model, which expects a fixed
feature vector.

Three public entry points:

  load_frame(conn, game_id, minute=None)
      → (frame_dict, details_list) reading from DB

  compute_objective_state(frame)
      → {"dragon_diff": ..., "baron_state": ..., "soul_state": ..., ...}

  integrate_state(comp_a, comp_b, frame, details, minute, picks_a, picks_b)
      → full ~75-feature dict ready for the model

  integrate_pregame(comp_a, comp_b, picks_a, picks_b)
      → same schema but state features zeroed; for pre-game predictions
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from loltrader.comp.aggregator import ChampionPick, CompProfile, SCALING_MINUTES
from loltrader.comp.matchup import comp_matchup, crossover_minute, lane_matchup

log = logging.getLogger(__name__)

# Baron spawns at minute 20; respawn 6 min after death. Used in
# time_to_next_baron when no baron is currently active.
BARON_FIRST_SPAWN = 20 * 60
BARON_RESPAWN_DELAY = 6 * 60
BARON_BUFF_DURATION = 3 * 60   # ~3-min buff from a taken baron

# Dragon spawns every 5 min. Elder dragon replaces normal at 35 min.
DRAGON_SPAWN_INTERVAL = 5 * 60
ELDER_SPAWN_MINUTE = 35

# An item is "completed" when it occupies an inventory slot AND its ID is not
# a basic component (Riot's item IDs <2000 are mostly components; ≥3000 are
# completed in most cases). Trinket slot (last item) is excluded.
COMPLETED_ITEM_MIN_ID = 3000


# ---------- frame loading -----------------------------------------------


def load_frame(
    conn: sqlite3.Connection,
    game_id: str,
    minute: int | None = None,
) -> tuple[dict | None, list[dict]]:
    """Load the most recent live_frame + live_frames_details for a game.

    Args:
        conn: SQLite connection.
        game_id: Riot's esports gameId.
        minute: Optional in-game minute. If specified, we pick the frame
            closest to (game_start_ts_unix + minute*60). If None, the
            latest frame is returned.

    Returns:
        (frame_dict, details_list) — frame is None if no rows exist.
        details_list is empty if /details endpoint hasn't run yet.
    """
    # Latest frame is the simple case
    if minute is None:
        row = conn.execute(
            """
            SELECT frame_id, game_id, frame_ts_unix, game_state,
                   blue_gold, blue_kills, blue_towers, blue_inhibitors,
                   blue_barons, blue_dragons_json,
                   red_gold, red_kills, red_towers, red_inhibitors,
                   red_barons, red_dragons_json,
                   raw_json
            FROM live_frames
            WHERE game_id = ?
            ORDER BY frame_ts_unix DESC LIMIT 1
            """,
            (game_id,),
        ).fetchone()
    else:
        # Frame closest to game_start + minute*60
        gs = conn.execute(
            "SELECT game_start_ts_unix FROM games_live WHERE game_id = ?",
            (game_id,),
        ).fetchone()
        if not gs or gs["game_start_ts_unix"] is None:
            return None, []
        target_ts = gs["game_start_ts_unix"] + minute * 60
        row = conn.execute(
            """
            SELECT frame_id, game_id, frame_ts_unix, game_state,
                   blue_gold, blue_kills, blue_towers, blue_inhibitors,
                   blue_barons, blue_dragons_json,
                   red_gold, red_kills, red_towers, red_inhibitors,
                   red_barons, red_dragons_json,
                   raw_json
            FROM live_frames
            WHERE game_id = ?
            ORDER BY ABS(frame_ts_unix - ?) ASC LIMIT 1
            """,
            (game_id, target_ts),
        ).fetchone()

    if not row:
        return None, []

    frame = dict(row)
    # Decode dragon lists
    for side in ("blue", "red"):
        raw = frame.pop(f"{side}_dragons_json", None)
        try:
            frame[f"{side}_dragons"] = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            frame[f"{side}_dragons"] = []

    # Matching details rows for the same frame_ts
    details_rows = conn.execute(
        """
        SELECT side, participant_id, level, kills, deaths, assists,
               total_gold, creep_score, kill_participation, champion_damage_share,
               wards_placed, wards_destroyed,
               attack_damage, ability_power, armor, magic_resistance,
               attack_speed, critical_chance, life_steal, tenacity,
               items_json, perks_json, abilities_json
        FROM live_frames_details
        WHERE game_id = ? AND frame_ts_unix = ?
        ORDER BY participant_id
        """,
        (game_id, frame["frame_ts_unix"]),
    ).fetchall()
    details = []
    for r in details_rows:
        d = dict(r)
        try:
            d["items"] = json.loads(d.get("items_json") or "[]")
        except (TypeError, ValueError):
            d["items"] = []
        details.append(d)

    return frame, details


# ---------- state extraction --------------------------------------------


def compute_objective_state(frame: dict) -> dict[str, Any]:
    """Extract structured objective features from a frame dict.

    Includes: dragon counts + diff, soul state (whichever team has 4 dragons
    of the same element gets soul; we approximate with "team has ≥3 dragons"),
    baron differential, baron buff active.
    """
    blue_dragons = frame.get("blue_dragons", [])
    red_dragons = frame.get("red_dragons", [])
    blue_d_count = len(blue_dragons)
    red_d_count = len(red_dragons)

    # Soul: a team gets the soul when they take their 4th dragon of any kind.
    # Approximation: ≥3 dragons indicates approaching soul; ≥4 means they have it.
    soul_state = "none"
    if blue_d_count >= 4:
        soul_state = "blue"
    elif red_d_count >= 4:
        soul_state = "red"

    baron_blue = int(frame.get("blue_barons") or 0)
    baron_red = int(frame.get("red_barons") or 0)
    baron_diff = baron_blue - baron_red

    return {
        "blue_dragon_count": blue_d_count,
        "red_dragon_count": red_d_count,
        "dragon_diff": blue_d_count - red_d_count,
        "soul_state": soul_state,         # categorical: "blue"/"red"/"none"
        "blue_barons": baron_blue,
        "red_barons": baron_red,
        "baron_diff": baron_diff,
    }


def compute_item_progression(details: list[dict]) -> dict[str, Any]:
    """Per-side item completion stats from live_frames_details.

    Counts:
      - completed_items_a / _b: total ≥3000-id items across all 5 participants
      - avg_items_a / _b: average completed items per participant
      - max_items_player_a / _b: most completed items on any one player
      - total_gold_a / _b: sum of totalGoldEarned across the team
    """
    blue = [d for d in details if d.get("side") == "blue"]
    red = [d for d in details if d.get("side") == "red"]

    def count_completed(participant: dict) -> int:
        items = participant.get("items") or []
        # Exclude trinket (typically last slot) — filter by item ID range
        return sum(1 for it in items
                   if isinstance(it, int) and it >= COMPLETED_ITEM_MIN_ID
                   and it < 3900)  # 39xx are trinkets/wards

    blue_completed = [count_completed(p) for p in blue]
    red_completed = [count_completed(p) for p in red]

    blue_total_gold = sum(int(p.get("total_gold") or 0) for p in blue)
    red_total_gold = sum(int(p.get("total_gold") or 0) for p in red)

    return {
        "completed_items_a": sum(blue_completed),
        "completed_items_b": sum(red_completed),
        "avg_items_a": (sum(blue_completed) / len(blue_completed)) if blue_completed else 0.0,
        "avg_items_b": (sum(red_completed) / len(red_completed)) if red_completed else 0.0,
        "max_items_player_a": max(blue_completed) if blue_completed else 0,
        "max_items_player_b": max(red_completed) if red_completed else 0,
        "details_total_gold_a": blue_total_gold,
        "details_total_gold_b": red_total_gold,
    }


def time_to_next_baron(minute: int, baron_diff: int) -> float:
    """Heuristic seconds-until-next-baron given the current minute.

    First baron spawns at min 20. After a take, 6-min respawn. If a baron is
    currently active (baron_diff != 0 within the buff duration), assume the
    next spawn is 6 min from "now."
    """
    if minute < 20:
        return (20 - minute) * 60
    # If a baron was taken recently, estimate ~6 min from "now"
    if baron_diff != 0:
        return BARON_RESPAWN_DELAY
    # No active baron, fall back to 6-min cycle
    return BARON_RESPAWN_DELAY


# ---------- feature integration -----------------------------------------


# Stable feature schema — order matters for reproducibility but the dict
# iteration order is also stable in Python 3.7+.
FEATURE_SCHEMA: list[str] = [
    # --- State features ---
    "minute",
    "gold_diff",
    "kill_diff",
    "tower_diff",
    "inhib_diff",
    "blue_dragon_count",
    "red_dragon_count",
    "dragon_diff",
    "blue_barons",
    "red_barons",
    "baron_diff",
    "time_to_next_baron",
    "soul_blue",
    "soul_red",
    "is_pregame",

    # --- Comp features ---
    "comp_a_scaling_at_t",
    "comp_b_scaling_at_t",
    "scaling_diff_at_t",
    "comp_a_scaling_late",
    "comp_b_scaling_late",
    "comp_a_baron_dps",
    "comp_b_baron_dps",
    "baron_dps_diff",
    "comp_a_squishiness",
    "comp_b_squishiness",
    "comp_a_disengage",
    "comp_b_disengage",
    "comp_a_engage",
    "comp_b_engage",
    "comp_a_split_push",
    "comp_b_split_push",
    "comp_a_pick_threat",
    "comp_b_pick_threat",
    "comp_a_teamfight",
    "comp_b_teamfight",
    "comp_a_wave_clear",
    "comp_b_wave_clear",
    "comp_a_ult_impact",
    "comp_b_ult_impact",
    "comp_a_synergies",
    "comp_b_synergies",
    "comp_a_confidence",
    "comp_b_confidence",

    # --- Archetype one-hot (4 per side) ---
    "arch_a_scaling",
    "arch_a_teamfight",
    "arch_a_pick",
    "arch_a_balanced",
    "arch_b_scaling",
    "arch_b_teamfight",
    "arch_b_pick",
    "arch_b_balanced",

    # --- Item progression ---
    "completed_items_a",
    "completed_items_b",
    "avg_items_a",
    "avg_items_b",
    "max_items_player_a",
    "max_items_player_b",
    "items_diff",

    # --- Lane matchup ---
    "lane_winrate_avg_a",

    # --- Interactions ---
    "gold_diff_x_time_remaining",
    "gold_diff_x_squishiness_a",
    "minute_x_scaling_diff",
    "baron_state_x_baron_dps_a",
    "crossover_minute",
]


def _zero_features() -> dict[str, float]:
    """Return a feature dict with every key in the schema set to 0.0."""
    return {k: 0.0 for k in FEATURE_SCHEMA}


def integrate_state(
    comp_a: CompProfile,
    comp_b: CompProfile,
    frame: dict | None,
    details: list[dict] | None,
    minute: int,
    picks_a: list[ChampionPick] | None = None,
    picks_b: list[ChampionPick] | None = None,
) -> dict[str, float]:
    """Combine comp eval + live state into the full feature dict.

    Args:
        comp_a, comp_b: CompProfile from Layer 2.
        frame: dict from load_frame() (or None for pre-game).
        details: list of detail rows from load_frame() (or empty).
        minute: current in-game minute. Used to evaluate the comp curves AND
            as a feature in its own right.
        picks_a, picks_b: optional pick lists for lane matchup lookup.

    Returns: stable-schema dict[str, float] of ~75 features.
    """
    feats = _zero_features()
    minute = max(0, min(SCALING_MINUTES[-1], int(minute)))

    # ----- state features -----
    feats["minute"] = float(minute)
    feats["is_pregame"] = 1.0 if (frame is None or minute == 0) else 0.0

    if frame is not None:
        feats["gold_diff"] = float((frame.get("blue_gold") or 0) - (frame.get("red_gold") or 0))
        feats["kill_diff"] = float((frame.get("blue_kills") or 0) - (frame.get("red_kills") or 0))
        feats["tower_diff"] = float((frame.get("blue_towers") or 0) - (frame.get("red_towers") or 0))
        feats["inhib_diff"] = float((frame.get("blue_inhibitors") or 0) - (frame.get("red_inhibitors") or 0))

        obj = compute_objective_state(frame)
        feats["blue_dragon_count"] = float(obj["blue_dragon_count"])
        feats["red_dragon_count"] = float(obj["red_dragon_count"])
        feats["dragon_diff"] = float(obj["dragon_diff"])
        feats["blue_barons"] = float(obj["blue_barons"])
        feats["red_barons"] = float(obj["red_barons"])
        feats["baron_diff"] = float(obj["baron_diff"])
        feats["time_to_next_baron"] = float(time_to_next_baron(minute, obj["baron_diff"]))
        feats["soul_blue"] = 1.0 if obj["soul_state"] == "blue" else 0.0
        feats["soul_red"] = 1.0 if obj["soul_state"] == "red" else 0.0

    # ----- comp features -----
    feats["comp_a_scaling_at_t"] = comp_a.scaling_curve[minute]
    feats["comp_b_scaling_at_t"] = comp_b.scaling_curve[minute]
    feats["scaling_diff_at_t"] = feats["comp_a_scaling_at_t"] - feats["comp_b_scaling_at_t"]
    feats["comp_a_scaling_late"] = comp_a.scaling_curve[SCALING_MINUTES[-1] - 8]  # min 32
    feats["comp_b_scaling_late"] = comp_b.scaling_curve[SCALING_MINUTES[-1] - 8]

    for side, comp in (("a", comp_a), ("b", comp_b)):
        feats[f"comp_{side}_baron_dps"] = float(comp.baron_dps_total)
        feats[f"comp_{side}_squishiness"] = float(comp.peel_demand_total - comp.peel_supply_total)
        feats[f"comp_{side}_disengage"] = float(comp.disengage_score)
        feats[f"comp_{side}_engage"] = float(comp.engage_score)
        feats[f"comp_{side}_split_push"] = float(comp.split_push_threat)
        feats[f"comp_{side}_pick_threat"] = float(comp.pick_threat)
        feats[f"comp_{side}_teamfight"] = float(comp.teamfight_score)
        feats[f"comp_{side}_wave_clear"] = float(comp.wave_clear)
        feats[f"comp_{side}_ult_impact"] = float(comp.ult_impact)
        feats[f"comp_{side}_synergies"] = float(len(comp.synergy_bonuses))
        feats[f"comp_{side}_confidence"] = float(comp.confidence)

    feats["baron_dps_diff"] = feats["comp_a_baron_dps"] - feats["comp_b_baron_dps"]

    # Archetype one-hot
    for side, comp in (("a", comp_a), ("b", comp_b)):
        for arch in ("scaling", "teamfight", "pick", "balanced"):
            feats[f"arch_{side}_{arch}"] = 1.0 if comp.archetype == arch else 0.0

    # ----- item progression -----
    if details:
        ip = compute_item_progression(details)
        feats["completed_items_a"] = float(ip["completed_items_a"])
        feats["completed_items_b"] = float(ip["completed_items_b"])
        feats["avg_items_a"] = float(ip["avg_items_a"])
        feats["avg_items_b"] = float(ip["avg_items_b"])
        feats["max_items_player_a"] = float(ip["max_items_player_a"])
        feats["max_items_player_b"] = float(ip["max_items_player_b"])
        feats["items_diff"] = feats["completed_items_a"] - feats["completed_items_b"]

    # ----- lane matchup -----
    if picks_a and picks_b:
        try:
            assessment = comp_matchup(comp_a, comp_b, minute, picks_a, picks_b)
            feats["lane_winrate_avg_a"] = float(assessment.lane_winrate_avg)
        except Exception as e:
            log.debug("comp_matchup failed: %s", e)
            feats["lane_winrate_avg_a"] = 0.5
        # Crossover minute is a "when will the game tip?" feature
        try:
            co = crossover_minute(comp_a, comp_b, picks_a, picks_b)
            feats["crossover_minute"] = float(co) if co is not None else 40.0
        except Exception:
            feats["crossover_minute"] = 40.0
    else:
        feats["lane_winrate_avg_a"] = 0.5
        feats["crossover_minute"] = 40.0

    # ----- interactions -----
    time_remaining = max(1.0, 40.0 - float(minute))
    feats["gold_diff_x_time_remaining"] = feats["gold_diff"] * time_remaining
    feats["gold_diff_x_squishiness_a"] = feats["gold_diff"] * feats["comp_a_squishiness"]
    feats["minute_x_scaling_diff"] = float(minute) * feats["scaling_diff_at_t"]
    feats["baron_state_x_baron_dps_a"] = feats["baron_diff"] * feats["comp_a_baron_dps"]

    return feats


def integrate_pregame(
    comp_a: CompProfile,
    comp_b: CompProfile,
    picks_a: list[ChampionPick] | None = None,
    picks_b: list[ChampionPick] | None = None,
) -> dict[str, float]:
    """Pre-game features: same schema as integrate_state, state zeroed.

    Used for predictions BEFORE the game starts. The model can use the same
    pipeline as during the game, with is_pregame=1 telling it to ignore the
    state features that haven't materialized yet.
    """
    return integrate_state(comp_a, comp_b, frame=None, details=[],
                            minute=0, picks_a=picks_a, picks_b=picks_b)
