"""Tests for loltrader.livestats.storage."""
from __future__ import annotations

from pathlib import Path

from loltrader.db import connect, migrate
from loltrader.livestats import storage


def _setup(tmp_path: Path):
    db = tmp_path / "test.db"
    conn = connect(db)
    migrate(conn)
    return conn


def _sample_frame(ts: str = "2026-05-24T22:00:00.000Z",
                  state: str = "in_game",
                  blue_gold: int = 1000,
                  red_gold: int = 900) -> dict:
    return {
        "rfc460Timestamp": ts,
        "gameState": state,
        "blueTeam": {
            "totalGold": blue_gold, "totalKills": 0,
            "towers": 0, "inhibitors": 0,
            "dragons": ["infernal"], "barons": 0,
        },
        "redTeam": {
            "totalGold": red_gold, "totalKills": 0,
            "towers": 1, "inhibitors": 0,
            "dragons": [], "barons": 0,
        },
    }


def test_parse_rfc460_to_unix_round_trip() -> None:
    from datetime import datetime, timezone
    ts = "2026-05-24T22:21:29.817Z"
    unix = storage.parse_rfc460_to_unix(ts)
    # Round-trip back: 2026-05-24 22:21:29 UTC
    expected = int(datetime(2026, 5, 24, 22, 21, 29, tzinfo=timezone.utc).timestamp())
    assert unix == expected


def test_register_game_first_seen_inserts(tmp_path: Path) -> None:
    conn = _setup(tmp_path)
    storage.register_game_first_seen(conn, "g1", "lck",
                                     blue_team_code="TLAW", red_team_code="LYON")
    row = storage.get_game_state(conn, "g1")
    assert row is not None
    assert row["game_id"] == "g1"
    assert row["league"] == "lck"
    assert row["blue_team_code"] == "TLAW"
    assert row["red_team_code"] == "LYON"
    assert row["first_seen_ts_unix"] is not None
    assert row["game_start_ts_unix"] is None  # not set yet


def test_register_game_first_seen_idempotent(tmp_path: Path) -> None:
    conn = _setup(tmp_path)
    storage.register_game_first_seen(conn, "g1", "lck")
    first = storage.get_game_state(conn, "g1")
    storage.register_game_first_seen(conn, "g1", "lck")
    second = storage.get_game_state(conn, "g1")
    # first_seen_ts should be preserved
    assert first["first_seen_ts_unix"] == second["first_seen_ts_unix"]


def test_register_game_first_seen_fills_null_fields(tmp_path: Path) -> None:
    """If team code wasn't known on first call, a later call with the code should fill it in."""
    conn = _setup(tmp_path)
    storage.register_game_first_seen(conn, "g1", "lck", blue_team_code=None)
    storage.register_game_first_seen(conn, "g1", "lck", blue_team_code="TLAW")
    row = storage.get_game_state(conn, "g1")
    assert row["blue_team_code"] == "TLAW"


def test_set_game_start_if_unset_sets_once(tmp_path: Path) -> None:
    """Game start should be set exactly once per spec §6.1, §10.5."""
    conn = _setup(tmp_path)
    storage.register_game_first_seen(conn, "g1", "lck")
    # First call returns True (we set it)
    assert storage.set_game_start_if_unset(conn, "g1", 1700000000) is True
    row = storage.get_game_state(conn, "g1")
    assert row["game_start_ts_unix"] == 1700000000
    # Second call returns False and does not overwrite
    assert storage.set_game_start_if_unset(conn, "g1", 1700000999) is False
    row = storage.get_game_state(conn, "g1")
    assert row["game_start_ts_unix"] == 1700000000  # unchanged


def test_write_frame_inserts_and_parses(tmp_path: Path) -> None:
    conn = _setup(tmp_path)
    storage.register_game_first_seen(conn, "g1", "lck")
    inserted = storage.write_frame(conn, "g1", _sample_frame())
    assert inserted is True
    row = conn.execute(
        "SELECT * FROM live_frames WHERE game_id = 'g1'"
    ).fetchone()
    assert row["game_state"] == "in_game"
    assert row["blue_gold"] == 1000
    assert row["red_gold"] == 900
    assert row["red_towers"] == 1


