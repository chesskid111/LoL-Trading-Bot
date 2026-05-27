"""Download + cache champion icons from Riot DataDragon.

DataDragon serves League of Legends champion portraits at:
    https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{champion_key}.png

Each icon is ~120x120. We downscale to the minimap-icon size (~25-30px) at
load time. Icons are cached locally in data/datadragon/{version}/ to avoid
re-downloading.

The "version" follows LoL patch numbers (e.g. "14.10.1"). The current version
is fetched from the DataDragon versions endpoint. We cache by version so
that patch changes pull fresh icons (icon art can change subtly across patches).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import requests

log = logging.getLogger(__name__)

DDRAGON_BASE = "https://ddragon.leagueoflegends.com"
VERSIONS_URL = f"{DDRAGON_BASE}/api/versions.json"


def get_latest_version() -> str:
    """Fetch the latest DataDragon patch version from Riot's API."""
    r = requests.get(VERSIONS_URL, timeout=10)
    r.raise_for_status()
    versions = r.json()
    if not versions:
        raise RuntimeError("DataDragon versions endpoint returned empty list")
    return versions[0]  # First entry is the latest


def get_champion_list(version: str) -> dict[str, dict]:
    """Return {champion_key: champion_metadata} for the given DDragon version."""
    url = f"{DDRAGON_BASE}/cdn/{version}/data/en_US/champion.json"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return r.json().get("data", {})


def _icon_url(version: str, champion_key: str) -> str:
    return f"{DDRAGON_BASE}/cdn/{version}/img/champion/{champion_key}.png"


def _cache_dir(version: str, project_root: Path) -> Path:
    return project_root / "data" / "datadragon" / version / "champion_icons"


def fetch_champion_icon(
    champion_key: str,
    version: str,
    project_root: Path,
    force_redownload: bool = False,
) -> np.ndarray:
    """Return a BGR numpy image of the champion's portrait icon.

    Downloads from DataDragon if not in the local cache.
    """
    cache = _cache_dir(version, project_root)
    cache.mkdir(parents=True, exist_ok=True)
    local = cache / f"{champion_key}.png"
    if local.exists() and not force_redownload:
        img = cv2.imread(str(local), cv2.IMREAD_COLOR)
        if img is not None:
            return img
        log.warning("cached icon for %s unreadable; redownloading", champion_key)

    url = _icon_url(version, champion_key)
    log.info("downloading %s icon from %s", champion_key, url)
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    local.write_bytes(r.content)
    img = cv2.imread(str(local), cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"downloaded icon for {champion_key} is unreadable")
    return img


def fetch_all_champion_icons(version: str, project_root: Path) -> int:
    """Pre-fetch every champion icon for the given version. Returns count downloaded.

    Useful as a one-time bootstrap so live operation doesn't pause for downloads.
    """
    champions = get_champion_list(version)
    cache = _cache_dir(version, project_root)
    cache.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    for champion_key in champions:
        local = cache / f"{champion_key}.png"
        if local.exists():
            continue
        try:
            fetch_champion_icon(champion_key, version, project_root)
            downloaded += 1
        except Exception as e:
            log.warning("failed to download %s: %s", champion_key, e)
    return downloaded


@dataclass(frozen=True)
class ChampionTemplate:
    """A champion icon prepared for minimap template matching."""
    key: str               # DDragon key, e.g. "Caitlyn"
    template: np.ndarray   # downscaled icon ready for cv2.matchTemplate


def load_template(
    champion_key: str,
    version: str,
    project_root: Path,
    target_size: int = 28,
) -> ChampionTemplate:
    """Load a champion icon and downscale it to minimap-icon size.

    target_size in pixels — what we expect the icon to look like on a
    1280x720 minimap. Default 28px is tuned for the LCK 2026 broadcast
    minimap dimensions.
    """
    icon = fetch_champion_icon(champion_key, version, project_root)
    resized = cv2.resize(icon, (target_size, target_size), interpolation=cv2.INTER_AREA)
    return ChampionTemplate(key=champion_key, template=resized)
