-- 007_perf_indexes.sql
-- Indexes to support the v1.6 feature queries:
--   - Player-on-champion winrate: (player_id, champion) filter on
--     match_player_stats joined with matches.date
--   - Lane matchup winrate: (role, champion) filter on match_player_stats

CREATE INDEX IF NOT EXISTS idx_mps_player_champion
    ON match_player_stats(player_id, champion);

CREATE INDEX IF NOT EXISTS idx_mps_role_champion
    ON match_player_stats(role, champion);

CREATE INDEX IF NOT EXISTS idx_mps_game_team
    ON match_player_stats(game_id, team_id);

CREATE INDEX IF NOT EXISTS idx_match_drafts_game_team_isban
    ON match_drafts(game_id, team_id, is_ban);
