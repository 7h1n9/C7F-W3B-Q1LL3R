"""Guardrails that make a bad metric a code error instead of a habit.

Three decisions in this project were made on wrong numbers, and each one has a
mechanical fix:

1. **Counting a feature no production path ever called.**  ``assert_feature_live``
   compares the last observed event with the mtime of the code that is supposed
   to emit it.  Newer code than data means the feature never executed.
2. **Two hours of analysis before noticing the code had not run.**  Same check,
   now as a hard precondition: the only way past it is a loud failure.
3. **Reading 4/122 vs 127/142 as a causal effect.**  ``format_comparison``
   prints numerators, denominators, both rates and the ratios every time, and
   appends a selection-bias warning that cannot be switched off -- only
   explicitly overridden with a recorded reason.

The judging logic is pure: every function that decides something takes already
measured values (timestamps, counts) and returns a value.  Only ``snapshot``,
``append_snapshot`` and ``file_mtime_iso`` touch the filesystem.  That split is
what lets the tests pin fixed timestamps instead of racing the clock, and it is
why ``scripts/assert_feature_live.py`` is a thin shell and not a second
implementation of the rules.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

#: Where snapshots land by default.  ``data/logs/`` is git-ignored: these rows
#: are evidence, not source.
DEFAULT_SNAPSHOT_LOG = Path("data/logs/metrics-snapshots.jsonl")

#: A comparison whose two denominators differ by more than this factor is
#: usually two different populations wearing the same axis label.
DEFAULT_SAMPLE_RATIO_THRESHOLD = 3.0

#: Below this, a rate is one or two events away from flipping.  Ten is not a
#: statistical threshold, it is the point where eyeballing stops being absurd.
DEFAULT_MIN_DENOMINATOR = 10

#: The 4/122 vs 127/142 incident: the denominators were fine (1.16x), the
#: *numerators* differed by 31.8x.  When the grouping variable was measured
#: after the outcome, that asymmetry is the bias, not the effect.
DEFAULT_NUMERATOR_RATIO_THRESHOLD = 3.0

_UTC = timezone.utc


class MetricsGuardError(RuntimeError):
    """Base class: the guardrail fired and the caller must not report a number."""

    code = "METRICS_GUARD"


class MetricsDataUnavailable(MetricsGuardError):
    """Nothing was measured, so nothing can be concluded (not the same as zero)."""

    code = "METRICS_DATA_UNAVAILABLE"


class FeatureNeverRan(MetricsGuardError):
    """The code is newer than the last event it was supposed to produce."""

    code = "FEATURE_NEVER_RAN"


class PreregistrationIncomplete(MetricsGuardError):
    """The experiment file is missing a locked metric or decision rule."""

    code = "PREREGISTRATION_INCOMPLETE"


class ComparisonBiasRisk(MetricsGuardError):
    """Raised by ``require_causal_comparison`` when a biased comparison is used."""

    code = "COMPARISON_BIAS_RISK"


# --------------------------------------------------------------------------
# 1. Traceable snapshots
# --------------------------------------------------------------------------


def _as_utc(value: datetime) -> datetime:
    """Naive timestamps are treated as UTC; MySQL DATETIME columns come back naive."""

    if value.tzinfo is None:
        return value.replace(tzinfo=_UTC)
    return value.astimezone(_UTC)


def _iso(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _display_width(text: Any) -> int:
    """East-Asian wide characters occupy two terminal cells."""

    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in str(text))


def _pad(text: Any, width: int, *, right: bool = False) -> str:
    gap = " " * max(0, width - _display_width(text))
    return gap + str(text) if right else str(text) + gap


def file_mtime_iso(path: str | Path) -> str:
    """mtime of the code under test, as an ISO-8601 UTC string."""

    mtime = Path(path).stat().st_mtime
    return _iso(datetime.fromtimestamp(mtime, tz=_UTC))


@dataclass(frozen=True)
class MetricSnapshot:
    """One row of provenance: everything needed to recompute a reported number."""

    query_id: str
    sql_or_source: str
    filters: dict[str, Any]
    code_mtime: str | None
    data_window: Any
    computed_at: str
    result: Any = field(default=None)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def snapshot(
    *,
    sql_or_source: str,
    result: Any,
    filters: Mapping[str, Any] | None = None,
    data_window: Any = "all",
    code_mtime: str | datetime | None = None,
    code_path: str | Path | None = None,
    query_id: str | None = None,
    computed_at: datetime | None = None,
    path: str | Path | None = DEFAULT_SNAPSHOT_LOG,
) -> MetricSnapshot:
    """Build a provenance row and append it to ``path`` as JSONL.

    ``sql_or_source`` and ``code_mtime`` are mandatory.  A snapshot without the
    query text cannot be rerun; one without the code version cannot be told
    apart from a run of different code.  Both were missing in the incidents, so
    both raise instead of defaulting to an empty string.
    """

    if not str(sql_or_source or "").strip():
        raise ValueError("snapshot 必须记录 sql_or_source（查询语句或数据来源），否则无法重算")
    if code_mtime is None:
        if code_path is None:
            raise ValueError("snapshot 必须记录 code_mtime 或 code_path，否则无法判断数据对应哪版代码")
        code_mtime = file_mtime_iso(code_path)
    elif isinstance(code_mtime, datetime):
        code_mtime = _iso(code_mtime)

    record = MetricSnapshot(
        query_id=query_id or f"mtr-{uuid.uuid4().hex[:12]}",
        sql_or_source=str(sql_or_source),
        filters=dict(filters or {}),
        code_mtime=str(code_mtime),
        data_window=data_window,
        computed_at=_iso(computed_at or datetime.now(tz=_UTC)),
        result=result,
    )
    if path is not None:
        append_snapshot(record, path)
    return record


def append_snapshot(record: MetricSnapshot, path: str | Path = DEFAULT_SNAPSHOT_LOG) -> Path:
    """Append one JSONL line.  No locking: see the known limitations."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record.to_dict(), ensure_ascii=False, default=str)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    return target


