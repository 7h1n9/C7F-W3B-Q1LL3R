-- Restore the two hand-set keys on claude-deep that a capability probe wiped.
-- route_via_proxy routes the Worker through the local protocol-translating
-- proxy; model_aliases is what lets that proxy resolve a Claude CLI model name
-- back to this config instead of falling back to the first enabled config.
UPDATE model_configs
SET capabilities_json = JSON_SET(
      COALESCE(capabilities_json, JSON_OBJECT()),
      '$.route_via_proxy', CAST('true' AS JSON),
      '$.model_aliases', JSON_OBJECT(
          'claude-opus-5', 'deepseek-v4.1-flash',
          'claude-sonnet-5', 'deepseek-v4.1-flash',
          'claude-haiku-4-5', 'deepseek-v4.1-flash',
          'claude-3-5-sonnet-latest', 'deepseek-v4.1-flash',
          'claude-3-5-haiku-latest', 'deepseek-v4.1-flash'
      )
    )
WHERE id = '7e58443e-b983-4876-b174-7b5358d6f77d';

SELECT name,
       JSON_EXTRACT(capabilities_json, '$.route_via_proxy') AS via_proxy,
       JSON_EXTRACT(capabilities_json, '$.model_aliases')    AS aliases
FROM model_configs
WHERE id = '7e58443e-b983-4876-b174-7b5358d6f77d';
