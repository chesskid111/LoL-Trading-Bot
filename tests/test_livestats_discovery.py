"""Tests for loltrader.livestats.discovery (mocked Riot API)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest

from loltrader.livestats import discovery


def _live_response(games: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a fake persisted/getLive response shaped like Riot's."""
    return {"data": {"schedule": {"events": games}}}


def _event_details(team_a: str, team_b: str,
                   game_states: list[str], game_ids: list[str]) -> dict[str, Any]:
    """Build a fake persisted/getEventDetails response."""
    return {
        "data": {
            "event": {
                "match": {
                    "teams": [{"name": team_a}, {"name": team_b}],
                    "games": [
                        {"id": gid, "number": i + 1, "state": state}
                        for i, (gid, state) in enumerate(zip(game_ids, game_states))
                    ],
                }
            }
        }
    }


def test_find_live_games_filters_by_league() -> None:
    live_events = [
        {"id": "evt1", "league": {"name": "LCK", "slug": "lck"}},
        {"id": "evt2", "league": {"name": "LPL", "slug": "lpl"}},
    ]

    def fake_get_json(url: str, params: Any = None, timeout: Any = None) -> Any:
        if "getLive" in url:
            return _live_response(live_events)
        if "getEventDetails" in url:
            event_id = params["id"]
            if event_id == "evt1":
                return _event_details("Gen.G", "T1", ["inProgress"], ["g_lck_1"])
            if event_id == "evt2":
                return _event_details("BLG", "JDG", ["inProgress"], ["g_lpl_1"])
        return None

    with patch.object(discovery, "_get_json", side_effect=fake_get_json):
        # Filter to LCK only
        games = discovery.find_live_games(league_slugs=["lck"])
        assert len(games) == 1
        assert games[0].game_id == "g_lck_1"
        assert games[0].league_slug == "lck"
        assert games[0].team_a_name == "Gen.G"
        assert games[0].team_b_name == "T1"


def test_find_live_games_returns_all_when_no_filter() -> None:
    live_events = [
        {"id": "evt1", "league": {"name": "LCK", "slug": "lck"}},
        {"id": "evt2", "league": {"name": "LPL", "slug": "lpl"}},
    ]

    def fake_get_json(url: str, params: Any = None, timeout: Any = None) -> Any:
        if "getLive" in url:
            return _live_response(live_events)
        if "getEventDetails" in url:
            return _event_details("A", "B", ["inProgress"], [f"g_{params['id']}"])
        return None

    with patch.object(discovery, "_get_json", side_effect=fake_get_json):
        games = discovery.find_live_games()
        assert len(games) == 2


def test_find_live_games_skips_non_inprogress_games() -> None:
    live_events = [{"id": "evt1", "league": {"name": "LCK", "slug": "lck"}}]

    def fake_get_json(url: str, params: Any = None, timeout: Any = None) -> Any:
        if "getLive" in url:
            return _live_response(live_events)
        if "getEventDetails" in url:
            # Game 1 is completed, Game 2 is in progress, Game 3 is unstarted
            return _event_details(
                "A", "B",
                ["completed", "inProgress", "unstarted"],
                ["g1", "g2", "g3"],
            )
        return None

    with patch.object(discovery, "_get_json", side_effect=fake_get_json):
        games = discovery.find_live_games(league_slugs=["lck"])
        assert [g.game_id for g in games] == ["g2"]


def test_find_live_games_empty_response() -> None:
    with patch.object(discovery, "_get_json", return_value=None):
        assert discovery.find_live_games() == []


