"""Live win-prob serving layer for the v3 dashboard.

Wraps ``LiveWinProbModel`` with a stateful per-game cache so we don't
re-fetch picks or re-evaluate comps on every frame.

Public surface:

    WinprobService(model_path).predict(game_id) -> LivePrediction | None

The service is intentionally synchronous and DB-driven — it reads the latest
``live_frames`` + ``live_frames_details`` for a game, plus a cached pick list,
runs the full Phase 3 ``integrate_state`` pipeline, and returns a
``LivePrediction`` ready to push over WS.

Pick resolution mirrors ``loltrader.winprob.dataset``: try the lolesports
gameMetadata endpoint first, fall back to Oracle's match_drafts cross-ref.
Both are cached per game_id so each game costs at most one resolution attempt.

Spec §Phase 5.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time as _time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from loltrader.comp.aggregator import ChampionPick, evaluate_comp
from loltrader.winprob.dataset import (
    _resolve_picks_from_api,
    _resolve_picks_from_oracle,
)
from loltrader.winprob.model import LiveWinProbModel, WinProbPrediction
from loltrader.winprob.state import integrate_state

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LivePrediction:
    """Wire-format prediction for one frame of one game."""
    game_id: str
    minute: int
    p_blue: float
    p10: float
    p90: float
    band_width: float
    raw_p_blue: float
    league: str | None
    blue_team_code: str | None
    red_team_code: str | None
    model_version: str | None     # which model produced this (timestamp)
    has_full_features: bool       # False if picks couldn't be resolved
    risk: dict | None = None      # position-agnostic risk badge (leverage/triggers)


class _GameState:
    """Per-game cached state: picks + comp profiles + last prediction."""
    __slots__ = ("blue_picks", "red_picks", "blue_comp", "red_comp",
                 "league", "blue_team_code", "red_team_code", "resolved")

    def __init__(self) -> None:
        self.blue_picks: list[ChampionPick] | None = None
        self.red_picks: list[ChampionPick] | None = None
        self.blue_comp = None
        self.red_comp = None
        self.league: str | None = None
        self.blue_team_code: str | None = None
        self.red_team_code: str | None = None
        self.resolved = False     # have we attempted pick resolution yet?


class WinprobService:
    """Stateful live-prediction service backed by ``LiveWinProbModel``."""

    def __init__(self, model_path: str | Path = "models/winprob_latest.pkl",
                 profiles_path: str | Path = "data/champion_profiles.json") -> None:
        self.model_path = Path(model_path)
        self.profiles_path = Path(profiles_path)
        self._model: LiveWinProbModel | None = None
        self._model_version: str | None = None
        self._games: dict[str, _GameState] = {}

    def load(self) -> bool:
        """Load the model from disk. Returns True on success, False otherwise."""
        if not self.model_path.exists():
            log.warning("winprob model not found at %s — service will return None",
                        self.model_path)
            return False
        try:
            self._model = LiveWinProbModel.load(self.model_path)
            self._model_version = self._model.metadata.get("trained_at", "unknown")
            log.info("loaded winprob model (trained_at=%s, n_train=%d, brier=%.4f)",
                     self._model_version,
                     self._model.metadata.get("n_train", 0),
                     self._model.metadata.get("brier", float("nan")))
            return True
        except Exception:
            log.exception("failed to load winprob model from %s", self.model_path)
            self._model = None
            return False

    @property
    def is_ready(self) -> bool:
        return self._model is not None

    def _ensure_game_state(self, conn: sqlite3.Connection, game_id: str) -> _GameState:
        """Resolve picks + evaluate comps for ``game_id``. Cached per-game.

        Returns a _GameState even if resolution failed (so we don't retry
        every frame on a broken game).
        """
        st = self._games.get(game_id)
        if st is not None and st.resolved:
            return st
        if st is None:
            st = _GameState()
            self._games[game_id] = st

        g = conn.execute(
            """SELECT league, blue_team_code, red_team_code, game_start_ts_unix
               FROM games_live WHERE game_id = ?""",
            (game_id,),
        ).fetchone()
        if not g:
            st.resolved = True
            return st

        st.league = (g["league"] or "other").lower() if g["league"] else None
        st.blue_team_code = g["blue_team_code"]
        st.red_team_code = g["red_team_code"]
        start = g["game_start_ts_unix"]

        # Try API first, then Oracle
        picks = None
        if start:
            picks = _resolve_picks_from_api(game_id, int(start))
        if not picks and start:
            picks = _resolve_picks_from_oracle(
                conn, game_id, st.blue_team_code, st.red_team_code, int(start)
            )
        if picks:
            st.blue_picks, st.red_picks = picks
            try:
                st.blue_comp = evaluate_comp(st.blue_picks, profiles_path=self.profiles_path)
                st.red_comp = evaluate_comp(st.red_picks, profiles_path=self.profiles_path)
            except Exception:
                log.exception("comp eval failed for %s", game_id)
                st.blue_comp = None
                st.red_comp = None

        st.resolved = True
        return st

    def _load_frame_and_details(
        self, conn: sqlite3.Connection, game_id: str
    ) -> tuple[dict | None, list[dict], dict | None]:
        """Load the latest in_game frame, its details, and a frame ~60s prior
        (for momentum features)."""
        row = conn.execute(
            """SELECT frame_ts_unix, game_state,
                      blue_gold, blue_kills, blue_towers, blue_inhibitors,
                      blue_barons, blue_dragons_json,
                      red_gold, red_kills, red_towers, red_inhibitors,
                      red_barons, red_dragons_json
               FROM live_frames
               WHERE game_id = ? AND game_state = 'in_game'
               ORDER BY frame_ts_unix DESC LIMIT 1""",
            (game_id,),
        ).fetchone()
        if not row:
            return None, [], None

        frame = dict(row)
        for side in ("blue", "red"):
            raw = frame.pop(f"{side}_dragons_json", None)
            try:
                frame[f"{side}_dragons"] = json.loads(raw) if raw else []
            except (TypeError, ValueError):
                frame[f"{side}_dragons"] = []

        ts = int(frame["frame_ts_unix"])

        # ~60s earlier
        prev = conn.execute(
            """SELECT frame_ts_unix, game_state,
                      blue_gold, blue_kills, red_gold, red_kills,
                      blue_dragons_json, red_dragons_json
               FROM live_frames
               WHERE game_id = ? AND game_state = 'in_game'
                     AND frame_ts_unix <= ?
               ORDER BY frame_ts_unix DESC LIMIT 1""",
            (game_id, ts - 60),
        ).fetchone()
        prev_dict = dict(prev) if prev else None
        if prev_dict:
            for side in ("blue", "red"):
                raw = prev_dict.pop(f"{side}_dragons_json", None)
                try:
                    prev_dict[f"{side}_dragons"] = json.loads(raw) if raw else []
                except (TypeError, ValueError):
                    prev_dict[f"{side}_dragons"] = []

        # Details for the latest frame
        det_rows = conn.execute(
            """SELECT side, participant_id, level, kills, deaths, assists,
                      total_gold, creep_score, kill_participation, champion_damage_share,
                      wards_placed, wards_destroyed,
                      attack_damage, ability_power, armor, magic_resistance,
                      attack_speed, critical_chance, life_steal, tenacity,
                      items_json
               FROM live_frames_details
               WHERE game_id = ? AND frame_ts_unix = ?""",
            (game_id, ts),
        ).fetchall()
        details = [dict(r) for r in det_rows]
        for d in details:
            try:
                d["items"] = json.loads(d.get("items_json") or "[]")
            except (TypeError, ValueError):
                d["items"] = []

        return frame, details, prev_dict

    def predict(self, conn: sqlite3.Connection, game_id: str) -> LivePrediction | None:
        """Compute the live win-prob for the most recent frame of ``game_id``."""
        if not self.is_ready:
            return None

        st = self._ensure_game_state(conn, game_id)

        frame, details, prev_frame = self._load_frame_and_details(conn, game_id)
        if not frame:
            return None  # no in_game frames yet

        # Convert frame_ts → minute since game start
        gs_row = conn.execute(
            "SELECT game_start_ts_unix FROM games_live WHERE game_id = ?",
            (game_id,),
        ).fetchone()
        if not gs_row or gs_row["game_start_ts_unix"] is None:
            return None
        minute = max(0, int((int(frame["frame_ts_unix"]) - int(gs_row["game_start_ts_unix"])) // 60))

        # If we have comps, run full integrate_state. Otherwise, build features
        # with zero comp contribution (model handles missing values).
        if st.blue_comp is not None and st.red_comp is not None:
            features = integrate_state(
                st.blue_comp, st.red_comp, frame, details, minute,
                st.blue_picks, st.red_picks,
                league=st.league, prev_frame=prev_frame,
            )
            has_full = True
        else:
            # Skeleton features: just state + league. The model's calibrated
            # prediction will be wider but not nonsensical.
            from loltrader.winprob.state import FEATURE_SCHEMA
            features = {k: 0.0 for k in FEATURE_SCHEMA}
            features["minute"] = float(minute)
            features["gold_diff"] = float((frame.get("blue_gold") or 0) - (frame.get("red_gold") or 0))
            features["kill_diff"] = float((frame.get("blue_kills") or 0) - (frame.get("red_kills") or 0))
            features["tower_diff"] = float((frame.get("blue_towers") or 0) - (frame.get("red_towers") or 0))
            features["inhib_diff"] = float((frame.get("blue_inhibitors") or 0) - (frame.get("red_inhibitors") or 0))
            if st.league:
                key = f"league_{st.league.replace('-', '_')}"
                if key in features:
                    features[key] = 1.0
                else:
                    features["league_other"] = 1.0
            has_full = False

        try:
            pred = self._model.predict(features)  # type: ignore[union-attr]
        except Exception:
            log.exception("model.predict failed for %s", game_id)
            return None

        # Position-agnostic risk badge (leverage / coinflip-zone / triggers).
        # Computed from the same integrated features so the dashboard can show
        # the exit-discipline alert without the user entering a position.
        try:
            from loltrader.trader.exits import risk_signals
            rs = risk_signals(float(pred.p_blue), features)
            risk = {
                "leverage": rs.leverage,
                "coinflip_zone": rs.coinflip_zone,
                "triggers_blue": rs.triggers_blue,
                "triggers_red": rs.triggers_red,
                "headline": rs.headline,
            }
        except Exception:
            log.exception("risk_signals failed for %s", game_id)
            risk = None

        return LivePrediction(
            game_id=game_id,
            minute=minute,
            p_blue=float(pred.p_blue),
            p10=float(pred.p10),
            p90=float(pred.p90),
            band_width=float(pred.band_width),
            raw_p_blue=float(pred.raw_p_blue),
            league=st.league,
            blue_team_code=st.blue_team_code,
            red_team_code=st.red_team_code,
            model_version=self._model_version,
            has_full_features=has_full,
            risk=risk,
        )

    def to_wire(self, pred: LivePrediction) -> dict[str, Any]:
        """Serialize a LivePrediction for WS / JSON output."""
        return asdict(pred)

    def draft_breakdown(self, conn: sqlite3.Connection, game_id: str) -> dict | None:
        """Plain-English draft read for ``game_id`` (picks + comps + dynamics).

        Static for the game (computed from the locked draft), so the dashboard
        can fetch it once. Returns None if picks/comps couldn't be resolved.
        """
        st = self._ensure_game_state(conn, game_id)
        if st.blue_comp is None or st.red_comp is None or not st.blue_picks:
            return None
        from loltrader.comp.draft_read import build_draft_read
        from dataclasses import asdict as _asdict
        read = build_draft_read(
            st.blue_comp, st.red_comp, st.blue_picks, st.red_picks,
            blue_team=st.blue_team_code, red_team=st.red_team_code,
        )
        # DraftSide dataclasses -> dicts for JSON
        read["blue"] = _asdict(read["blue"])
        read["red"] = _asdict(read["red"])
        read["game_id"] = game_id
        read["league"] = st.league
        return read
