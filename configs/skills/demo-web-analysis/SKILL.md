---
name: demo-web-analysis
description: Basic workflow for analyzing an authorized CTF Web challenge.
---

# Demo Web Analysis

## Applicable scope

Use only for a local CTF, a designated practice range, or a target explicitly included in the run allowlist.

## Initial analysis steps

Review `challenge.json`, establish the exact allowed host, and inspect supplied source or attachments before requesting the target.

## Evidence requirements

Record the source artifact, request/response identifier, and a short factual summary for every meaningful conclusion.

## Tool selection

Prefer `file_read` and `file_search` for supplied materials. Use `http_request` only for an allowlisted host. Use `python_run` only for an existing workspace script.

## Stop conditions

Stop when the target host is not allowlisted, scope is ambiguous, a tool policy blocks the action, or verification needs user input.

## Common false positives

A matching string is a candidate, not a verified flag. Error messages and source comments are hypotheses until independently corroborated.