# --------------------------------------------------------------------------
# 2. Did the feature actually run?
# --------------------------------------------------------------------------

LivenessStatus = Literal["LIVE", "NEVER_RAN", "UNKNOWN"]


@dataclass(frozen=True)
class LivenessVerdict:
    status: LivenessStatus
    event_type: str
    message: str
    hint: str
    code_path: str | None = None
    code_mtime: datetime | None = None
    last_event_at: datetime | None = None
    event_count: int | None = None
    lag_hours: float | None = None
    data_source: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "LIVE"

    def explain(self) -> str:
        lines = [f"[功能存活检查] event_type={self.event_type} status={self.status}"]
        if self.code_path:
            lines.append(f"  被测代码: {self.code_path}")
        if self.code_mtime is not None:
            lines.append(f"  代码 mtime: {_iso(self.code_mtime)}")
        if self.last_event_at is not None:
            lines.append(f"  最后事件: {_iso(self.last_event_at)}（共 {self.event_count} 条）")
        elif self.event_count == 0:
            lines.append("  最后事件: 无（0 条）")
        if self.lag_hours is not None:
            lines.append(f"  代码比最后事件新: {self.lag_hours:.1f} 小时")
        if self.data_source:
            lines.append(f"  数据源: {self.data_source}")
        lines.append(f"  结论: {self.message}")
        if self.hint:
            lines.append(f"  下一步: {self.hint}")
        return "\n".join(lines)


