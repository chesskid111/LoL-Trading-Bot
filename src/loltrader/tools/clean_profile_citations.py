"""Strip hallucinated analyst citations from champion profile validation flags.

The Phase 1.4 LLM curator named specific analysts (LS, Caedrel, MonteCristo,
IWillDominate, etc.) in validation flags as if they were sources it had
consulted. In reality, it had only web-search snippets and training-data
priors — it could not access stream content, transcripts, or co-stream
commentary. The named citations were aspirational, not real.

This tool:
  1. Removes flags that consist mostly of "No <analyst> content found"-style
     non-information.
  2. Replaces flags that hallucinate citations like "confirmed by <analyst>"
     with an honest provenance note.
  3. Leaves intact: legitimate statistical sources (gol.gg, lolvvv, OP.GG),
     legitimate observations (counter-pick patterns, ban-rate trends),
     legitimate priors derived from training data without false attribution.

Idempotent — running multiple times produces the same result.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Names the LLM was found to fabricate citations for
HALLUCINATED_NAMES = [
    r"\bLS\b",                # Last Shadow
    r"\bCaedrel\b",
    r"\bMonteCristo\b",
    r"\bMonte\s+Cristo\b",
    r"\bIWillDominate\b",
    r"\bYamato\b",
    r"\bCoreJJ\b",
    r"\bBjergsen\b",
    r"\bReapered\b",
    r"\bTafokints\b",
]

# Flag types to handle
ABSENCE_PATTERNS = [
    re.compile(r"no .*(?:" + "|".join(HALLUCINATED_NAMES) + r").*(?:found|content)", re.IGNORECASE),
    re.compile(r"no patch-specific.*(?:" + "|".join(HALLUCINATED_NAMES) + r")", re.IGNORECASE),
    re.compile(r"(?:" + "|".join(HALLUCINATED_NAMES) + r").*(?:not found|unavailable)", re.IGNORECASE),
]

# If a flag mentions ≥2 of the hallucinated names, it's almost certainly the
# template "no LS/Caedrel/MonteCristo content found" pattern — purge entirely.
def has_multiple_names(flag: str) -> bool:
    hits = 0
    for pat in HALLUCINATED_NAMES:
        if re.search(pat, flag, re.IGNORECASE):
            hits += 1
            if hits >= 2:
                return True
    return False

CONFIRMATION_PATTERNS = [
    re.compile(r"confirmed by .*(?:" + "|".join(HALLUCINATED_NAMES) + r")", re.IGNORECASE),
    re.compile(r"(?:per|according to) (?:" + "|".join(HALLUCINATED_NAMES) + r")", re.IGNORECASE),
    re.compile(r"(?:" + "|".join(HALLUCINATED_NAMES) + r").* (?:says|stated|noted|analysis)", re.IGNORECASE),
]


def is_absence_flag(flag: str) -> bool:
    """Flag is just 'we couldn't find <analyst> content' — pure non-information."""
    if any(p.search(flag) for p in ABSENCE_PATTERNS):
        return True
    # Multiple name mentions = template absence flag, regardless of phrasing
    return has_multiple_names(flag)


def is_hallucinated_citation(flag: str) -> bool:
    """Flag claims to cite a named analyst that the LLM couldn't actually consume."""
    return any(p.search(flag) for p in CONFIRMATION_PATTERNS)


def clean_flag(flag: str) -> str | None:
    """Return cleaned flag text, or None to delete the flag entirely."""
    if is_absence_flag(flag):
        return None
    if is_hallucinated_citation(flag):
        # Strip the citation; keep the substantive claim if any
        for pat in HALLUCINATED_NAMES:
            flag = re.sub(pat, "[name removed]", flag, flags=re.IGNORECASE)
        # If after stripping, the flag is mostly placeholder text, drop it
        if "[name removed]" in flag and len(flag.replace("[name removed]", "").strip()) < 30:
            return None
        return flag
    return flag


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--profile-path", default="data/champion_profiles.json")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would change without writing")
    args = p.parse_args(argv)

    path = Path(args.profile_path)
    profiles = json.loads(path.read_text(encoding="utf-8"))

    n_flags_before = 0
    n_flags_after = 0
    n_champs_changed = 0
    n_absence_dropped = 0
    n_citation_stripped = 0

    for name, p in profiles.items():
        flags = p.get("validation_flags", [])
        n_flags_before += len(flags)
        new_flags = []
        changed = False
        for flag in flags:
            if is_absence_flag(flag):
                n_absence_dropped += 1
                changed = True
                continue
            if is_hallucinated_citation(flag):
                cleaned = clean_flag(flag)
                if cleaned is None:
                    n_absence_dropped += 1
                    changed = True
                    continue
                else:
                    n_citation_stripped += 1
                    changed = True
                    new_flags.append(cleaned)
                    continue
            new_flags.append(flag)
        if changed:
            n_champs_changed += 1
            p["validation_flags"] = new_flags
        n_flags_after += len(new_flags)

    print(f"Champions touched:       {n_champs_changed}")
    print(f"Total flags before:      {n_flags_before}")
    print(f"Total flags after:       {n_flags_after}")
    print(f"Absence flags dropped:   {n_absence_dropped}")
    print(f"Citations stripped:      {n_citation_stripped}")

    if args.dry_run:
        print("\n(dry-run — no file written)")
        return 0

    path.write_text(json.dumps(profiles, indent=2), encoding="utf-8")
    print(f"\nwrote cleaned profiles to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
