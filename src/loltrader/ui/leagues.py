"""Team-to-league mapping for filtering and display.

Kalshi only stores `competition='League of Legends'` for all LoL markets — it
doesn't tag which regional league each match belongs to. We derive that here
by matching team names in the event title.

When a match contains teams from different leagues (international events
like MSI / Worlds / EWC), we classify it as 'International'.
"""
from __future__ import annotations

import re

# Major-league team rosters (full names + common abbreviations as they appear
# in Kalshi event titles). Keep these in sync with current splits.

LCK_TEAMS = {
    "T1", "kt", "kt Rolster", "KT Rolster", "KT",
    "Gen.G", "Gen.G Esports", "GEN", "GENG",
    "Dplus KIA", "DK", "DPLUS KIA",
    "Hanwha Life Esports", "HLE",
    "KIWOOM DRX", "DRX",
    "NONGSHIM RED FORCE", "NS", "NRF",
    "BNK FEARX", "BFX",
    "HANJIN BRION", "HBR", "BRO",
    "DN SOOPers", "DNS", "DNF",  # DN Freecs / DN SOOPers depending on split
    "OKSavingsBank BRION",
}

LCS_TEAMS = {
    # NA / North America (now part of LTA North)
    "Cloud9", "C9",
    "FlyQuest", "FLY",
    "Team Liquid", "TL", "Team Liquid Alienware", "TLA", "TLAW",
    "100 Thieves", "100T",
    "Dignitas", "DIG",
    "NRG Esports", "NRG",
    "Immortals", "IMT",
    "Shopify Rebellion", "SR",
    "LYON", "Lyon",
    "Sentinels", "SEN",
    "Disguised", "DSG",
    "Golden Guardians", "GG", "GGS",  # disambiguate from Gen.G via title context
}

LEC_TEAMS = {
    "G2", "G2 Esports",
    "Karmine Corp", "KC",
    "Fnatic", "FNC",
    "MAD Lions", "MAD",
    "Team BDS", "BDS",
    "GIANTX", "GX",
    "Rogue", "RGE",
    "Team Heretics", "TH",
    "Vitality", "VIT",
    "SK Gaming", "SK",
    "Natus Vincere", "NAVI",
    "Astralis", "AST",
    "Excel Esports", "XL", "EXCEL",
}

LPL_TEAMS = {
    "Bilibili Gaming", "BLG",
    "JDG Intel Esports Club", "JDG",
    "Top Esports", "TES",
    "Edward Gaming", "EDG",
    "Royal Never Give Up", "RNG",
    "LNG Esports", "LNG",
    "FunPlus Phoenix", "FPX",
    "Invictus Gaming", "IG",
    "Weibo Gaming", "WBG",
    "Team WE", "WE",
    "LGD Gaming", "LGD",
    "Anyone's Legend", "AL",
    "Oh My God", "OMG",
    "ThunderTalk Gaming", "TT",
    "Ninjas in Pyjamas", "NIP",
    "Ultra Prime", "UP", "UPT",
    "Rare Atom", "RA",
    "EDG Gaming",
}

LJL_TEAMS = {
    "DetonatioN FocusMe", "DFM",
    "V3 Esports", "V3",
    "Sengoku Gaming", "SG", "SHG",
    "BC Swell", "BC",
    "Fukuoka SoftBank HAWKS gaming", "SHG", "HKG",
}

LTA_SOUTH_TEAMS = {  # Latin America South
    "Los Grandes", "LOS",
    "Vivo Keyd Stars", "VKS",
    "Kaya", "KAY",
    "INTZ", "Int",
    "FURIA Esports", "FUR",
    "RED Canids", "RED",
    "ISURUS Estral", "ISG",
    "Leviatán", "LEV",
    "Knights", "KCB",
    "Paingame", "PNG",
    "Fluxo W7M", "FLU", "FLUXO",
    "All Knights", "ALL", "AKE",
}

LCP_TEAMS = {  # Asia-Pacific
    "GAM Esports", "GAM",
    "Vikings Esports", "VKE",
    "CTBC Flying Oyster", "CFO",
    "Deep Cross Gaming", "DCG",
    "Awaken Tiger Zombie", "ATZ",
    "Frank Esports", "FE",
    "MGN Vikings",
    "Bren Esports", "BRU",
    "Frank Esports BC",
    "Relove Deep Cross Gaming",
}

