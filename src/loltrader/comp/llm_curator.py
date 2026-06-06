"""LLM-assisted curation of champion qualitative dimensions.

Per the design spec (§Layer 1, Track B), this module aggregates pro analyst
content (LS YouTube, Caedrel, MonteCristo, Reddit weekly threads, etc.) into
the structured qualitative dimensions of a ChampionProfile.

The backend is pluggable so we can switch between Anthropic Claude and OpenAI
GPT-4 without rewriting the prompt / validation logic. A ``manual`` backend
exists for testing and for seed data where direct synthesis is preferred over
API calls (e.g., the initial 20-champion bootstrap done in-session).

Cost target: ~$0.03 per champion at Claude Sonnet prices, ~$5 per full
170-champion patch refresh.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from loltrader.comp.profiles import (
    ChampionProfile,
    Qualitative,
    QUALITATIVE_RANGES,
    VALID_COMFORT_CURVES,
    VALID_ROLES,
    validate_profile,
)

log = logging.getLogger(__name__)

Backend = Literal["anthropic", "openai", "manual"]

# Source list passed to the LLM; analyst priority order is intentional —
# higher-up sources are weighted higher in case of disagreement.
PRO_SOURCES = [
    "LS / Last Shadow (YouTube draft analysis videos)",
    "Caedrel (YouTube co-streams of pro games)",
    "MonteCristo (Twitter and Korean podcast)",
    "Reddit r/leagueoflegends weekly meta threads",
    "Pro broadcast analyst commentary (LCK / LPL casts)",
    "Recent pro coach interviews",
]


# ---------- prompt template ---------------------------------------------


def build_prompt(
    champion: str,
    patch: str,
    league: str | None = None,
    pickrate: float | None = None,
    winrate: float | None = None,
    draft_patterns: str | None = None,
) -> str:
    """Build the prompt for one champion's qualitative profile.

    Passes the champion's pro stats and (optionally) factual draft patterns
    extracted from our local DB. The patterns include actual top teammates,
    actual lane matchups with winrates, and player-on-champion comfort —
    grounding the LLM's qualitative scoring in evidence rather than
    training-data bias (which is heavily solo-queue-weighted).

    When draft_patterns is provided, the LLM is instructed to derive
    common_partners and common_counters from the data block rather than
    guessing.
    """
    league_str = f" for the {league} region" if league else ""
    stats_str = ""
    if pickrate is not None:
        stats_str += f"- pro pickrate (recent): {pickrate*100:.1f}%\n"
    if winrate is not None:
        stats_str += f"- pro winrate (recent): {winrate*100:.1f}%\n"

    sources_str = "\n".join(f"  - {s}" for s in PRO_SOURCES)

    # Conditional draft-patterns block — only included if we have local data
    if draft_patterns:
        patterns_block = f"""
Factual pro draft data from our local DB (use this to derive synergies and counters
rather than guessing — the data is unambiguously pro, not solo queue):

{draft_patterns}

When you output ``common_partners`` and ``common_counters``, prefer champions
that appear in the data above. Only add others if you have strong reason to.
"""
    else:
        patterns_block = ""

    return f"""You are aggregating pro LoL meta analysis for patch {patch}{league_str}.

Champion to analyze: {champion}
{stats_str}{patterns_block}
For qualitative dimensions you can't infer from the data above, look up recent
analysis (last 14 days) from these sources via web_search:
{sources_str}

Output JSON conforming to this schema:

{{
  "qualitative": {{
    "scaling_early": int [-3..+3],   // strength minute 0-15
    "scaling_mid":   int [-3..+3],   // strength minute 15-25
    "scaling_late":  int [-3..+3],   // strength minute 25+
    "baron_dps_tier": int [1..5],     // single-target sustained DPS for objectives
    "peel_needs":     int [0..3],     // 0=self-sufficient, 3=needs heavy peel
    "peel_supply":    int [0..3],     // peel provided to teammates
    "split_push_threat": int [0..3],
    "pick_threat":    int [0..3],
    "teamfight_score": int [-3..+3],
    "engage_score":   int [0..3],
    "disengage_score": int [0..3],
    "wave_clear":     int [0..3],
    "ult_impact":     int [0..3],
    "comfort_curve":  "smooth" | "spike-2-item" | "spike-3-item",
    "primary_role":   "top" | "jungle" | "mid" | "bot" | "support",
    "secondary_roles": [str]   // empty list if pure single-role
  }},
  "common_partners": [str],   // 2-5 champions this works well with
  "common_counters": [str],   // 2-5 champions this struggles into
  "data_sources":   [str],    // which sources you cited
  "confidence":     float [0..1],  // your confidence in these values
  "flags":          [str]     // optional notes (e.g. "meta shift, weak data")
}}