def judge_feature_live(
    event_type: str,
    *,
    event_times: Sequence[datetime] | None,
    code_mtime: datetime | None,
    code_path: str | None = None,
    data_source: str | None = None,
) -> LivenessVerdict:
    """Pure decision: is the last event newer than the code that emits it?

    ``event_times=None`` means "no data source could be read" and yields
    ``UNKNOWN`` -- explicitly *not* a pass, and not the same as "zero events",
    which is ``event_times=[]`` and yields ``NEVER_RAN``.
    """

    if not str(event_type or "").strip():
        raise ValueError("event_type 不能为空")

    if code_mtime is None:
        return LivenessVerdict(
            status="UNKNOWN",
            event_type=event_type,
            code_path=code_path,
            data_source=data_source,
            message="无法判定：被测代码的 mtime 不可得，任何『功能跑过』的说法都没有依据。",
            hint="确认 --code 指向真实存在的文件；不要把路径写错当成通过。",
        )

    mtime = _as_utc(code_mtime)

    if event_times is None:
        return LivenessVerdict(
            status="UNKNOWN",
            event_type=event_type,
            code_path=code_path,
            code_mtime=mtime,
            data_source=data_source,
            message="无法判定（无数据源）：没有读到任何事件表数据，既不能确认跑过，也不能断言没跑过。",
            hint="先恢复数据源（DB 3307 或离线 dump），或用 --from-json 传入已导出的事件时间；不要用空结果当通过。",
        )

    times = [_as_utc(item) for item in event_times]
    if not times:
        return LivenessVerdict(
            status="NEVER_RAN",
            event_type=event_type,
            code_path=code_path,
            code_mtime=mtime,
            event_count=0,
            data_source=data_source,
            message=f"被测代码从未产生过 {event_type} 事件（0 条）：要么功能没被调用，要么埋点缺失，不要基于此出数。",
            hint="grep 该事件名的生产调用方、确认 feature flag 已开、确认写入路径没有吞异常。",
        )

    last = max(times)
    if last > mtime:
        return LivenessVerdict(
            status="LIVE",
            event_type=event_type,
            code_path=code_path,
            code_mtime=mtime,
            last_event_at=last,
            event_count=len(times),
            data_source=data_source,
            message="通过：最后一个事件晚于代码 mtime，事件确实由当前代码产生。",
            hint="",
        )

    lag_hours = (mtime - last).total_seconds() / 3600.0
    return LivenessVerdict(
        status="NEVER_RAN",
        event_type=event_type,
        code_path=code_path,
        code_mtime=mtime,
        last_event_at=last,
        event_count=len(times),
        lag_hours=lag_hours,
        data_source=data_source,
        message=(
            f"该功能代码比最后一个事件新 {lag_hours:.1f} 小时，说明它从未执行过，"
            "不要基于此出数。"
        ),
        hint="先确认新代码在生产路径上被调用（不是只写了函数），再重跑并看到新事件后才允许汇报。",
    )


def assert_feature_live(
    event_type: str,
    *,
    event_times: Sequence[datetime] | None,
    code_mtime: datetime | str | None = None,
    code_path: str | Path | None = None,
    data_source: str | None = None,
    strict: bool = True,
) -> LivenessVerdict:
    """Strict precondition in front of any metric about a feature.

    With ``strict=True`` (the default) ``UNKNOWN`` and ``NEVER_RAN`` raise;
    "I could not tell" must never be treated as "it ran".  Returns the verdict
    so callers can log exactly why it passed.
    """

    if code_mtime is None and code_path is not None:
        code_mtime = datetime.fromtimestamp(Path(code_path).stat().st_mtime, tz=_UTC)
    elif isinstance(code_mtime, str):
        code_mtime = datetime.fromisoformat(code_mtime.replace("Z", "+00:00"))

    verdict = judge_feature_live(
        event_type,
        event_times=event_times,
        code_mtime=code_mtime,
        code_path=str(code_path) if code_path is not None else None,
        data_source=data_source,
    )
    if not strict or verdict.ok:
        return verdict
    if verdict.status == "UNKNOWN":
        raise MetricsDataUnavailable(verdict.explain(), verdict)
    raise FeatureNeverRan(verdict.explain(), verdict)


# --------------------------------------------------------------------------
# 3. Selection-bias guardrail for two-group comparisons
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ComparisonReport:
    label_a: str
    numerator_a: int
    denominator_a: int
    label_b: str
    numerator_b: int
    denominator_b: int
    rate_a: float
    rate_b: float
    denominator_ratio: float
    numerator_ratio: float | None
    warnings: tuple[str, ...]
    override: bool
    override_reason: str | None
    text: str

    @property
    def has_bias_risk(self) -> bool:
        return bool(self.warnings)

    @property
    def causal_claim_allowed(self) -> bool:
        """A biased comparison may only carry causal wording after an override."""

        return (not self.has_bias_risk) or self.override

    def __str__(self) -> str:
        return self.text


def _ratio(high: int, low: int) -> float | None:
    if low == 0:
        return None if high == 0 else float("inf")
    return high / low