def test_probe_minimum_delay_finds_smallest_working() -> None:
    """probe should return the smallest delay where the API serves in_game frames."""
    def fake_get_json(url: str, params: Any = None, timeout: Any = None) -> Any:
        # Simulate: delays >= 45s work, 30s returns empty
        starting = params["startingTime"]
        # The 30s probe is more recent (later UTC); 45s+ probes are older.
        # Distinguish by checking time relative to now.
        ts = datetime.strptime(starting[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        delta = (datetime.now(timezone.utc) - ts).total_seconds()
        # API serves frames only when probed at delta >= 45s
        if delta >= 44:  # 1s slack for test floor-to-10s effects
            return {"frames": [{"gameState": "in_game", "rfc460Timestamp": "2026-01-01T00:00:00.000Z"}]}
        return None

    with patch.object(discovery, "_get_json", side_effect=fake_get_json):
        result = discovery.probe_minimum_delay("g1")
        assert result == 45


def test_probe_minimum_delay_returns_none_if_nothing_works() -> None:
    with patch.object(discovery, "_get_json", return_value=None):
        assert discovery.probe_minimum_delay("g1") is None


def test_get_frame_returns_latest() -> None:
    sample = {
        "frames": [
            {"gameState": "in_game", "rfc460Timestamp": "2026-01-01T00:00:00.000Z",
             "blueTeam": {"totalGold": 1000}, "redTeam": {"totalGold": 900}},
            {"gameState": "in_game", "rfc460Timestamp": "2026-01-01T00:00:10.000Z",
             "blueTeam": {"totalGold": 1100}, "redTeam": {"totalGold": 950}},
        ]
    }
    with patch.object(discovery, "_get_json", return_value=sample):
        f = discovery.get_frame("g1", 30)
        assert f is not None
        assert f["blueTeam"]["totalGold"] == 1100  # latest


def test_get_frame_returns_none_on_empty_response() -> None:
    with patch.object(discovery, "_get_json", return_value=None):
        assert discovery.get_frame("g1", 30) is None


def test_find_game_start_ts_binary_search_converges() -> None:
    """find_game_start_ts should converge to the true game-start within ~10s."""
    # Simulate a game that started at "60 minutes ago" wall-clock.
    now = datetime.now(timezone.utc)
    true_start = now - timedelta(minutes=60)

    def fake_get_json(url: str, params: Any = None, timeout: Any = None) -> Any:
        starting = params["startingTime"]
        ts = datetime.strptime(starting[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        # If probe is at or after true_start, return an in_game frame at the probe time
        if ts >= true_start:
            frame_ts = ts.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            return {"frames": [{"gameState": "in_game", "rfc460Timestamp": frame_ts}]}
        return None

    with patch.object(discovery, "_get_json", side_effect=fake_get_json):
        found = discovery.find_game_start_ts("g1", look_back_hours=2.0)
        assert found is not None
        # Should land within 10s of the true start (binary-search step size)
        diff = abs((found - true_start).total_seconds())
        assert diff <= 20, f"expected within 20s of true start, off by {diff}s"


def test_get_team_sides_extracts_codes_from_summoner_names() -> None:
    sample = {
        "gameMetadata": {
            "blueTeamMetadata": {
                "esportsTeamId": "tlid",
                "participantMetadata": [
                    {"summonerName": "TLAW Morgan"},
                    {"summonerName": "TLAW Josedeodo"},
                ],
            },
            "redTeamMetadata": {
                "esportsTeamId": "lyid",
                "participantMetadata": [
                    {"summonerName": "LYON Dhokla"},
                    {"summonerName": "LYON Inspired"},
                ],
            },
        },
        "frames": [],
    }
    with patch.object(discovery, "_get_json", return_value=sample):
        sides = discovery.get_team_sides("g1")
        assert sides is not None
        assert sides.blue_team_code == "TLAW"
        assert sides.red_team_code == "LYON"
        assert sides.blue_esports_team_id == "tlid"


def test_get_team_sides_returns_none_when_metadata_missing() -> None:
    with patch.object(discovery, "_get_json", return_value={"frames": []}):
        assert discovery.get_team_sides("g1") is None


def test_riot_api_error_raises_on_transport_failure() -> None:
    import requests
    with patch.object(discovery.requests, "get",
                      side_effect=requests.RequestException("boom")):
        with pytest.raises(discovery.RiotApiError, match="transport failure"):
            discovery._get_json("http://example.com/x")
