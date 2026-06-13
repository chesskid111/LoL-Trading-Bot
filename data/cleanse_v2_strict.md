# Cleansing Report

- Input:   `data\winprob_v2.parquet`
- Output:  `data\winprob_v2_clean_strict.parquet`
- Coverage mode: **strict**

## Summary
- Input games:  677
- Output games: 670
- Input rows:   116,319
- Output rows:  19,612
- Drop rate (games): 1.0%

## Game-level drops
- Too short (<15 min):     4
- Too long (>60 min):      0
- Low frame count:         0
- Missing minute coverage: 3
- No picks resolved:       0
- Ambiguous winner:        0
- Missing champion:        0

## Row-level drops (within kept games)
- Minute out of range: 0
- Impossible state:    0
- Duplicates:          96239

## Sample weight adjustments
- Low-confidence rows (0.5x weight): 0