def format_comparison(
    label_a: str,
    numerator_a: int,
    denominator_a: int,
    label_b: str,
    numerator_b: int,
    denominator_b: int,
    *,
    override: bool = False,
    override_reason: str | None = None,
    sample_ratio_threshold: float = DEFAULT_SAMPLE_RATIO_THRESHOLD,
    min_denominator: int = DEFAULT_MIN_DENOMINATOR,
    numerator_ratio_threshold: float = DEFAULT_NUMERATOR_RATIO_THRESHOLD,
    audit_path: str | Path | None = None,
) -> ComparisonReport:
    """Render a two-group comparison, always with its raw numbers and a bias check.

    There is no flag to hide the warning.  ``override=True`` does not silence it
    either: it keeps the text identical and adds a recorded reason, so the only
    way to talk causally about a biased table is to leave a trace saying you
    chose to.
    """

    for name, value in (
        ("numerator_a", numerator_a),
        ("numerator_b", numerator_b),
        ("denominator_a", denominator_a),
        ("denominator_b", denominator_b),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{name} 必须是非负整数，收到 {value!r}")
    if denominator_a <= 0 or denominator_b <= 0:
        raise ValueError(
            "分母必须 > 0：0/0 报出的 0% 不是『没发生』，而是『没测量』，禁止进入对比表"
        )
    if numerator_a > denominator_a or numerator_b > denominator_b:
        raise ValueError("分子不能大于分母：检查是否把两个不同口径的计数混在了同一组")

    if override and not str(override_reason or "").strip():
        raise ValueError("override=True 必须同时给出 override_reason，否则无法审计这次例外")

    rate_a = numerator_a / denominator_a
    rate_b = numerator_b / denominator_b
    denominator_ratio = max(denominator_a, denominator_b) / min(denominator_a, denominator_b)
    numerator_ratio = _ratio(max(numerator_a, numerator_b), min(numerator_a, numerator_b))

    warnings: list[str] = []
    if denominator_ratio > sample_ratio_threshold:
        warnings.append(
            f"两组样本量相差 {denominator_ratio:.1f} 倍（分母 {denominator_a} vs {denominator_b}，"
            f"阈值 {sample_ratio_threshold:.0f}x）：小样本组的一个事件就能让率翻倍"
        )
    if min(denominator_a, denominator_b) < min_denominator:
        warnings.append(
            f"存在小于 {min_denominator} 的分母（{min(denominator_a, denominator_b)}）："
            "该组的率由个位数事件决定，不能作为结论依据"
        )
    if numerator_ratio is not None and (
        numerator_ratio == float("inf") or numerator_ratio > numerator_ratio_threshold
    ):
        shown = "∞" if numerator_ratio == float("inf") else f"{numerator_ratio:.1f}"
        warnings.append(
            f"两组命中数不对称（分子 {numerator_a} vs {numerator_b}，相差 {shown} 倍，"
            f"阈值 {numerator_ratio_threshold:.0f}x）：若分组是观测到的结局划分"
            "（如 ACTIVE vs SOLVED），这个差异本身就说明分组与结局相关"
        )
    if abs(rate_a - rate_b) >= 0.5:
        warnings.append(
            f"两组率相差 {abs(rate_a - rate_b) * 100:.1f} 个百分点：先排查分组口径，"
            "再讨论效应，否则量级本身就是选择偏差的症状"
        )

    label_width = max(_display_width(label_a), _display_width(label_b))
    lines = [
        f"[对比] {label_a} vs {label_b}",
        f"  {_pad(label_a, label_width)}: {numerator_a}/{denominator_a} = {rate_a:.2%}",
        f"  {_pad(label_b, label_width)}: {numerator_b}/{denominator_b} = {rate_b:.2%}",
        f"  两组样本量(分母): {denominator_a} vs {denominator_b}，比值 {denominator_ratio:.2f}x",
        (
            "  两组命中数(分子): "
            f"{numerator_a} vs {numerator_b}，比值 "
            + ("∞" if numerator_ratio in (None, float("inf")) else f"{numerator_ratio:.2f}x")
        ),
        f"  率差: {abs(rate_a - rate_b) * 100:.1f} pp",
    ]

    if warnings:
        lines.append("")
        lines.extend(f"!! {item}" for item in warnings)
        lines.append("!! 该对比存在选择偏差风险，不可直接解读为因果")
        lines.append(
            "   护栏无法关闭；如确需在文档里使用因果措辞，必须显式 override=True 并记录原因"
        )
    else:
        lines.append("")
        lines.append(
            "   未触发选择偏差护栏（阈值：分母比 "
            f"{sample_ratio_threshold:g}x、最小分母 {min_denominator}、分子比 "
            f"{numerator_ratio_threshold:g}x）——这只说明数字没有明显失衡，不证明分组无偏"
        )

    if override:
        lines.append("")
        lines.append(f"[OVERRIDE] 护栏被显式覆盖，原因已记录: {override_reason}")

    text = "\n".join(lines)
    report = ComparisonReport(
        label_a=label_a,
        numerator_a=numerator_a,
        denominator_a=denominator_a,
        label_b=label_b,
        numerator_b=numerator_b,
        denominator_b=denominator_b,
        rate_a=rate_a,
        rate_b=rate_b,
        denominator_ratio=denominator_ratio,
        numerator_ratio=numerator_ratio,
        warnings=tuple(warnings),
        override=override,
        override_reason=override_reason,
        text=text,
    )
    if override and audit_path is not None:
        audit = {
            "kind": "comparison_override",
            "recorded_at": _iso(datetime.now(tz=_UTC)),
            "comparison": {
                "label_a": label_a,
                "numerator_a": numerator_a,
                "denominator_a": denominator_a,
                "label_b": label_b,
                "numerator_b": numerator_b,
                "denominator_b": denominator_b,
            },
            "warnings": list(report.warnings),
            "override_reason": override_reason,
        }
        target = Path(audit_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(audit, ensure_ascii=False) + "\n")
    return report


def format_comparison_text(*args: Any, **kwargs: Any) -> str:
    """Convenience wrapper when only the rendered block is needed."""

    return str(format_comparison(*args, **kwargs))


def require_causal_comparison(report: ComparisonReport) -> ComparisonReport:
    """Gate for prose that claims cause: refuses a biased report without override."""

    if not report.causal_claim_allowed:
        raise ComparisonBiasRisk(
            "该对比存在选择偏差风险，不可直接解读为因果；"
            "请改为描述性措辞，或显式 override=True 并记录原因。"
        )
    return report


# --------------------------------------------------------------------------
# 4. Preregistration check
# --------------------------------------------------------------------------

#: Dotted paths that must exist and be non-empty before an experiment may be run.
#: The instrumentation metric is not optional: fa-causal-v1 says the primary
#: metric is void if the injection never fired, so it is part of the contract.
REQUIRED_PREREGISTRATION_FIELDS: dict[str, tuple[str, ...]] = {
    "id": ("id",),
    "primary_metric": ("metrics", "primary", "name"),
    "instrumentation_metric": ("metrics", "instrumentation", "name"),
    "decision_rule": ("analysis", "decision_rule"),
}


@dataclass(frozen=True)
class PreregistrationCheck:
    experiment_id: str
    path: str
    ok: bool
    missing: tuple[str, ...]
    values: dict[str, str]
    text: str

    def __str__(self) -> str:
        return self.text


def _dig(payload: Mapping[str, Any], path: Sequence[str]) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


def load_preregistration(path: str | Path) -> dict[str, Any]:
    """Load an experiment YAML.  PyYAML is a backend dependency already."""

    import yaml

    with Path(path).open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"预注册文件不是映射结构: {path}")
    return payload


