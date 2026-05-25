"""Riot livestats API: live-game discovery and frame retrieval.

All endpoints use the widely-known public API key. The persisted endpoints
require the x-api-key header; the livestats endpoints don't. We pass it on
both for safety.

Spec §6.1 (Riot livestats), §10 (empirical findings).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

# Public Riot Esports API key (per andydanger/live-lol-esports, etc.).
# Not a secret — Riot publishes this for the lolesports.com website.
RIOT_API_KEY = "0TvQnueqKa5mxJntVWt0w4LpLfEkrV1Ta8rQBb9Z"

PERSISTED = "https://esports-api.lolesports.com/persisted/gw"
LIVE = "https://feed.lolesports.com/livestats/v1"

# Default request timeout. Riot's API is usually <1s; 5s is generous.
DEFAULT_TIMEOUT = 5

# Adaptive-delay safety rails (spec §6.1).
MIN_DELAY_SEC = 30
MAX_DELAY_SEC = 600


class RiotApiError(RuntimeError):
    """Raised when the Riot API returns a non-200 or malformed response we
    can't recover from at this layer."""


@dataclass(frozen=True)
class LiveGame:
    """A live pro LoL game discovered via persisted/getLive."""

    game_id: str
    league: str
    league_slug: str  # lower-case, e.g. "lck"
    team_a_name: str
    team_b_name: str
    game_number: int  # series game (1, 2, 3, ...)


def _floor_to_10s(d: datetime) -> datetime:
    return d.replace(microsecond=0, second=d.second - (d.second % 10))


