"""Initial team_aliases seed data.

Maps Kalshi's variant spellings to Oracle's Elixir canonical team names.
Grown over time via the manual review CLI.
"""
from __future__ import annotations

import sqlite3
import time

# (alias_used_by_Kalshi, canonical_name_in_Oracle's_Elixir)
SEED_ALIASES: list[tuple[str, str]] = [
    # NA / LCS
    ("C9", "Cloud9"),
    ("Cloud9 Esports", "Cloud9"),
    ("100T", "100 Thieves"),
    ("100Thieves", "100 Thieves"),
    ("TL", "Team Liquid"),
    ("FLY", "FlyQuest"),
    ("TSM", "TSM"),  # already canonical, listed for completeness
    ("CLG", "Counter Logic Gaming"),
    ("IMT", "Immortals"),
    ("EG", "Evil Geniuses"),
    ("DIG", "Dignitas"),
    ("GG", "Golden Guardians"),
    ("SR", "Shopify Rebellion"),
    ("NRG", "NRG Esports"),
    ("LYON", "LYON"),

    # EU / LEC
    ("G2", "G2 Esports"),
    ("FNC", "Fnatic"),
    ("MAD", "MAD Lions KOI"),
    ("MAD Lions", "MAD Lions KOI"),
    ("KOI", "Movistar KOI"),
    ("XL", "Excel Esports"),
    ("XL Esports", "Excel Esports"),
    ("VIT", "Team Vitality"),
    ("Vitality", "Team Vitality"),
    ("TH", "Team Heretics"),
    ("Heretics", "Team Heretics"),
    ("SK", "SK Gaming"),
    ("KC", "Karmine Corp"),
    ("RGE", "Rogue"),
    ("BDS", "Team BDS"),
    ("GX", "GIANTX"),

    # KR / LCK  (Oracle's canonical names sometimes lag the current sponsor.)
    ("T1", "T1"),
    ("GENG", "Gen.G"),
    ("Gen G", "Gen.G"),
    ("Gen.G Esports", "Gen.G"),
    ("Gen.G eSports", "Gen.G"),
    ("HLE", "Hanwha Life Esports"),
    ("Hanwha Life", "Hanwha Life Esports"),
    ("DK", "Dplus Kia"),
    ("DAMWON KIA", "Dplus Kia"),
    ("Dplus", "Dplus Kia"),
    ("Dplus KIA", "Dplus Kia"),
    ("KT", "KT Rolster"),
    ("NS", "Nongshim RedForce"),
    ("Nongshim Red Force", "Nongshim RedForce"),
    ("Nongshim", "Nongshim RedForce"),
    # BRION has had many sponsors. Oracle stores historical "HANJIN BRION".
    ("BRO", "HANJIN BRION"),
    ("BRION", "HANJIN BRION"),
    ("OKSavingsBank BRION", "HANJIN BRION"),
    ("OKBRION", "HANJIN BRION"),
    ("OK BRION", "HANJIN BRION"),
    # DRX has been "Kiwoom DRX" in recent Oracle data
    ("DRX", "Kiwoom DRX"),
    # FearX
    ("FX", "BNK FEARX"),
    ("BNK FearX", "BNK FEARX"),
    ("FearX", "BNK FEARX"),
    # DN Freecs / SOOPers
    ("DN Freecs", "DN SOOPers"),
    ("Freecs", "DN SOOPers"),
    ("SOOPers", "DN SOOPers"),

    # CN / LPL (many use short names). LPL teams often shorten in Oracle.
    ("BLG", "Bilibili Gaming"),
    ("Bilibili", "Bilibili Gaming"),
    ("TES", "TOP Esports"),
    ("TOP", "TOP Esports"),
    ("WBG", "Weibo Gaming"),
    ("Weibo", "Weibo Gaming"),
    ("JDG", "JD Gaming"),
    ("JDG Intel Esports Club", "JD Gaming"),
    ("LNG", "LNG Esports"),
    ("EDG", "EDward Gaming"),
    ("EDward Gaming Hycan", "EDward Gaming"),
    ("Edward Gaming", "EDward Gaming"),
    ("Edward Gaming Hycan", "EDward Gaming"),
    ("AL", "Anyone's Legend"),
    ("Anyones Legend", "Anyone's Legend"),
    ("Anyone Legend", "Anyone's Legend"),
    ("RNG", "Royal Never Give Up"),
    ("IG", "Invictus Gaming"),
    ("FPX", "FunPlus Phoenix"),
    ("OMG", "Oh My God"),
    ("UP", "Ultra Prime"),
    ("LGD", "LGD Gaming"),
    ("Team WE", "WE"),
    ("WE", "WE"),
    ("RA", "Rare Atom"),
    ("NIP", "Ninjas In Pyjamas"),
    ("Ninjas in Pyjamas", "Ninjas In Pyjamas"),
    ("NIP.CN", "Ninjas In Pyjamas"),
    ("TT", "ThunderTalk Gaming"),
    ("TTG", "ThunderTalk Gaming"),
    ("ThunderTalk", "ThunderTalk Gaming"),

    # BR / CBLOL — Kalshi may list these
    ("LOS", "LOS"),
    ("VKS", "Vivo Keyd Stars"),
    ("Vivo Keyd", "Vivo Keyd Stars"),
    ("PAIN", "paiN Gaming"),
    ("paiN", "paiN Gaming"),
    ("PNGA", "paiN Gaming Academy"),
    ("INTZ", "INTZ"),
    ("INTZ e-Sports", "INTZ"),
    ("LOUD", "LOUD"),
    ("RED", "RED Canids"),
    ("FUR", "FURIA Esports"),
    ("Furia", "FURIA Esports"),
    ("KBM", "KaBuM! Esports"),
    ("KaBuM", "KaBuM! Esports"),
]


def seed_team_aliases(conn: sqlite3.Connection) -> int:
    """Refresh SEED_ALIASES. Wipes existing rows where source='seed' (so
    edits to this file take effect on re-run) but leaves manual aliases
    intact. Returns the number of seed rows inserted."""
    now = int(time.time())
    conn.execute("DELETE FROM team_aliases WHERE source = 'seed'")
    inserted = 0
    for alias, canonical in SEED_ALIASES:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO team_aliases (alias, canonical_name, source, created_at)
            VALUES (?, ?, 'seed', ?)
            """,
            (alias, canonical, now),
        )
        if cur.rowcount > 0:
            inserted += 1
    conn.commit()
    return inserted
