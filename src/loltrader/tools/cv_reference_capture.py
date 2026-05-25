"""Capture candidate reference frames from a VOD for classifier training.

Workflow:
    1. Run this against a VOD file with --interval N (default 30s)
    2. It writes PNGs to a flat output directory
    3. You manually sort them into:
         data/cv_references/in_game/
         data/cv_references/studio/
         data/cv_references/replay/
         data/cv_references/ads/
    4. ~10-30 PNGs per class is sufficient for the SSIM-based classifier.
    5. Frames you can't confidently label → drop them.

Usage:
    python -m loltrader.tools.cv_reference_capture <video.mp4> \\
        --interval 30 --output data/cv_capture_session_1

Spec §6.2 (CV classifier reference frames).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("video", help="Path to video file (mp4/mkv/ts)")
    p.add_argument("--interval", type=float, default=30.0,
                   help="Seconds between captured frames (default: 30)")
    p.add_argument("--output", default="data/cv_capture_session",
                   help="Output directory for PNGs")
    p.add_argument("--max-frames", type=int, default=200,
                   help="Stop after this many captures (default 200)")
    args = p.parse_args(argv)

    video = Path(args.video)
    if not video.exists():
        print(f"ERROR: video not found: {video}", file=sys.stderr)
        return 1

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        print(f"ERROR: cv2 could not open: {video}", file=sys.stderr)
        return 2

    native_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration_sec = total_frames / native_fps if total_frames else None

    print(f"Source: {video}")
    if duration_sec:
        print(f"  native fps={native_fps:.1f}, total_frames={total_frames}, "
              f"duration={duration_sec:.0f}s ({duration_sec/60:.1f}min)")
    else:
        print(f"  fps={native_fps:.1f}, duration unknown")
    print(f"  capturing every {args.interval}s")
    print(f"  output dir: {out_dir}")
    print()

    # Seek-based extraction — orders of magnitude faster than reading every
    # frame for long videos. We seek to msec offsets directly.
    captured = 0
    ts_sec = 0
    while captured < args.max_frames:
        if duration_sec is not None and ts_sec >= duration_sec:
            break
        cap.set(cv2.CAP_PROP_POS_MSEC, ts_sec * 1000)
        ret, frame = cap.read()
        if not ret:
            break
        out_path = out_dir / f"frame_{ts_sec:06d}s.png"
        cv2.imwrite(str(out_path), frame)
        captured += 1
        if captured % 10 == 0:
            print(f"  captured {captured} ({ts_sec}s into video)")
        ts_sec += int(args.interval)

    cap.release()
    print()
    print(f"Done: captured {captured} reference candidates to {out_dir}")
    print()
    print("Next steps:")
    print(f"  1. Browse {out_dir} and sort PNGs by class")
    print("  2. Move 10-30 best examples per class into:")
    print("       data/cv_references/in_game/")
    print("       data/cv_references/studio/")
    print("       data/cv_references/replay/")
    print("       data/cv_references/ads/")
    print("  3. Delete uncategorizable frames")
    return 0


if __name__ == "__main__":
    sys.exit(main())
