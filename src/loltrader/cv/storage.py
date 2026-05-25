"""SQLite writes for the CV pipeline: cv_frames + cv_validation.

Spec §6.2 (CV pipeline schema), §17 #7 (dedup on game_id + frame_ts).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class CvFrameRecord:
    """One row's worth of CV output. Most fields are nullable because they
    only apply to in_game frames. The classifier-related fields are always
    populated.
    """
    game_id: str
    frame_ts_unix: int

    # Classifier output (always present)
    classifier_class: str             # in_game | studio | replay | ads | unknown
    classifier_confidence: float

    # OCR results (only populated when classifier_class == 'in_game')
    ocr_gold_blue: int | None = None
    ocr_gold_red: int | None = None
    ocr_kills_blue: int | None = None
    ocr_kills_red: int | None = None
    ocr_towers_blue: int | None = None
    ocr_towers_red: int | None = None
    ocr_dragons_blue: int | None = None
    ocr_dragons_red: int | None = None
    ocr_barons_blue: int | None = None
    ocr_barons_red: int | None = None
    ocr_timer_seconds: int | None = None

    # Positional / qualitative outputs (JSON-encoded)
    minimap_dots: list | None = None  # [{"team":..., "champion":..., "x":..., "y":...}, ...]
    items: dict | None = None         # {participant_id: [item_ids]}

    # PNG path on disk, NULL if not retained
    frame_png_path: str | None = None


def write_cv_frame(conn: sqlite3.Connection, record: CvFrameRecord) -> bool:
    """Insert a CV frame row. Dedup on (game_id, frame_ts_unix).

    Returns True if a new row was inserted, False if it was a duplicate.
    """
    cursor = conn.execute(
        """
        INSERT INTO cv_frames (
            game_id, frame_ts_unix, classifier_class, classifier_confidence,
            ocr_gold_blue, ocr_gold_red, ocr_kills_blue, ocr_kills_red,
            ocr_towers_blue, ocr_towers_red,
            ocr_dragons_blue, ocr_dragons_red,
            ocr_barons_blue, ocr_barons_red,
            ocr_timer_seconds,
            minimap_dots_json, items_json, frame_png_path
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(game_id, frame_ts_unix) DO NOTHING
        """,
        (
            record.game_id, record.frame_ts_unix,
            record.classifier_class, record.classifier_confidence,
            record.ocr_gold_blue, record.ocr_gold_red,
            record.ocr_kills_blue, record.ocr_kills_red,
            record.ocr_towers_blue, record.ocr_towers_red,
            record.ocr_dragons_blue, record.ocr_dragons_red,
            record.ocr_barons_blue, record.ocr_barons_red,
            record.ocr_timer_seconds,
            json.dumps(record.minimap_dots) if record.minimap_dots is not None else None,
            json.dumps(record.items) if record.items is not None else None,
            record.frame_png_path,
        ),
    )
    conn.commit()
    return cursor.rowcount > 0


def cv_frames_dir(game_id: str, project_root: Path) -> Path:
    """Return the per-game directory for retained PNG snapshots."""
    return project_root / "data" / "cv_frames" / game_id


def save_frame_png(image_bytes: bytes, game_id: str, frame_ts_unix: int,
                   project_root: Path) -> str:
    """Save a raw PNG bytestring to disk and return the relative path.

    Path convention: data/cv_frames/{game_id}/{frame_ts_unix}.png
    """
    out_dir = cv_frames_dir(game_id, project_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{frame_ts_unix}.png"
    out_path.write_bytes(image_bytes)
    return str(out_path.relative_to(project_root))


def write_cv_validation(
    conn: sqlite3.Connection,
    cv_frame_id: int,
    live_frame_id: int | None,
    ts_offset_sec: float,
    gold_diff_blue: int | None,
    gold_diff_red: int | None,
    gold_pct_diff_blue: float | None,
    gold_pct_diff_red: float | None,
    kills_diff_blue: int | None,
    kills_diff_red: int | None,
    flagged: bool,
) -> None:
    """Insert a row into cv_validation for the OCR-vs-livestats watchdog (spec §6.3)."""
    conn.execute(
        """
        INSERT INTO cv_validation (
            cv_frame_id, live_frame_id, ts_offset_sec,
            gold_diff_blue, gold_diff_red,
            gold_pct_diff_blue, gold_pct_diff_red,
            kills_diff_blue, kills_diff_red, flagged
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (cv_frame_id, live_frame_id, ts_offset_sec,
         gold_diff_blue, gold_diff_red,
         gold_pct_diff_blue, gold_pct_diff_red,
         kills_diff_blue, kills_diff_red, 1 if flagged else 0),
    )
    conn.commit()
