"""Entry point: per-game CV pipeline subprocess.

Pulls Twitch HLS frames at 1 fps, classifies each, writes to cv_frames.
Optionally retains PNG snapshots on disk for 30 days (spec §13.4).

OCR + minimap + items extraction are NO-OPS in Phase 3 — Phase 4 adds them.
Phase 3 only verifies the stream ingestion + classifier + storage layer.

Usage (live):
    python -m loltrader.tools.cv_pipeline <game_id> --channel lck

Usage (VOD replay):
    python -m loltrader.tools.cv_pipeline <game_id> --vod /path/to/video.mp4

Spec §6.2.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import cv2

from loltrader.config import load_config
from loltrader.cv import storage as cv_storage
from loltrader.cv.classifier import FrameClassifier
from loltrader.cv.frame_source import FrameSource, TwitchLiveSource, VideoFileSource
from loltrader.db import connect
from loltrader.twitch.auth import load_twitch_auth

# Same heartbeat cadence as the livestats poller.
HEARTBEAT_INTERVAL_SEC = 10.0

log = logging.getLogger(__name__)


def heartbeat_path(game_id: str, project_root: Path) -> Path:
    return project_root / "data" / "heartbeat" / f"cv_pipeline_{game_id}"


def _encode_png(img) -> bytes | None:
    """Encode a BGR image to PNG bytes. Returns None on failure."""
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        return None
    return bytes(buf)


def run_pipeline(
    game_id: str,
    source: FrameSource,
    classifier: FrameClassifier,
    project_root: Path,
    retain_pngs: bool = True,
    max_runtime_sec: float | None = None,
) -> dict[str, int]:
    """Long-running per-frame loop. Yields stats dict at exit."""
    stats: dict[str, int] = {
        "frames_classified": 0,
        "frames_inserted": 0,
        "frames_skipped_dup": 0,
        "frames_png_saved": 0,
        "classify_failures": 0,
    }
    conn = connect()
    hb = heartbeat_path(game_id, project_root)
    last_hb = 0.0
    started = time.time()
    try:
        for frame_ts, img in source:
            if max_runtime_sec is not None and (time.time() - started) > max_runtime_sec:
                log.info("Max runtime reached")
                break

            now = time.time()
            if now - last_hb >= HEARTBEAT_INTERVAL_SEC:
                hb.parent.mkdir(parents=True, exist_ok=True)
                hb.touch()
                last_hb = now

            try:
                result = classifier.classify(img)
            except Exception as e:
                log.warning("classify failed: %s", e)
                stats["classify_failures"] += 1
                continue
            stats["frames_classified"] += 1

            png_path: str | None = None
            if retain_pngs and result.label != "ads":  # don't waste disk on ads
                pb = _encode_png(img)
                if pb is not None:
                    png_path = cv_storage.save_frame_png(pb, game_id, frame_ts, project_root)
                    stats["frames_png_saved"] += 1

            record = cv_storage.CvFrameRecord(
                game_id=game_id,
                frame_ts_unix=frame_ts,
                classifier_class=result.label,
                classifier_confidence=result.confidence,
                frame_png_path=png_path,
            )
            inserted = cv_storage.write_cv_frame(conn, record)
            if inserted:
                stats["frames_inserted"] += 1
            else:
                stats["frames_skipped_dup"] += 1
    finally:
        conn.close()
        source.close()
    return stats


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("game_id", help="Riot esports gameId (used as cv_frames.game_id)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--channel", help="Twitch channel to pull live (e.g. 'lck')")
    g.add_argument("--vod", help="Path to a VOD file to process offline")
    p.add_argument("--quality", default="best", help="streamlink quality (live mode)")
    p.add_argument("--fps", type=int, default=1, help="Frames per second to process")
    p.add_argument("--max-runtime-sec", type=float, default=None,
                   help="Optional safety stop. Default = run until source ends.")
    p.add_argument("--no-retain-pngs", action="store_true",
                   help="Skip saving PNG snapshots to disk")
    p.add_argument("--refs-dir", default=None,
                   help="Override reference-frame directory")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    cfg = load_config()
    refs_dir = Path(args.refs_dir) if args.refs_dir else cfg.data_dir / "cv_references"
    clf = FrameClassifier(refs_dir)
    if not clf.has_references:
        log.warning("No reference frames in %s — classifier will return UNKNOWN", refs_dir)

    source: FrameSource
    if args.vod:
        source = VideoFileSource(Path(args.vod), fps=args.fps)
    else:
        auth = load_twitch_auth()
        source = TwitchLiveSource(args.channel, auth, fps=args.fps, quality=args.quality)

    stats = run_pipeline(
        game_id=args.game_id,
        source=source,
        classifier=clf,
        project_root=cfg.project_root,
        retain_pngs=not args.no_retain_pngs,
        max_runtime_sec=args.max_runtime_sec,
    )
    print("cv_pipeline exit stats:")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