Rules:
1. Every value MUST be in the stated range. Never output null.
2. Be specific to PRO PLAY, not solo queue. Solo queue tier lists are systematically wrong
   for pro because pro adds coordination, longer games, and intentional drafting.
3. Cite at least one source for confidence > 0.5.
4. If you genuinely cannot find current-patch info, set confidence < 0.4 and flag it.
5. Return ONLY the JSON object, no prose."""


# ---------- backends ----------------------------------------------------


@dataclass
class CurationResult:
    """One LLM curation result before merging into a ChampionProfile."""
    champion: str
    qualitative: Qualitative
    common_partners: list[str]
    common_counters: list[str]
    data_sources: list[str]
    confidence: float
    flags: list[str]
    cost_usd: float = 0.0


def _parse_response(champion: str, response_text: str) -> CurationResult:
    """Parse the LLM's JSON response and coerce into a CurationResult.

    Raises ValueError if the response isn't valid JSON or doesn't match the
    expected schema. Range-clamps integer dimensions defensively — we'd rather
    accept a slightly-out-of-range value than fail an entire patch refresh.
    """
    # Tolerate markdown fences like ```json ... ```
    txt = response_text.strip()
    if txt.startswith("```"):
        first_nl = txt.find("\n")
        last_fence = txt.rfind("```")
        txt = txt[first_nl + 1:last_fence].strip()
    data = json.loads(txt)

    q_raw = data.get("qualitative") or {}
    q: dict[str, Any] = {}
    for field_name, (lo, hi) in QUALITATIVE_RANGES.items():
        v = q_raw.get(field_name, 0)
        try:
            v = int(v)
        except (TypeError, ValueError):
            v = 0
        q[field_name] = max(lo, min(hi, v))

    comfort_curve = q_raw.get("comfort_curve", "smooth")
    if comfort_curve not in VALID_COMFORT_CURVES:
        comfort_curve = "smooth"
    primary_role = q_raw.get("primary_role", "mid")
    if primary_role not in VALID_ROLES:
        primary_role = "mid"
    secondary_roles = [r for r in (q_raw.get("secondary_roles") or [])
                       if r in VALID_ROLES and r != primary_role]

    qualitative = Qualitative(
        **q,
        comfort_curve=comfort_curve,
        primary_role=primary_role,
        secondary_roles=secondary_roles,
    )

    confidence = float(data.get("confidence", 0.5))
    confidence = max(0.0, min(1.0, confidence))

    return CurationResult(
        champion=champion,
        qualitative=qualitative,
        common_partners=list(data.get("common_partners") or []),
        common_counters=list(data.get("common_counters") or []),
        data_sources=list(data.get("data_sources") or []),
        confidence=confidence,
        flags=list(data.get("flags") or []),
    )


def _call_anthropic(
    prompt: str,
    api_key: str,
    model: str = "claude-sonnet-4-6",
    enable_web_search: bool = False,
    max_searches: int = 3,
) -> tuple[str, float]:
    """Call Anthropic API; returns (response_text, usd_cost).

    When ``enable_web_search`` is True, the model can use Anthropic's
    server-side web search tool to look up current-patch content. This is
    important for champions released after the training cutoff (e.g.,
    Ambessa, Aurora, Yunara) and for current-patch meta shifts that the
    model wouldn't otherwise know about.

    Web search adds ~$0.01 per search to the cost, and the model typically
    issues 1-3 searches per champion when enabled.
    """
    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError(
            "Anthropic backend requires the `anthropic` package. "
            "Install with: pip install anthropic"
        ) from e

    client = anthropic.Anthropic(api_key=api_key)
    create_kwargs: dict = {
        "model": model,
        "max_tokens": 4000 if enable_web_search else 2000,
        "messages": [{"role": "user", "content": prompt}],
    }
    if enable_web_search:
        create_kwargs["tools"] = [{
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": max_searches,
        }]

    msg = client.messages.create(**create_kwargs)

    # When tools are involved, the response can have multiple content blocks:
    # tool_use blocks for searches and text blocks for prose. We want the
    # final assistant text — typically the last text block.
    text_chunks = [
        b.text for b in msg.content  # type: ignore[attr-defined]
        if getattr(b, "type", None) == "text" and getattr(b, "text", None)
    ]
    if not text_chunks:
        raise RuntimeError(f"Anthropic returned no text content for prompt")
    text = text_chunks[-1]

    # Token-based cost. Sonnet 4.6: $3/M input, $15/M output. Web search adds
    # ~$0.01/search via server_tool_use, billed in addition. Estimate
    # conservatively: count search invocations in the response.
    in_tokens = msg.usage.input_tokens
    out_tokens = msg.usage.output_tokens
    token_cost = (in_tokens * 3.0 + out_tokens * 15.0) / 1_000_000
    n_searches = sum(
        1 for b in msg.content  # type: ignore[attr-defined]
        if getattr(b, "type", None) == "server_tool_use"
    )
    search_cost = n_searches * 0.01
    return text, token_cost + search_cost


def _call_openai(prompt: str, api_key: str, model: str = "gpt-4o-mini") -> tuple[str, float]:
    """Call OpenAI API; returns (response_text, usd_cost)."""
    try:
        import openai
    except ImportError as e:
        raise RuntimeError(
            "OpenAI backend requires the `openai` package. "
            "Install with: pip install openai"
        ) from e

    client = openai.OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=model,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.choices[0].message.content or ""
    in_tokens = resp.usage.prompt_tokens
    out_tokens = resp.usage.completion_tokens
    # gpt-4o-mini pricing
    cost = (in_tokens * 0.15 + out_tokens * 0.6) / 1_000_000
    return text, cost


# ---------- public curator -----------------------------------------------


class LLMCurator:
    """Drives per-champion LLM curation with cost tracking and validation."""

    def __init__(
        self,
        backend: Backend = "anthropic",
        api_key: str | None = None,
        model: str | None = None,
        manual_provider: Callable[[str, str], str] | None = None,
        enable_web_search: bool = False,
    ) -> None:
        self.backend = backend
        self.api_key = api_key or os.environ.get(
            "ANTHROPIC_API_KEY" if backend == "anthropic" else "OPENAI_API_KEY"
        )
        self.model = model
        self.manual_provider = manual_provider
        self.enable_web_search = enable_web_search
        self.total_cost_usd = 0.0
        self.call_count = 0

        if backend != "manual" and not self.api_key:
            raise RuntimeError(
                f"Backend '{backend}' requires an API key (set env var or pass api_key=)"
            )
        if enable_web_search and backend != "anthropic":
            raise RuntimeError(
                "Web search is only supported with the anthropic backend"
            )

    def curate_one(
        self,
        champion: str,
        patch: str,
        league: str | None = None,
        pickrate: float | None = None,
        winrate: float | None = None,
        draft_patterns: str | None = None,
    ) -> CurationResult:
        """Run the LLM on one champion and return the parsed result."""
        prompt = build_prompt(champion, patch, league, pickrate, winrate, draft_patterns)

        if self.backend == "manual":
            if not self.manual_provider:
                raise RuntimeError("Manual backend requires manual_provider callable")
            text = self.manual_provider(champion, prompt)
            cost = 0.0
        elif self.backend == "anthropic":
            text, cost = _call_anthropic(
                prompt, self.api_key,
                model=self.model or "claude-sonnet-4-6",
                enable_web_search=self.enable_web_search,
            )
        elif self.backend == "openai":
            text, cost = _call_openai(prompt, self.api_key,
                                       self.model or "gpt-4o-mini")
        else:
            raise ValueError(f"Unknown backend: {self.backend}")

        self.total_cost_usd += cost
        self.call_count += 1
        log.info("curated %s ($%.3f, total $%.2f)", champion, cost, self.total_cost_usd)

        return _parse_response(champion, text)


def result_to_profile(
    result: CurationResult,
    patch: str,
    last_updated: str,
) -> ChampionProfile:
    """Lift a CurationResult into a full ChampionProfile (without pro_stats —
    that gets merged in separately by Phase 1.2's merge_into_profiles)."""
    p = ChampionProfile(
        name=result.champion,
        patch=patch,
        qualitative=result.qualitative,
        common_partners=result.common_partners,
        common_counters=result.common_counters,
        data_sources=result.data_sources,
        confidence=result.confidence,
        last_updated=last_updated,
        validation_flags=result.flags,
    )
    validate_profile(p)
    return p


def load_draft_profiles(path: str | Path) -> dict[str, ChampionProfile]:
    """Load LLM-draft profiles for review. Same format as champion_profiles.json
    but kept in a separate file so manual review can happen before promotion."""
    from loltrader.comp.profiles import load_profiles
    return load_profiles(path)
