"""Riot livestats API ingestion for v2 live trading.

Three submodules:
    discovery: find live games, probe delays, parse team sides
    storage:   SQLite writes (UPSERT on game_id+frame_ts), game-start cache
    poller:    long-running per-game polling loop
"""
from loltrader.livestats import discovery, poller, storage

__all__ = ["discovery", "poller", "storage"]
