# Cleansing Report

- Input:   `data\winprob_v2.parquet`
- Output:  `data\winprob_v2_clean_moderate.parquet`
- Coverage mode: **moderate**

## Summary
- Input games:  677
- Output games: 673
- Input rows:   116,319
- Output rows:  19,666
- Drop rate (games): 0.6%

## Game-level drops
- Too short (<15 min):     4
- Too long (>60 min):      0
- Low frame count:         0
- Missing minute coverage: 0
- No picks resolved:       0
- Ambiguous winner:        0
- Missing champion:        0

## Row-level drops (within kept games)
- Minute out of range: 0
- Impossible state:    0
- Duplicates:          96499

## Sample weight adjustments
- Low-confidence rows (0.5x weight): 0
