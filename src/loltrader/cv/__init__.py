"""Computer-vision pipeline for v2 live trading.

Submodules:
    frame_source: stream-ingestion abstractions (live Twitch or VOD file)
    classifier:   per-frame classifier (in_game / studio / replay / ads / unknown)
    storage:      SQLite writes for cv_frames + cv_validation
"""
from loltrader.cv import classifier, frame_source, storage

__all__ = ["classifier", "frame_source", "storage"]
