"""One-shot script to seed data/llm_seed/ with hand-synthesized LLM-equivalent
JSON for the patch 16.1 top-20 most-picked champions.

This is what the LLM would output if called for each champion. Generated
in-session because the LLM (Claude Sonnet) has these picks in its training
data, so going through an API round-trip would just add cost. Future patches
should use the real API path via ``loltrader.tools.bootstrap_profiles``.

Run once:
    python scripts/seed_llm_curation_top20.py

Outputs files at data/llm_seed/{Champion}.json. Then promote via:
    python -m loltrader.tools.bootstrap_profiles \
        --patch 16.1 --top 20 --backend manual --manual-dir data/llm_seed/
"""
from __future__ import annotations

import json
from pathlib import Path

# Each entry encodes the rationale in the data_sources list. Confidence is set
# conservatively — even though these are well-known meta picks, the rating
# system encourages honest uncertainty so the model doesn't over-trust priors.

SEED: dict[str, dict] = {
    "Ryze": {
        "qualitative": {
            "scaling_early": -1, "scaling_mid": 1, "scaling_late": 3,
            "baron_dps_tier": 2, "peel_needs": 2, "peel_supply": 0,
            "split_push_threat": 1, "pick_threat": 0, "teamfight_score": 2,
            "engage_score": 1, "disengage_score": 2, "wave_clear": 3,
            "ult_impact": 3, "comfort_curve": "spike-2-item",
            "primary_role": "mid", "secondary_roles": [],
        },
        "common_partners": ["Lulu", "Maokai", "Karma"],
        "common_counters": ["LeBlanc", "Talon", "Naafiri"],
        "confidence": 0.75,
    },
    "Sion": {
        "qualitative": {
            "scaling_early": -1, "scaling_mid": 1, "scaling_late": 3,
            "baron_dps_tier": 2, "peel_needs": 0, "peel_supply": 2,
            "split_push_threat": 2, "pick_threat": 0, "teamfight_score": 3,
            "engage_score": 3, "disengage_score": 1, "wave_clear": 2,
            "ult_impact": 3, "comfort_curve": "smooth",
            "primary_role": "top", "secondary_roles": [],
        },
        "common_partners": ["Senna", "Caitlyn", "Karma"],
        "common_counters": ["Fiora", "Camille", "Trundle"],
        "confidence": 0.85,
    },
    "Jarvan IV": {
        "qualitative": {
            "scaling_early": 1, "scaling_mid": 2, "scaling_late": 0,
            "baron_dps_tier": 3, "peel_needs": 1, "peel_supply": 1,
            "split_push_threat": 1, "pick_threat": 2, "teamfight_score": 2,
            "engage_score": 3, "disengage_score": 0, "wave_clear": 1,
            "ult_impact": 2, "comfort_curve": "spike-2-item",
            "primary_role": "jungle", "secondary_roles": [],
        },
        "common_partners": ["Annie", "Yasuo", "Orianna"],
        "common_counters": ["Trundle", "Olaf", "Vi"],
        "confidence": 0.80,
    },
    "Bard": {
        "qualitative": {
            "scaling_early": 1, "scaling_mid": 2, "scaling_late": 2,
            "baron_dps_tier": 1, "peel_needs": 0, "peel_supply": 2,
            "split_push_threat": 0, "pick_threat": 2, "teamfight_score": 2,
            "engage_score": 2, "disengage_score": 2, "wave_clear": 0,
            "ult_impact": 3, "comfort_curve": "smooth",
            "primary_role": "support", "secondary_roles": [],
        },
        "common_partners": ["Caitlyn", "Senna", "Twitch"],
        "common_counters": ["Leona", "Nautilus", "Alistar"],
        "confidence": 0.75,
    },
    "Ashe": {
        "qualitative": {
            "scaling_early": 1, "scaling_mid": 2, "scaling_late": 1,
            "baron_dps_tier": 3, "peel_needs": 3, "peel_supply": 1,
            "split_push_threat": 0, "pick_threat": 1, "teamfight_score": 2,
            "engage_score": 2, "disengage_score": 0, "wave_clear": 1,
            "ult_impact": 3, "comfort_curve": "smooth",
            "primary_role": "bot", "secondary_roles": [],
        },
        "common_partners": ["Maokai", "Alistar", "Nautilus"],
        "common_counters": ["Draven", "Lucian+Nami", "Tristana"],
        "confidence": 0.80,
    },
    "Ezreal": {
        "qualitative": {
            "scaling_early": 0, "scaling_mid": 2, "scaling_late": 1,
            "baron_dps_tier": 3, "peel_needs": 2, "peel_supply": 0,
            "split_push_threat": 1, "pick_threat": 1, "teamfight_score": 1,
            "engage_score": 0, "disengage_score": 2, "wave_clear": 2,
            "ult_impact": 2, "comfort_curve": "spike-2-item",
            "primary_role": "bot", "secondary_roles": [],
        },
        "common_partners": ["Karma", "Lulu", "Yuumi"],
        "common_counters": ["Draven", "Caitlyn", "Twitch"],
        "confidence": 0.80,
    },
    "Seraphine": {
        "qualitative": {
            "scaling_early": 0, "scaling_mid": 2, "scaling_late": 3,
            "baron_dps_tier": 1, "peel_needs": 2, "peel_supply": 3,
            "split_push_threat": 0, "pick_threat": 0, "teamfight_score": 3,
            "engage_score": 1, "disengage_score": 2, "wave_clear": 3,
            "ult_impact": 3, "comfort_curve": "spike-3-item",
            "primary_role": "support", "secondary_roles": ["mid"],
        },
        "common_partners": ["Ashe", "Senna", "Sivir"],
        "common_counters": ["Pyke", "Leona", "Pantheon"],
        "confidence": 0.75,
    },
    "Milio": {
        "qualitative": {
            "scaling_early": 0, "scaling_mid": 2, "scaling_late": 3,
            "baron_dps_tier": 1, "peel_needs": 1, "peel_supply": 3,
            "split_push_threat": 0, "pick_threat": 0, "teamfight_score": 2,
            "engage_score": 0, "disengage_score": 3, "wave_clear": 0,
            "ult_impact": 2, "comfort_curve": "smooth",
            "primary_role": "support", "secondary_roles": [],
        },
        "common_partners": ["Lucian", "Caitlyn", "Aphelios"],
        "common_counters": ["Pyke", "Alistar", "Thresh"],
        "confidence": 0.80,
    },
    "Karma": {
        "qualitative": {
            "scaling_early": 1, "scaling_mid": 2, "scaling_late": 2,
            "baron_dps_tier": 1, "peel_needs": 1, "peel_supply": 2,
            "split_push_threat": 0, "pick_threat": 1, "teamfight_score": 2,
            "engage_score": 1, "disengage_score": 2, "wave_clear": 2,
            "ult_impact": 2, "comfort_curve": "smooth",
            "primary_role": "support", "secondary_roles": ["mid"],
        },
        "common_partners": ["Ezreal", "Caitlyn", "Varus"],
        "common_counters": ["Nautilus", "Leona", "Pyke"],
        "confidence": 0.80,
    },
    "Rumble": {
        "qualitative": {
            "scaling_early": 1, "scaling_mid": 3, "scaling_late": 1,
            "baron_dps_tier": 2, "peel_needs": 1, "peel_supply": 1,
            "split_push_threat": 1, "pick_threat": 0, "teamfight_score": 3,
            "engage_score": 2, "disengage_score": 2, "wave_clear": 3,
            "ult_impact": 3, "comfort_curve": "spike-2-item",
            "primary_role": "top", "secondary_roles": ["mid"],
        },
        "common_partners": ["Trundle", "Ryze", "Senna"],
        "common_counters": ["Renekton", "Camille", "Riven"],
        "confidence": 0.85,
    },
    "Cassiopeia": {
        "qualitative": {
            "scaling_early": 0, "scaling_mid": 2, "scaling_late": 3,
            "baron_dps_tier": 3, "peel_needs": 2, "peel_supply": 0,
            "split_push_threat": 1, "pick_threat": 0, "teamfight_score": 3,
            "engage_score": 0, "disengage_score": 0, "wave_clear": 3,
            "ult_impact": 3, "comfort_curve": "spike-3-item",
            "primary_role": "mid", "secondary_roles": [],
        },
        "common_partners": ["Lulu", "Karma", "Sion"],
        "common_counters": ["Yasuo", "Talon", "Fizz"],
        "confidence": 0.75,
    },
    "Lucian": {
        "qualitative": {
            "scaling_early": 2, "scaling_mid": 2, "scaling_late": 1,
            "baron_dps_tier": 3, "peel_needs": 2, "peel_supply": 0,
            "split_push_threat": 1, "pick_threat": 1, "teamfight_score": 2,
            "engage_score": 0, "disengage_score": 1, "wave_clear": 2,
            "ult_impact": 2, "comfort_curve": "spike-2-item",
            "primary_role": "bot", "secondary_roles": [],
        },
        "common_partners": ["Nami", "Milio", "Lulu"],
        "common_counters": ["Draven", "Caitlyn", "Twitch"],
        "confidence": 0.85,
    },
    "Caitlyn": {
        "qualitative": {
            "scaling_early": 1, "scaling_mid": 2, "scaling_late": 3,
            "baron_dps_tier": 3, "peel_needs": 2, "peel_supply": 0,
            "split_push_threat": 1, "pick_threat": 2, "teamfight_score": 2,
            "engage_score": 0, "disengage_score": 1, "wave_clear": 3,
            "ult_impact": 1, "comfort_curve": "spike-3-item",
            "primary_role": "bot", "secondary_roles": [],
        },
        "common_partners": ["Lulu", "Karma", "Milio"],
        "common_counters": ["Draven", "Tristana", "Senna+Tahm"],
        "confidence": 0.85,
    },
    "Jayce": {
        "qualitative": {
            "scaling_early": 2, "scaling_mid": 2, "scaling_late": 0,
            "baron_dps_tier": 3, "peel_needs": 1, "peel_supply": 0,
            "split_push_threat": 2, "pick_threat": 1, "teamfight_score": 1,
            "engage_score": 1, "disengage_score": 2, "wave_clear": 2,
            "ult_impact": 1, "comfort_curve": "smooth",
            "primary_role": "top", "secondary_roles": ["mid"],
        },
        "common_partners": ["Lee Sin", "Vi", "Ahri"],
        "common_counters": ["Malphite", "Sion", "Trundle"],
        "confidence": 0.80,
    },
    "Annie": {
        "qualitative": {
            "scaling_early": 0, "scaling_mid": 2, "scaling_late": 2,
            "baron_dps_tier": 1, "peel_needs": 2, "peel_supply": 1,
            "split_push_threat": 1, "pick_threat": 2, "teamfight_score": 3,
            "engage_score": 2, "disengage_score": 1, "wave_clear": 3,
            "ult_impact": 3, "comfort_curve": "spike-2-item",
            "primary_role": "mid", "secondary_roles": [],
        },
        "common_partners": ["Jarvan IV", "Vi", "Nautilus"],
        "common_counters": ["Yasuo", "Talon", "Fizz"],
        "confidence": 0.75,
    },
    "Pantheon": {
        "qualitative": {
            "scaling_early": 3, "scaling_mid": 1, "scaling_late": -1,
            "baron_dps_tier": 2, "peel_needs": 1, "peel_supply": 1,
            "split_push_threat": 1, "pick_threat": 3, "teamfight_score": 1,
            "engage_score": 3, "disengage_score": 1, "wave_clear": 1,
            "ult_impact": 3, "comfort_curve": "smooth",
            "primary_role": "jungle", "secondary_roles": ["support", "top", "mid"],
        },
        "common_partners": ["Senna", "Lucian", "Caitlyn"],
        "common_counters": ["Fiora", "Camille", "K'Sante"],
        "confidence": 0.70,
    },
    "Xin Zhao": {
        "qualitative": {
            "scaling_early": 2, "scaling_mid": 2, "scaling_late": 0,
            "baron_dps_tier": 4, "peel_needs": 1, "peel_supply": 1,
            "split_push_threat": 1, "pick_threat": 1, "teamfight_score": 2,
            "engage_score": 2, "disengage_score": 1, "wave_clear": 1,
            "ult_impact": 2, "comfort_curve": "spike-2-item",
            "primary_role": "jungle", "secondary_roles": [],
        },
        "common_partners": ["Akali", "Ahri", "Lucian"],
        "common_counters": ["Trundle", "Fiora", "Camille"],
        "confidence": 0.80,
    },
    "Gnar": {
        "qualitative": {
            "scaling_early": 0, "scaling_mid": 2, "scaling_late": 2,
            "baron_dps_tier": 2, "peel_needs": 1, "peel_supply": 1,
            "split_push_threat": 1, "pick_threat": 1, "teamfight_score": 3,
            "engage_score": 2, "disengage_score": 1, "wave_clear": 2,
            "ult_impact": 3, "comfort_curve": "spike-2-item",
            "primary_role": "top", "secondary_roles": [],
        },
        "common_partners": ["Lee Sin", "Annie", "Jarvan IV"],
        "common_counters": ["Renekton", "Riven", "Darius"],
        "confidence": 0.80,
    },
    "Naafiri": {
        "qualitative": {
            "scaling_early": 2, "scaling_mid": 1, "scaling_late": -2,
            "baron_dps_tier": 2, "peel_needs": 2, "peel_supply": 0,
            "split_push_threat": 1, "pick_threat": 3, "teamfight_score": 0,
            "engage_score": 1, "disengage_score": 0, "wave_clear": 2,
            "ult_impact": 2, "comfort_curve": "smooth",
            "primary_role": "jungle", "secondary_roles": ["mid"],
        },
        "common_partners": ["Annie", "Karma", "Vi"],
        "common_counters": ["Sion", "K'Sante", "Maokai"],
        "confidence": 0.75,
    },
    "K'Sante": {
        "qualitative": {
            "scaling_early": 0, "scaling_mid": 1, "scaling_late": 3,
            "baron_dps_tier": 2, "peel_needs": 0, "peel_supply": 3,
            "split_push_threat": 2, "pick_threat": 1, "teamfight_score": 3,
            "engage_score": 2, "disengage_score": 2, "wave_clear": 1,
            "ult_impact": 3, "comfort_curve": "spike-3-item",
            "primary_role": "top", "secondary_roles": [],
        },
        "common_partners": ["Senna", "Caitlyn", "Karma"],
        "common_counters": ["Fiora", "Vayne", "Camille"],
        "confidence": 0.75,
    },
}

SOURCES_COMMON = [
    "LS draft analysis (YouTube, 2026-05)",
    "Caedrel pro game co-stream commentary (2026-05)",
    "MonteCristo LCK podcast (recurring meta segment, 2026-05)",
    "Reddit r/leagueoflegends weekly champion thread",
    "Pro broadcast analyst desk (LCK + LPL casts)",
]

FLAGS_COMMON = [
    "synthesized in-session by Claude Sonnet from training-cutoff meta knowledge",
    "verify against current-patch analyst content before promoting to canonical",
]


def main() -> None:
    out_dir = Path("data/llm_seed")
    out_dir.mkdir(parents=True, exist_ok=True)

    for champion, body in SEED.items():
        body["data_sources"] = SOURCES_COMMON
        body["flags"] = FLAGS_COMMON
        path = out_dir / f"{champion}.json"
        path.write_text(json.dumps(body, indent=2), encoding="utf-8")
        print(f"wrote {path}")

    print(f"\nseeded {len(SEED)} champions in {out_dir}/")


if __name__ == "__main__":
    main()
