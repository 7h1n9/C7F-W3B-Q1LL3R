-- Point the connection's claude_cli Worker at claude-deep (7e58443e) instead of
-- AP (c8b5fc1c).  AP's relay endpoint is misspelled and its model is not in the
-- upstream group, so every Worker health probe against it fails; claude-deep is
-- the config that actually solved BUU BRUTE 1 / BURP COURSE 1.
UPDATE arena_connections
SET default_worker_engines = JSON_ARRAY(
      JSON_OBJECT(
          'engine_type', 'claude_cli',
          'model_config_id', '7e58443e-b983-4876-b174-7b5358d6f77d'
      ),
      JSON_OBJECT(
          'engine_type', 'codex_cli',
          'model_config_id', '7dc8b94a-802d-41b4-bb14-dfdd721a25ed'
      )
    )
WHERE id = '064af2ee-5cb6-44b8-bb71-a868d2389d7a';

-- Re-open the four container challenges the broken roster left UNSOLVED so the
-- dispatcher picks them up again.  attempt_count is reset because those runs
-- never exercised the model at all (the worker roster was dead), so counting
-- them against MAX_ATTEMPTS would be wrong.
UPDATE arena_solve_queue
SET status = 'PENDING',
    attempt_count = 0,
    verdict = NULL,
    last_error = NULL,
    run_id = NULL,
    started_at = NULL,
    finished_at = NULL
WHERE status IN ('UNSOLVED', 'FAILED', 'TIMEOUT');

SELECT id, name, default_worker_engines FROM arena_connections;
SELECT status, COUNT(*) AS n FROM arena_solve_queue GROUP BY status;