def check_preregistration(
    path: str | Path,
    *,
    strict: bool = False,
    require_instrumentation: bool = True,
) -> PreregistrationCheck:
    """Refuse an experiment whose metric or decision rule was never locked.

    ``strict=True`` raises ``PreregistrationIncomplete`` so a measurement script
    cannot accidentally run against a half-written experiment file.
    """

    target = Path(path)
    payload = load_preregistration(target)
    required = dict(REQUIRED_PREREGISTRATION_FIELDS)
    if not require_instrumentation:
        required.pop("instrumentation_metric")

    missing: list[str] = []
    values: dict[str, str] = {}
    for name, keys in required.items():
        raw = _dig(payload, keys)
        if isinstance(raw, str):
            text = " ".join(raw.split())
        elif raw is None:
            text = ""
        else:
            text = str(raw).strip()
        if not text:
            missing.append(".".join(keys))
        else:
            values[name] = text

    experiment_id = values.get("id") or target.stem
    shown = target.as_posix()
    if missing:
        text = "\n".join(
            [
                f"[预注册检查] {experiment_id} ({shown}): REFUSED",
                f"!! 缺少必需项: {', '.join(missing)}",
                "   在补齐并锁定之前，拒绝该实验出数（预注册的意义是开跑前锁定口径，"
                "跑完再补等于没有预注册）",
            ]
        )
    else:
        rule = values.get("decision_rule", "")
        if len(rule) > 120:
            rule = rule[:117] + "..."
        text = "\n".join(
            [
                f"[预注册检查] {experiment_id} ({shown}): PASS",
                f"  主指标: {values.get('primary_metric', '')}",
                f"  工具性指标: {values.get('instrumentation_metric', '')}",
                f"  判定规则: {rule}",
            ]
        )

    check = PreregistrationCheck(
        experiment_id=experiment_id,
        path=shown,
        ok=not missing,
        missing=tuple(missing),
        values=values,
        text=text,
    )
    if strict and not check.ok:
        raise PreregistrationIncomplete(text, check)
    return check


