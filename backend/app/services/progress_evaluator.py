import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from app.models.challenge import Challenge
from app.models.run import Artifact, Observation, SolveRun
from app.services.skill_router import skill_router
from app.services.solver_state import solver_state_service


@dataclass
class NoveltyResult:
    is_novel: bool
    novel_fact_keys: list[str] = field(default_factory=list)
    duplicate_fact_keys: list[str] = field(default_factory=list)
    hypothesis_changed: bool = False
    flag_candidate_added: bool = False
    new_resource_discovered: bool = False
    novelty_score: int = 0
    reason: str = ""
    evidence_fingerprint: str = ""
    normalized_facts: dict[str, Any] = field(default_factory=dict)


class EvidenceNoveltyEvaluator:
    _TRACKED_FACTS = {
        "final_url",
        "status_code",
        "content_type",
        "redirect_history",
        "cookie_names",
        "json_keys",
        "suspected_flags",
        "suspected_credentials",
        "selected_headers",
        "body_length",
        "forms",
        "links",
        "tables",
        "files",
        "parameters",
        "error_signature",
        "technology",
    }

    @staticmethod
    def _canonical(value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): EvidenceNoveltyEvaluator._canonical(value[key]) for key in sorted(value)}
        if isinstance(value, list):
            return [EvidenceNoveltyEvaluator._canonical(item) for item in value[:50]]
        if isinstance(value, str):
            return value.strip()[:2000]
        return value

    def _extract(self, tool_name: str, arguments: dict, facts: dict) -> dict[str, Any]:
        view = facts.get("tool_model_view") if isinstance(facts.get("tool_model_view"), dict) else {}
        extracted = view.get("extracted_facts") if isinstance(view.get("extracted_facts"), dict) else {}
        source = extracted or facts
        normalized = {
            "tool_name": tool_name,
            "arguments": self._canonical(arguments),
        }
        for key in self._TRACKED_FACTS:
            if key in source and source[key] not in (None, "", [], {}):
                normalized[key] = self._canonical(source[key])
        if facts.get("flag_candidate_count"):
            normalized["flag_candidate_count"] = facts.get("flag_candidate_count")
        if facts.get("artifact_type"):
            normalized["artifact_type"] = facts.get("artifact_type")
        return normalized

    @staticmethod
    def _fingerprint(facts: dict[str, Any]) -> str:
        raw = json.dumps(facts, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()

    async def evaluate(
        self,
        previous_state,
        tool_name: str,
        arguments: dict,
        model_view: dict,
        observation: Observation,
    ) -> NoveltyResult:
        facts = dict(observation.facts_json or {})
        if model_view:
            facts["tool_model_view"] = model_view
        normalized = self._extract(tool_name, arguments, facts)
        fingerprint = self._fingerprint(normalized)
        previous_confirmed = previous_state.confirmed_facts_json if previous_state else []
        previous_rejected = previous_state.rejected_paths_json if previous_state else []
        previous_fingerprints = {
            str(item.get("evidence_fingerprint"))
            for item in [*previous_confirmed, *previous_rejected]
            if isinstance(item, dict) and item.get("evidence_fingerprint")
        }
        if fingerprint in previous_fingerprints:
            return NoveltyResult(
                is_novel=False,
                duplicate_fact_keys=sorted(normalized.keys()),
                novelty_score=0,
                reason="Evidence fingerprint already exists for this run.",
                evidence_fingerprint=fingerprint,
                normalized_facts=normalized,
            )

        known_values: dict[str, set[str]] = {}
        for entry in previous_confirmed:
            if not isinstance(entry, dict):
                continue
            entry_facts = entry.get("facts") if isinstance(entry.get("facts"), dict) else {}
            for key, value in entry_facts.items():
                known_values.setdefault(key, set()).add(
                    json.dumps(self._canonical(value), ensure_ascii=False, sort_keys=True, default=str)
                )

        novel_keys: list[str] = []
        duplicate_keys: list[str] = []
        for key, value in normalized.items():
            signature = json.dumps(self._canonical(value), ensure_ascii=False, sort_keys=True, default=str)
            if signature in known_values.get(key, set()):
                duplicate_keys.append(key)
            else:
                novel_keys.append(key)

        flag_candidate_added = bool(normalized.get("flag_candidate_count") or normalized.get("suspected_flags"))
        new_resource_discovered = any(key in novel_keys for key in ("final_url", "redirect_history", "files", "links", "forms", "tables", "parameters"))
        novelty_score = len(novel_keys) * 10
        if flag_candidate_added:
            novelty_score += 50
        if new_resource_discovered:
            novelty_score += 20
        is_novel = flag_candidate_added or new_resource_discovered or novelty_score >= 20
        return NoveltyResult(
            is_novel=is_novel,
            novel_fact_keys=sorted(novel_keys),
            duplicate_fact_keys=sorted(duplicate_keys),
            flag_candidate_added=flag_candidate_added,
            new_resource_discovered=new_resource_discovered,
            novelty_score=novelty_score,
            reason="New structured evidence was found." if is_novel else "Tool completed without new structured evidence.",
            evidence_fingerprint=fingerprint,
            normalized_facts=normalized,
        )


class ProgressEvaluator:
    def __init__(self) -> None:
        self.novelty = EvidenceNoveltyEvaluator()

    async def evaluate(
        self,
        session,
        run: SolveRun,
        challenge: Challenge,
        action_arguments: dict,
        tool_name: str,
        result: dict,
        observation: Observation,
        artifact: Artifact,
    ) -> dict:
        confirmed = False
        rejected = False

        state = await solver_state_service.load(session, run.id)
        model_view = result.get("model_view") if isinstance(result.get("model_view"), dict) else {}
        novelty = await self.novelty.evaluate(
            state,
            tool_name,
            action_arguments,
            model_view,
            observation,
        )
        facts = dict(novelty.normalized_facts or observation.facts_json or {})
        facts["tool_name"] = tool_name
        facts["artifact_path"] = artifact.file_path
        facts["artifact_type"] = artifact.artifact_type
        facts["evidence_fingerprint"] = novelty.evidence_fingerprint

        fact_entry = {
            "source": tool_name,
            "challenge_type": challenge.challenge_type,
            "status": result.get("status"),
            "facts": facts,
            "evidence_fingerprint": novelty.evidence_fingerprint,
            "novel_fact_keys": novelty.novel_fact_keys,
            "novelty_score": novelty.novelty_score,
        }
        if novelty.is_novel:
            confirmed = await solver_state_service.record_confirmation(
                session, run.id, fact_entry
            )
        else:
            rejected = await solver_state_service.record_rejected_path(
                session,
                run.id,
                {
                    "source": tool_name,
                    "reason": novelty.reason or str(result.get("error") or result.get("summary") or "unknown"),
                    "facts": facts,
                    "evidence_fingerprint": novelty.evidence_fingerprint,
                    "classification": "NEGATIVE" if result.get("status") == "COMPLETED" else "BLOCKED",
                },
            )

        recommendations = []
        if confirmed:
            recommendations = await skill_router.recommend_from_observation(
                session,
                run.id,
                challenge.challenge_type,
                {
                    "summary": observation.summary,
                    "facts_json": facts,
                    "tool_name": tool_name,
                    "observation_id": observation.id,
                    "artifact_path": artifact.file_path,
                    "evidence_fingerprint": novelty.evidence_fingerprint,
                },
            )
        extracted = facts.get("tool_model_view", {}).get("extracted_facts", {}) if isinstance(facts.get("tool_model_view"), dict) else facts
        if extracted.get("sql_syntax_signal") and not extracted.get("sql_injection_confirmed"):
            recommendations.append({"tool_name": "sql_injection_probe", "reason": "SQL syntax signal requires a bounded boolean probe."})
        if extracted.get("sql_injection_confirmed"):
            recommendations.append({"tool_name": "sql_union_probe", "reason": "Confirmed boolean differential enables a bounded UNION probe."})
        await solver_state_service.sync_hypotheses(session, run.id)
        made_progress = confirmed
        no_progress_count = await solver_state_service.record_progress(
            session, run.id, made_progress
        )
        return {
            "confirmed": confirmed,
            "rejected": rejected,
            "recommended_skills": recommendations,
            "made_progress": made_progress,
            "no_progress_count": no_progress_count,
            "novelty": {
                "is_novel": novelty.is_novel,
                "novel_fact_keys": novelty.novel_fact_keys,
                "duplicate_fact_keys": novelty.duplicate_fact_keys,
                "flag_candidate_added": novelty.flag_candidate_added,
                "new_resource_discovered": novelty.new_resource_discovered,
                "novelty_score": novelty.novelty_score,
                "reason": novelty.reason,
                "evidence_fingerprint": novelty.evidence_fingerprint,
            },
        }


progress_evaluator = ProgressEvaluator()