CBLOL_TEAMS = {  # Brazil
    "LOUD", "LLL",
    "paiN Gaming", "PAIN", "PNG",  # NB: PNG conflicts with Paingame in LTA-S — disambiguate below
    "Vivo Keyd Stars", "VKS",
    "RED Canids", "RED",
    "FURIA Esports", "FUR", "FURIA",
    "Fluxo W7M", "FLU", "FLUXO",
    "Los Grandes", "LOS",
    "INTZ", "Int",
    "Kabum Esports", "KBM", "KaBuM!",
    "Leviatán", "LEV",
}

LEAGUE_TEAMS: dict[str, set[str]] = {
    "LCK": LCK_TEAMS,
    "LCS": LCS_TEAMS,
    "LEC": LEC_TEAMS,
    "LPL": LPL_TEAMS,
    "LJL": LJL_TEAMS,
    "CBLOL": CBLOL_TEAMS,
    "LTA-S": LTA_SOUTH_TEAMS,
    "LCP": LCP_TEAMS,
}

# Order in which to display leagues in UI filters
LEAGUE_DISPLAY_ORDER = ["LCK", "LCS", "LEC", "LPL", "LCP", "LTA-S", "CBLOL", "LJL", "International", "Other"]

# Tier-1 leagues shown by default (the ones actually traded). Hides LJL/CBLOL/
# ERL/academy/Other clutter unless the user explicitly asks for "all" or a
# specific minor league via the dropdown.
TIER1_DEFAULT = {"LCK", "LPL", "LEC", "LCS", "LTA-S", "International"}


def _extract_teams_from_title(title: str) -> tuple[str, str] | None:
    """Parse the two team names from a market title. Returns (a, b) or None.

    Kalshi titles look like:
        "Will {winner} win the {Team A} vs. {Team B} League of Legends match?"
    We must strip the "Will … win the " prefix and the trailing
    " League of Legends match?" suffix, or the team names carry garbage that
    breaks exact-match classification.
    """
    if not title:
        return None
    s = title.strip()
    # Strip trailing "League of Legends match?" / "match?" / "?"
    s = re.sub(r"(?i)\s+league of legends match\??\s*$", "", s)
    s = re.sub(r"(?i)\s+match\??\s*$", "", s)
    s = s.rstrip("?").strip()
    # The "{A} vs. {B}" core always follows the last " the " — handles all
    # Kalshi formats: "...win the A vs. B", "...win map 1 in the A vs. B",
    # "...maps be played in the A vs. B". Greedy .* grabs the LAST "the".
    m = re.search(r"(?i)^.*\bthe\s+(.+?\s+vs\.?\s+.+)$", s)
    core = m.group(1) if m else s
    for sep in (" vs. ", " vs ", " VS ", " v. "):
        if sep in core:
            a, b = core.split(sep, 1)
            a, b = a.strip(), b.strip()
            if a and b:
                return a, b
    return None


def league_for_match(title: str, sub_title: str | None = None) -> str:
    """Classify a match into a league based on team names in the title.

    Returns one of the LEAGUE_DISPLAY_ORDER values.
    'International' if teams are from different known leagues (e.g. MSI/Worlds).
    'Other' if neither team matches a known league.
    """
    pair = _extract_teams_from_title(title)
    if not pair:
        return "Other"
    team_a, team_b = pair

    # Generic words shared across many team names — skip when matching
    STOP_WORDS = {"esports", "gaming", "team", "the", "of", "league", "legends",
                  "club", "academy", "challengers", "esport", "pro"}

    # Academy / Challenger / development squads are NOT their parent org's
    # tier-1 roster — e.g. "KT Rolster Challengers" must not classify as LCK
    # via "KT Rolster". Mark them so they don't inherit the parent league.
    ACADEMY_MARKERS = ("challenger", "challengers", "academy", "youth",
                       "developmental", " jr", "junior")

    def _league_of(team: str) -> str | None:
        team_lower = team.lower().strip()
        if not team_lower:
            return None
        if any(mark in team_lower for mark in ACADEMY_MARKERS):
            return None
        team_words = set(team_lower.split()) - STOP_WORDS

        # Pass 1: exact match (most reliable) — handles short codes
        for league, members in LEAGUE_TEAMS.items():
            for m in members:
                if m and m.lower() == team_lower:
                    return league

        # Pass 2: distinctive-word match (skip generic words like "Esports").
        # NOTE: deliberately no fuzzy substring pass — it produced false
        # positives (ERL/minor teams spuriously tagged LCK/International).
        for league, members in LEAGUE_TEAMS.items():
            for m in members:
                if not m:
                    continue
                m_words = set(m.lower().split()) - STOP_WORDS
                if team_words & m_words:
                    return league

        return None

    la = _league_of(team_a)
    lb = _league_of(team_b)
    if la and lb:
        return la if la == lb else "International"
    return la or lb or "Other"
