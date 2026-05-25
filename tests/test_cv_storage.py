"""Tests for loltrader.cv.storage."""
from __future__ import annotations

from pathlib import Path

from loltrader.cv.storage import (
    CvFrameRecord,
    cv_frames_dir,
    save_frame_png,
    write_cv_frame,
    write_cv_validation,
)
from loltrader.db import connect, migrate
from loltrader.livestats import storage as ls_storage


def _setup(tmp_path: Path):
    db = tmp_path / "test.db"
    conn = connect(db)
    migrate(conn)
    return conn


def test_write_cv_frame_inserts(tmp_path: Path) -> None:
    conn = _setup(tmp_path)
    ls_storage.register_game_first_seen(conn, "g1", "lck")
    rec = CvFrameRecord(
        game_id="g1", frame_ts_unix=1700000000,
        classifier_class="in_game", classifier_confidence=0.82,
        ocr_gold_blue=10000, ocr_gold_red=9500,
    )
    assert write_cv_frame(conn, rec) is True
    row = conn.execute(
        "SELECT * FROM cv_frames WHERE game_id='g1'"
    ).fetchone()
    assert row["classifier_class"] == "in_game"
    assert abs(row["classifier_confidence"] - 0.82) < 1e-9
    assert row["ocr_gold_blue"] == 10000


def test_write_cv_frame_dedup(tmp_path: Path) -> None:
    """Spec §17 #7: dedup on (game_id, frame_ts_unix)."""
    conn = _setup(tmp_path)
    ls_storage.register_game_first_seen(conn, "g1", "lck")
    rec = CvFrameRecord(game_id="g1", frame_ts_unix=1700000000,
                        classifier_class="in_game", classifier_confidence=0.5)
    assert write_cv_frame(conn, rec) is True
    assert write_cv_frame(conn, rec) is False
    count = conn.execute(
        "SELECT count(*) c FROM cv_frames WHERE game_id='g1'"
    ).fetchone()["c"]
    assert count == 1


def test_write_cv_frame_json_serializes_minimap_dots(tmp_path: Path) -> None:
    conn = _setup(tmp_path)
    ls_storage.register_game_first_seen(conn, "g1", "lck")
    rec = CvFrameRecord(
        game_id="g1", frame_ts_unix=1700000000,
        classifier_class="in_game", classifier_confidence=0.7,
        minimap_dots=[
            {"team": "blue", "champion": "Caitlyn", "x": 0.7, "y": 0.7},
            {"team": "red", "champion": "Jayce", "x": 0.3, "y": 0.3},
        ],
        items={"1": [3031, 6672]},
    )
    write_cv_frame(conn, rec)
    row = conn.execute("SELECT minimap_dots_json, items_json FROM cv_frames").fetchone()
    import json
    dots = json.loads(row["minimap_dots_json"])
    assert len(dots) == 2
    assert dots[0]["champion"] == "Caitlyn"
    items = json.loads(row["items_json"])
    assert items["1"] == [3031, 6672]


def test_save_frame_png(tmp_path: Path) -> None:
    # 1×1 black PNG
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108020000"
        "00907753de0000000c4944415478da636060000000000005000150"
        "0d0a2db4000000004945" + "4e44ae426082"
    )
    project_root = tmp_path
    rel_path = save_frame_png(png_bytes, "g1", 1700000000, project_root)
    assert rel_path.endswith("1700000000.png")
    full = project_root / rel_path
    assert full.exists()
    assert full.read_bytes() == png_bytes


def test_cv_frames_dir(tmp_path: Path) -> None:
    d = cv_frames_dir("g1", tmp_path)
    assert d == tmp_path / "data" / "cv_frames" / "g1"


def test_write_cv_validation(tmp_path: Path) -> None:
    conn = _setup(tmp_path)
    ls_storage.register_game_first_seen(conn, "g1", "lck")
    # Write a cv_frame to FK against
    rec = CvFrameRecord(game_id="g1", frame_ts_unix=1700000000,
                        classifier_class="in_game", classifier_confidence=0.7)
    write_cv_frame(conn, rec)
    cv_id = conn.execute("SELECT cv_frame_id FROM cv_frames LIMIT 1").fetchone()["cv_frame_id"]

    write_cv_validation(
        conn, cv_frame_id=cv_id, live_frame_id=None,
        ts_offset_sec=2.5,
        gold_diff_blue=100, gold_diff_red=-50,
        gold_pct_diff_blue=0.01, gold_pct_diff_red=0.005,
        kills_diff_blue=0, kills_diff_red=0,
        flagged=False,
    )
    row = conn.execute("SELECT * FROM cv_validation").fetchone()
    assert row["flagged"] == 0
    assert row["cv_frame_id"] == cv_id
