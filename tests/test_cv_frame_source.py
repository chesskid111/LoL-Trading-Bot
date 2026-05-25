"""Tests for loltrader.cv.frame_source."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from loltrader.cv.frame_source import (
    FrameSourceError,
    TwitchLiveSource,
    VideoFileSource,
)
from loltrader.twitch.auth import TwitchAuth


def _write_test_video(tmp_path: Path, n_frames: int = 30, fps: float = 30.0) -> Path:
    """Generate a tiny test video with n_frames distinct frames."""
    path = tmp_path / "test.mp4"
    # mp4v works on Windows + small videos; switch codec if needed
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(path), fourcc, fps, (64, 48))
    for i in range(n_frames):
        # Each frame has a unique gray level so we can verify frame ordering
        gray = (i * 8) % 256
        frame = np.full((48, 64, 3), gray, dtype=np.uint8)
        writer.write(frame)
    writer.release()
    return path


def test_video_file_source_yields_frames_at_requested_fps(tmp_path: Path) -> None:
    """30-frame video at 30fps native, requested 1fps → should yield ~1 frame."""
    video = _write_test_video(tmp_path, n_frames=30, fps=30.0)
    source = VideoFileSource(video, fps=1)
    frames = list(source)
    # Stride = 30, so first frame at idx 0, next would be idx 30 (past EOF) → 1 frame
    assert len(frames) == 1
    ts, img = frames[0]
    assert img.shape == (48, 64, 3)


def test_video_file_source_yields_more_with_higher_fps(tmp_path: Path) -> None:
    """30-frame video at 30fps native, requested 10fps → yields 3 frames (stride=3)."""
    video = _write_test_video(tmp_path, n_frames=30, fps=30.0)
    source = VideoFileSource(video, fps=10)
    frames = list(source)
    # stride = 30/10 = 3, frames at idx 0, 3, 6, ..., 27 → 10 frames
    assert len(frames) == 10


def test_video_file_source_raises_on_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FrameSourceError, match="not found"):
        VideoFileSource(tmp_path / "nonexistent.mp4")


def test_video_file_source_video_timestamps_mode(tmp_path: Path) -> None:
    """In video_timestamps_mode, ts should reflect position in video, not wall clock."""
    video = _write_test_video(tmp_path, n_frames=60, fps=30.0)  # 2 sec of video
    source = VideoFileSource(video, fps=1, video_timestamps_mode=True)
    frames = list(source)
    timestamps = [ts for ts, _ in frames]
    # Expect ts roughly = 0, then nothing more (60-frame stride means only 1 captured at 0)
    assert timestamps[0] == 0


def test_video_file_source_context_manager(tmp_path: Path) -> None:
    video = _write_test_video(tmp_path, n_frames=10, fps=30.0)
    with VideoFileSource(video, fps=1) as source:
        frames = list(source)
    assert len(frames) >= 1


def test_twitch_live_source_does_not_spawn_until_iterated() -> None:
    """Construction is cheap; iteration triggers subprocess spawn (which we
    don't exercise here — that's a live-channel acceptance test)."""
    auth = TwitchAuth(auth_token="fake")
    source = TwitchLiveSource("lck", auth)
    # No subprocesses should have been spawned yet
    assert source._ffmpeg_proc is None
    assert source._streamlink_proc is None
    source.close()  # safe even though nothing was spawned


def test_twitch_live_source_resolve_binary_finds_streamlink() -> None:
    """Sanity check: streamlink binary is locatable from the running venv."""
    auth = TwitchAuth(auth_token="fake")
    source = TwitchLiveSource("lck", auth)
    # Should not raise — streamlink is installed in the venv
    sl_path = source._resolve_binary("streamlink")
    assert sl_path
    # File should exist
    assert Path(sl_path).exists()