# --------------------------------------------------------------------------
# 5. Event coverage
# --------------------------------------------------------------------------


def _display_width(text: str) -> int:
    """East-Asian wide characters occupy two terminal cells."""

    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in str(text))


def _pad(text: Any, width: int, *, right: bool = False) -> str:
    gap = " " * max(0, width - _display_width(str(text)))
    return gap + str(text) if right else str(text) + gap


@dataclass(frozen=True)
class CoverageRow:
    event_type: str
    expected: int | None
    actual: int
    ok: bool


@dataclass(frozen=True)
class CoverageReport:
    rows: tuple[CoverageRow, ...]
    missing: tuple[str, ...]
    zero: tuple[str, ...]
    text: str

    @property
    def ok(self) -> bool:
        return not self.missing

    def __str__(self) -> str:
        return self.text


def coverage_report(
    expected: Mapping[str, int | None],
    actual: Mapping[str, int],
    *,
    min_expected: int = 1,
) -> CoverageReport:
    """Show, per declared event type, whether it produced anything at all.

    ``expected=0``/``None`` means "should be > 0" and is checked against
    ``min_expected``.  A type whose actual count is zero is marked ``ZERO``:
    that is the signature of a feature whose code exists but never runs.
    """

    rows: list[CoverageRow] = []
    for event_type, want in expected.items():
        got = int(actual.get(event_type, 0) or 0)
        target = min_expected if want is None else int(want)
        target = max(target, min_expected) if want in (None, 0) else target
        rows.append(CoverageRow(event_type, want, got, got >= target))

    zero = tuple(row.event_type for row in rows if row.actual == 0)
    missing = tuple(row.event_type for row in rows if not row.ok)

    width = max([_display_width("事件类型"), *(_display_width(row.event_type) for row in rows)] or [8])
    lines = [
        f"[事件覆盖率] 声明 {len(rows)} 类，未达标 {len(missing)} 类"
        + (f"，其中 0 事件 {len(zero)} 类" if zero else ""),
        f"  {_pad('事件类型', width)}  {_pad('期望', 6, right=True)}  "
        f"{_pad('实际', 6, right=True)}  状态",
    ]
    for row in rows:
        want = ">0" if row.expected in (None, 0) else str(row.expected)
        if row.actual == 0:
            state = "!! ZERO-0事件"
        elif row.ok:
            state = "OK"
        else:
            state = f"!! 低于期望({row.expected})"
        lines.append(
            f"  {_pad(row.event_type, width)}  {_pad(want, 6, right=True)}  "
            f"{_pad(row.actual, 6, right=True)}  {state}"
        )

    if zero:
        lines.append("")
        lines.append(f"!! 以下事件类型实际为 0：{', '.join(zero)}")
        lines.append("   功能产出 0 事件 = 代码可能从未被调用，先查调用方与埋点，再谈指标")
    elif missing:
        lines.append("")
        lines.append(f"!! 以下事件类型低于期望：{', '.join(missing)}")

    return CoverageReport(
        rows=tuple(rows), missing=missing, zero=zero, text="\n".join(lines)
    )
