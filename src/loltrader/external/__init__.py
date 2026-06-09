"""External data sources (gol.gg, DPM) — ingestion + validation.

These modules handle the import of manually-extracted data from premium
analytics sites that don't have a public API. The user extracts via the
clipboard/transcription and the importers parse + validate before the
data flows into model features.

Architecture:
  data/external/{source}/{type}/*.tsv|json  — raw human-extracted data
  data/processed/*.json                      — aggregated, feature-ready
  loltrader.external.schemas                 — Pydantic validation
  loltrader.tools.import_*                   — CLI parsers
"""