def test_write_frame_dedup_on_same_ts(tmp_path: Path) -> None:
    """Spec §17 #7: writes are deduped on (game_id, frame_ts_unix)."""
    conn = _setup(tmp_path)
    storage.register_game_first_seen(conn, "g1", "lck")
    f = _sample_frame()
    assert storage.write_frame(conn, "g1", f) is True
    # Second write of same frame → dedup
    assert storage.write_frame(conn, "g1", f) is False
    count = conn.execute(
        "SELECT count(*) c FROM live_frames WHERE game_id = 'g1'"
    ).fetchone()["c"]
    assert count == 1


def test_write_frame_sets_game_start_on_first_in_game(tmp_path: Path) -> None:
    conn = _setup(tmp_path)
    storage.register_game_first_seen(conn, "g1", "lck")
    f = _sample_frame(ts="2026-05-24T22:00:00.000Z", state="in_game")
    storage.write_frame(conn, "g1", f)
    row = storage.get_game_state(conn, "g1")
    assert row["game_start_ts_unix"] == storage.parse_rfc460_to_unix(f["rfc460Timestamp"])


def test_write_frame_does_not_set_game_start_on_pre_game(tmp_path: Path) -> None:
    conn = _setup(tmp_path)
    storage.register_game_first_seen(conn, "g1", "lck")
    f = _sample_frame(state="pre_game")
    storage.write_frame(conn, "g1", f)
    row = storage.get_game_state(conn, "g1")
    assert row["game_start_ts_unix"] is None


def test_write_frame_caches_only_earliest_in_game(tmp_path: Path) -> None:
    """If we somehow observe in_game at t1 then again at t2 > t1, the cache
    should preserve t1 (we don't re-set game_start)."""
    conn = _setup(tmp_path)
    storage.register_game_first_seen(conn, "g1", "lck")
    f1 = _sample_frame(ts="2026-05-24T22:00:00.000Z", state="in_game")
    f2 = _sample_frame(ts="2026-05-24T22:00:10.000Z", state="in_game")
    storage.write_frame(conn, "g1", f1)
    storage.write_frame(conn, "g1", f2)
    row = storage.get_game_state(conn, "g1")
    # Should be t1 from f1, not t2 from f2
    assert row["game_start_ts_unix"] == storage.parse_rfc460_to_unix(f1["rfc460Timestamp"])


def test_get_latest_frame_ts(tmp_path: Path) -> None:
    conn = _setup(tmp_path)
    storage.register_game_first_seen(conn, "g1", "lck")
    assert storage.get_latest_frame_ts(conn, "g1") is None
    storage.write_frame(conn, "g1", _sample_frame(ts="2026-05-24T22:00:00.000Z"))
    storage.write_frame(conn, "g1", _sample_frame(ts="2026-05-24T22:00:10.000Z"))
    storage.write_frame(conn, "g1", _sample_frame(ts="2026-05-24T22:00:05.000Z"))  # out of order
    latest = storage.get_latest_frame_ts(conn, "g1")
    assert latest == storage.parse_rfc460_to_unix("2026-05-24T22:00:10.000Z")


def test_set_adaptive_delay(tmp_path: Path) -> None:
    conn = _setup(tmp_path)
    storage.register_game_first_seen(conn, "g1", "lck")
    storage.set_adaptive_delay(conn, "g1", 45)
    row = storage.get_game_state(conn, "g1")
    assert row["api_min_delay_sec"] == 45


def test_set_game_end(tmp_path: Path) -> None:
    conn = _setup(tmp_path)
    storage.register_game_first_seen(conn, "g1", "lck")
    storage.set_game_end(conn, "g1", 1700001000)
    row = storage.get_game_state(conn, "g1")
    assert row["game_end_ts_unix"] == 1700001000
