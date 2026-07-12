---
name: web-ctf-methodology
display_name: Web CTF Methodology
description: Methodology skill for authorized web CTF challenges.
skill_kind: METHODOLOGY
activation_mode: AUTO
challenge_types: [WEB_TARGET]
required_tools: [http_request, file_read, file_search, python_run]
recommended_tools: [http_request, file_read, file_search, python_run]
ctf_phases: [INTAKE, BASELINE, MAPPING, HYPOTHESIS, TESTING, CHAINING, FLAG_SEARCH, FLAG_VERIFICATION, REPORTING]
triggers: [http, source, attachment, challenge, login, session, csrf, ssrf, xss, sqli]
---

# Web CTF Methodology

Start with supplied source, attachments, or target metadata. Build a minimal baseline, then test one concrete hypothesis at a time.

## Focus

- Establish the allowlisted host and any supplied source material.
- Derive the first hypothesis from visible behavior.
- Use the fewest requests that can prove or disprove the hypothesis.
- Track rejected paths and flag candidates separately.

## When the task meets resistance

- Do not keep replaying the same request if the response stops changing.
- Switch evidence sources: source code, attachment, route map, request/response history, or configuration.
- If the blocker is an auth/session boundary, map the boundary explicitly before trying payloads.
- If a path is confirmed dead, write it down and move to the next plausible route.
- If the same route keeps returning redirects or the same status code, treat it as a boundary signal and stop reissuing it unchanged.
- If automation hits a tool-usage contract error, repair the invocation or move to another evidence source before continuing the same hypothesis.
