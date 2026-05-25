"""Tests for loltrader.twitch.auth."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from loltrader.twitch.auth import (
    TwitchAuth,
    TwitchAuthError,
    load_twitch_auth,
    streamlink_auth_args,
)


def test_load_twitch_auth_happy_path(tmp_path: Path) -> None:
    p = tmp_path / "twitch_creds.json"
    p.write_text(json.dumps({"auth_token": "abc123xyz"}))
    auth = load_twitch_auth(p)
    assert auth.auth_token == "abc123xyz"


def test_load_twitch_auth_strips_whitespace(tmp_path: Path) -> None:
    p = tmp_path / "twitch_creds.json"
    p.write_text(json.dumps({"auth_token": "  abc123  "}))
    auth = load_twitch_auth(p)
    assert auth.auth_token == "abc123"


def test_load_twitch_auth_missing_file(tmp_path: Path) -> None:
    p = tmp_path / "nonexistent.json"
    with pytest.raises(TwitchAuthError, match="not found"):
        load_twitch_auth(p)


def test_load_twitch_auth_invalid_json(tmp_path: Path) -> None:
    p = tmp_path / "twitch_creds.json"
    p.write_text("not json {{")
    with pytest.raises(TwitchAuthError, match="not valid JSON"):
        load_twitch_auth(p)


def test_load_twitch_auth_missing_token(tmp_path: Path) -> None:
    p = tmp_path / "twitch_creds.json"
    p.write_text(json.dumps({"other_field": "x"}))
    with pytest.raises(TwitchAuthError, match="missing.*auth_token"):
        load_twitch_auth(p)


def test_load_twitch_auth_empty_token(tmp_path: Path) -> None:
    p = tmp_path / "twitch_creds.json"
    p.write_text(json.dumps({"auth_token": "   "}))
    with pytest.raises(TwitchAuthError, match="missing.*auth_token"):
        load_twitch_auth(p)


def test_header_value_format() -> None:
    auth = TwitchAuth(auth_token="xyz")
    assert auth.header_value() == "OAuth xyz"


def test_streamlink_auth_args_includes_auth_header() -> None:
    auth = TwitchAuth(auth_token="xyz")
    args = streamlink_auth_args(auth)
    # Auth header carries the cookie
    assert any("Authorization=OAuth xyz" in a for a in args)
    # Low-latency flag is on per spec §17 #13
    assert "--twitch-low-latency" in args
    # Disable-ads flag is on (subscription handles ads but belt-and-suspenders)
    assert "--twitch-disable-ads" in args