def _fmt_ts(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _get_json(url: str, params: dict[str, Any] | None = None,
              timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any] | None:
    """Light wrapper around requests.get returning parsed JSON or None on failure.

    Returns None for 200-with-empty-body (the livestats `window` endpoint does
    this when startingTime is outside the serving window). Raises RiotApiError
    on connection / non-200 errors so callers can distinguish "no data" from
    "transport broken".
    """
    headers = {"x-api-key": RIOT_API_KEY}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        raise RiotApiError(f"transport failure for {url}: {e}") from e
    if r.status_code != 200:
        raise RiotApiError(f"{url} returned HTTP {r.status_code}: {r.text[:200]}")
    if not r.text.strip():
        return None
    try:
        return r.json()
    except ValueError as e:
        raise RiotApiError(f"{url} returned non-JSON: {r.text[:200]}") from e


def find_live_games(league_slugs: list[str] | None = None) -> list[LiveGame]:
    """Return all currently-live pro LoL games, optionally filtered by league.

    Args:
        league_slugs: Lower-case league codes to keep (e.g. ["lck"]). If None,
            returns every live game worldwide.

    Spec §2: v2.0 targets LCK only. Pass league_slugs=["lck"] in production.
    """
    data = _get_json(f"{PERSISTED}/getLive", params={"hl": "en-US"})
    if not data:
        return []
    events = data.get("data", {}).get("schedule", {}).get("events", []) or []
    out: list[LiveGame] = []
    for e in events:
        league_name = e.get("league", {}).get("name", "")
        league_slug = (e.get("league", {}).get("slug")
                       or league_name.lower().replace(" ", ""))
        if league_slugs is not None and league_slug not in league_slugs:
            continue
        # Hydrate event details to discover individual games (a series is one
        # event but contains multiple games).
        details = _get_json(
            f"{PERSISTED}/getEventDetails",
            params={"hl": "en-US", "id": e["id"]},
        )
        if not details:
            continue
        match = details.get("data", {}).get("event", {}).get("match", {})
        teams = match.get("teams", [])
        team_names = [t.get("name", "?") for t in teams]
        if len(team_names) < 2:
            continue
        for g in match.get("games", []):
            if g.get("state") != "inProgress":
                continue
            out.append(LiveGame(
                game_id=str(g["id"]),
                league=league_name,
                league_slug=league_slug,
                team_a_name=team_names[0],
                team_b_name=team_names[1],
                game_number=int(g.get("number", 0)),
            ))
    return out


def get_frame(game_id: str, delay_sec: int) -> dict[str, Any] | None:
    """Return the most-recent available livestats frame at the given delay.

    Returns None if no frame is available (game not yet started, or delay
    too aggressive). Raises RiotApiError on transport failure.
    """
    d = _floor_to_10s(datetime.now(timezone.utc) - timedelta(seconds=delay_sec))
    data = _get_json(f"{LIVE}/window/{game_id}", params={"startingTime": _fmt_ts(d)})
    if not data:
        return None
    frames = data.get("frames") or []
    return frames[-1] if frames else None


def get_window(game_id: str, starting_time_utc: datetime) -> dict[str, Any] | None:
    """Raw window-endpoint fetch (returns the full response). For internal use."""
    return _get_json(
        f"{LIVE}/window/{game_id}",
        params={"startingTime": _fmt_ts(_floor_to_10s(starting_time_utc))},
    )


def probe_minimum_delay(game_id: str) -> int | None:
    """Probe the smallest delay that returns at least one in_game frame.

    Walks through standard candidates [30, 45, 60, 75, 90, 120, 180, 300, 600].
    Returns the smallest that works; None if even 600s returns nothing
    (meaning the game probably hasn't actually started yet, despite the
    persisted/getLive endpoint saying it has).
    """
    for delay in [MIN_DELAY_SEC, 45, 60, 75, 90, 120, 180, 300, MAX_DELAY_SEC]:
        d = datetime.now(timezone.utc) - timedelta(seconds=delay)
        try:
            data = _get_json(
                f"{LIVE}/window/{game_id}",
                params={"startingTime": _fmt_ts(_floor_to_10s(d))},
                timeout=DEFAULT_TIMEOUT,
            )
        except RiotApiError:
            continue
        if not data:
            continue
        frames = data.get("frames") or []
        if any(f.get("gameState") == "in_game" for f in frames):
            return delay
    return None


def find_game_start_ts(game_id: str,
                       look_back_hours: float = 2.0) -> datetime | None:
    """Binary-search backwards for the first in_game frame's wall-clock ts.

    Use this ONLY when boot up encounters a game already in progress (we
    missed the pre_game→in_game transition). For games we observe from the
    start, the caller should cache the first observed in_game frame's
    rfc460Timestamp directly — see spec §6.1, §10.5.

    Algorithm: binary-search the time axis between (now - look_back_hours)
    and (now - 60s). Each probe asks the window endpoint at that startingTime;
    if it returns in_game frames, the game had started by then; otherwise it
    hadn't (or data is gone from the serving window). Converges in ~10 calls.

    Returns the earliest observed in_game timestamp, or None if not found.
    """
    now = datetime.now(timezone.utc)
    lo = _floor_to_10s(now - timedelta(hours=look_back_hours))
    hi = _floor_to_10s(now - timedelta(seconds=60))
    earliest: datetime | None = None

    while (hi - lo).total_seconds() > 10:
        mid = lo + (hi - lo) / 2
        mid = _floor_to_10s(mid)
        try:
            data = _get_json(f"{LIVE}/window/{game_id}",
                             params={"startingTime": _fmt_ts(mid)})
        except RiotApiError:
            # Treat transport failure as "no data here" and search higher
            lo = mid
            continue
        if not data:
            lo = mid
            continue
        frames = data.get("frames") or []
        first_in_game_ts: datetime | None = None
        for f in frames:
            if f.get("gameState") == "in_game":
                ts_str = f.get("rfc460Timestamp", "")
                if not ts_str:
                    continue
                try:
                    ft = datetime.strptime(
                        ts_str[:19], "%Y-%m-%dT%H:%M:%S",
                    ).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                first_in_game_ts = ft
                break
        if first_in_game_ts is not None:
            if earliest is None or first_in_game_ts < earliest:
                earliest = first_in_game_ts
            hi = mid
        else:
            lo = mid

    return earliest


@dataclass(frozen=True)
class TeamSides:
    """Which team is on blue side vs red side for a given gameId."""

    blue_team_code: str  # e.g. "TLAW", derived from participant summonerName prefix
    red_team_code: str
    blue_esports_team_id: str
    red_esports_team_id: str


def get_team_sides(game_id: str) -> TeamSides | None:
    """Look up which team is on each side. Returns None if metadata missing.

    Reads gameMetadata from the latest livestats window frame and derives team
    codes from participant summonerName prefixes (e.g. "TLAW Morgan").
    """
    d = _floor_to_10s(datetime.now(timezone.utc) - timedelta(seconds=60))
    data = _get_json(f"{LIVE}/window/{game_id}", params={"startingTime": _fmt_ts(d)})
    if not data:
        return None
    gm = data.get("gameMetadata", {})

    def _code_from_metadata(side: dict[str, Any]) -> str:
        parts = side.get("participantMetadata", []) or []
        if parts:
            name = parts[0].get("summonerName", "")
            if " " in name:
                return name.split(" ", 1)[0]
        return ""

    blue = gm.get("blueTeamMetadata", {}) or {}
    red = gm.get("redTeamMetadata", {}) or {}
    blue_code = _code_from_metadata(blue)
    red_code = _code_from_metadata(red)
    if not blue_code or not red_code:
        return None
    return TeamSides(
        blue_team_code=blue_code,
        red_team_code=red_code,
        blue_esports_team_id=str(blue.get("esportsTeamId", "")),
        red_esports_team_id=str(red.get("esportsTeamId", "")),
    )
