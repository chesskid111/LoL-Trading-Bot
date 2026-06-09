# External Data Sources

Manually-extracted analytics data from premium sources (gol.gg, DPM) that
don't have a public API. Used as inputs to model features.

## Directory layout

```
external/
├── gol_gg/
│   ├── synergies/           # raw TSV from gol.gg Champion Synergy clipboard
│   │   ├── pairs_*.tsv      # 2-champion synergies
│   │   └── triples_*.tsv    # 3-champion synergies
│   └── champion_stats/      # raw TSV from gol.gg Champions ranking
│       ├── top_s16.tsv
│       ├── jungle_s16.tsv
│       ├── mid_s16.tsv
│       ├── bot_s16.tsv
│       └── support_s16.tsv
└── dpm/
    ├── teams/               # one JSON per team-split, manually entered
    ├── players/             # one JSON per player-split, manually entered
    └── teams_h2h/           # head-to-head specific matchups
```

## Data flow

1. **User extracts** data from gol.gg/DPM into raw files in this directory
2. **Importer tools** in `loltrader.tools.import_*` parse + validate via
   `loltrader.external.schemas`
3. **Aggregated output** written to `data/processed/*.json`
4. **Model features** in `loltrader.winprob.state` consume the processed JSON

## Raw file naming

- gol.gg TSVs: `<type>_s<season>_<split>_<scope>.tsv`
  - Examples: `pairs_s16_spring.tsv`, `triples_s16_spring.tsv`
- DPM team JSONs: `<team_code>_<season>_<league>_<split>.json`
  - Example: `T1_2026_LCK_R1-2.json`
- DPM player JSONs: `<handle>_<season>_<league>_<split>.json`
  - Example: `Faker_2026_LCK_R1-2.json`

## Update cadence

- **Per match week** (weekly): teams + players for active rosters
- **Per patch** (~biweekly): synergies + champion stats
- **As needed**: H2H specific matchups before important games

## Update workflow

```bash
# After extracting new TSVs from gol.gg
python -m loltrader.tools.import_gol_gg_synergies \
  --input data/external/gol_gg/synergies/ \
  --output data/processed/synergies_expanded.json

# After updating DPM team data
python -m loltrader.tools.aggregate_dpm_teams \
  --input data/external/dpm/teams/ \
  --output data/processed/team_strength.json

# Then retrain
python -m loltrader.tools.build_winprob_dataset --cadence 10
python -m loltrader.tools.train_winprob
```
