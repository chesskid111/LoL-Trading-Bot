"""Win-probability prediction (Layers 4 + 5 of the comp evaluation engine).

Layer 4: ``state.py`` — combines live game state (live_frames +
live_frames_details) with the comp evaluator output (Layer 3) into a flat
feature dict the model can consume.

Layer 5 (Phase 4): ``model.py`` (not built yet) — XGBoost on Match-V5
timelines, calibrated via isotonic regression, outputs P(blue wins).
"""
