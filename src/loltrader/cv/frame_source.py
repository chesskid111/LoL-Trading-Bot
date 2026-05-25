"""Frame-source abstraction: yields (timestamp_unix, numpy_image) tuples at ~1 fps.

Two implementations:
  - TwitchLiveSource: streamlink → ffmpeg → png-pipe → OpenCV
  - VideoFileSource: cv2.VideoCapture over a local file (for VOD replay + tests)

Both expose the same iterator interface so cv_pipeline.py doesn't care which
one feeds it. Spec §6.2.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path

import cv2
import numpy as np

from loltrader.twitch.auth import TwitchAuth, streamlink_auth_args

log = logging.getLogger(__name__)

# Default cadence — spec §6.2 (CV reads at 1 fps).
DEFAULT_FPS = 1


class FrameSourceError(RuntimeError):
    """Raised when a frame source can't open or recovers from a fatal error."""


class FrameSource(ABC):
    """Yields (ts_unix, BGR numpy array) tuples until exhausted/stopped."""

    @abstractmethod
    def __iter__(self) -> Iterator[tuple[int, np.ndarray]]:
        ...

    @abstractmethod
    def close(self) -> None:
        ...

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


class VideoFileSource(FrameSource):
    """Read frames from a local video file (mp4, mkv, ts) at the requested fps.

    Yields wall-clock-ts (NOT video-internal timestamps) so downstream code
    can use the same timestamp semantics as the live source. This is fine for
    VOD-replay use because we're processing each frame's content, not its
    intra-video timing.

    For training-data extraction where we DO need video-internal timestamps
    (to align with historical livestats frames), use video_timestamps_mode=True
    to yield (frame_position_seconds, image) instead.
    """

    def __init__(self, path: Path, fps: int = DEFAULT_FPS,
                 video_timestamps_mode: bool = False) -> None:
        self._path = Path(path)
        if not self._path.exists():
            raise FrameSourceError(f"video file not found: {self._path}")
        self._cap = cv2.VideoCapture(str(self._path))
        if not self._cap.isOpened():
            raise FrameSourceError(f"cv2.VideoCapture failed to open {self._path}")
        self._fps = fps
        self._video_ts_mode = video_timestamps_mode
        self._native_fps = self._cap.get(cv2.CAP_PROP_FPS) or 30.0
        # How many native frames to skip per yielded frame
        self._stride = max(1, int(round(self._native_fps / self._fps)))
        self._frame_index = 0

    def __iter__(self) -> Iterator[tuple[int, np.ndarray]]:
        while True:
            ret, frame = self._cap.read()
            if not ret:
                return
            if self._frame_index % self._stride == 0:
                if self._video_ts_mode:
                    ts = int(self._frame_index / self._native_fps)
                else:
                    ts = int(time.time())
                yield ts, frame
            self._frame_index += 1

    def close(self) -> None:
        try:
            self._cap.release()
        except Exception:
            pass


class TwitchLiveSource(FrameSource):
    """Pipe live Twitch HLS through streamlink → ffmpeg → PNG bytes.

    Why subprocess instead of OpenCV-direct-on-HLS-URL?
        - streamlink handles auth, ad-bypass, low-latency mode
        - ffmpeg handles HLS reconnection and decoding more robustly than
          cv2.VideoCapture
        - Subprocess isolation: crash in ffmpeg doesn't crash our pipeline
        - We can swap the encoded image format if needed
    """

    def __init__(self, channel: str, auth: TwitchAuth, fps: int = DEFAULT_FPS,
                 quality: str = "best") -> None:
        self._channel = channel
        self._auth = auth
        self._fps = fps
        self._quality = quality
        self._streamlink_proc: subprocess.Popen | None = None
        self._ffmpeg_proc: subprocess.Popen | None = None

    def _resolve_binary(self, name: str) -> str:
        """Locate a CLI binary, preferring the venv's Scripts dir."""
        py_dir = Path(sys.executable).parent
        for cand in (py_dir / f"{name}.exe", py_dir / name):
            if cand.exists():
                return str(cand)
        from shutil import which
        path = which(name)
        if path is None:
            raise FrameSourceError(f"{name} not found on PATH or in venv Scripts")
        return path

    def _spawn(self) -> None:
        """Start the streamlink → ffmpeg subprocess chain."""
        streamlink = self._resolve_binary("streamlink")
        ffmpeg = self._resolve_binary("ffmpeg")

        url = f"twitch.tv/{self._channel}"
        sl_cmd = [
            streamlink,
            *streamlink_auth_args(self._auth),
            "--stdout",
            url,
            self._quality,
        ]
        # ffmpeg reads MPEG-TS from stdin, outputs 1fps PNG to stdout
        ff_cmd = [
            ffmpeg,
            "-hide_banner", "-loglevel", "warning",
            "-i", "pipe:0",
            "-vf", f"fps={self._fps}",
            "-f", "image2pipe",
            "-vcodec", "png",
            "pipe:1",
        ]

        log.info("Spawning streamlink for %s (quality=%s)", self._channel, self._quality)
        self._streamlink_proc = subprocess.Popen(
            sl_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        log.info("Spawning ffmpeg to decode at %dfps", self._fps)
        self._ffmpeg_proc = subprocess.Popen(
            ff_cmd, stdin=self._streamlink_proc.stdout,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        # Allow streamlink to receive SIGPIPE if ffmpeg dies
        if self._streamlink_proc.stdout is not None:
            self._streamlink_proc.stdout.close()

    @staticmethod
    def _read_png_from_pipe(pipe) -> bytes | None:
        """Read a single PNG image from the ffmpeg stdout pipe.

        PNG format: starts with 8-byte signature, then chunks. Each chunk is
        4 bytes length + 4 bytes type + length bytes data + 4 bytes CRC.
        Ends with IEND chunk. We read until we see IEND.
        """
        signature = pipe.read(8)
        if not signature:
            return None
        if signature[:8] != b"\x89PNG\r\n\x1a\n":
            raise FrameSourceError(
                f"unexpected bytes from ffmpeg, not PNG signature: {signature[:8]!r}"
            )
        chunks = [signature]
        while True:
            length_bytes = pipe.read(4)
            if len(length_bytes) < 4:
                return None  # stream ended mid-image
            length = int.from_bytes(length_bytes, "big")
            type_bytes = pipe.read(4)
            data = pipe.read(length)
            crc = pipe.read(4)
            chunks.extend([length_bytes, type_bytes, data, crc])
            if type_bytes == b"IEND":
                return b"".join(chunks)

    def __iter__(self) -> Iterator[tuple[int, np.ndarray]]:
        if self._ffmpeg_proc is None:
            self._spawn()
        assert self._ffmpeg_proc is not None and self._ffmpeg_proc.stdout is not None
        try:
            while True:
                png_bytes = self._read_png_from_pipe(self._ffmpeg_proc.stdout)
                if png_bytes is None:
                    log.warning("ffmpeg pipe ended; stopping iteration")
                    return
                arr = np.frombuffer(png_bytes, dtype=np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if img is None:
                    log.warning("imdecode failed on a PNG frame; skipping")
                    continue
                yield int(time.time()), img
        finally:
            self.close()

    def close(self) -> None:
        for proc in (self._ffmpeg_proc, self._streamlink_proc):
            if proc is None:
                continue
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
            except Exception as e:
                log.warning("Error closing subprocess: %s", e)
        self._ffmpeg_proc = None
        self._streamlink_proc = None
