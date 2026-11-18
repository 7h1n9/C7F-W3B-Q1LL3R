"""Trace-driven Chinese writeup renderer for the canonical Muteki runtime."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_SENSITIVE_KEY = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|cookie)")
_FLAG_PATTERN = re.compile(r"flag\{[^{}\r\n]*\}", re.IGNORECASE)


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _safe_text(value: Any, *, limit: int = 1200) -> str:
    text = str(value or "")
    text = _FLAG_PATTERN.sub("{{verified_flag}}", text)
    text = re.sub(r"(?i)(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*[^\s,;]+", r"\1={{secret_value}}", text)
    return text[:limit]


def _display_verified_flag(value: Any, *, limit: int = 500) -> str:
    """Keep the final verified flag readable while masking it elsewhere."""

    text = str(value or "").strip()
    if _FLAG_PATTERN.fullmatch(text):
        return text[:limit]
    return _safe_text(text, limit=limit)


def _safe_argument(value: Any, *, key: str = "", target_url: str = "") -> Any:
    if _SENSITIVE_KEY.search(key):
        return "{{secret_value}}"
    if isinstance(value, Mapping):
        return {str(k): _safe_argument(v, key=str(k), target_url=target_url) for k, v in value.items()}
    if isinstance(value, list):
        return [_safe_argument(item, key=key, target_url=target_url) for item in value[:50]]
    if isinstance(value, str):
        if target_url:
            parsed_target = urlsplit(target_url)
            parsed = urlsplit(value)
            if parsed.scheme and parsed.netloc and parsed_target.netloc == parsed.netloc:
                value = urlunsplit((parsed.scheme, "{{target_host}}", parsed.path, parsed.query, parsed.fragment))
        return _FLAG_PATTERN.sub("{{flag_pattern_match}}", value[:4000])
    return value


def _result_field(result: Any, name: str, default: Any = None) -> Any:
    if isinstance(result, Mapping):
        return result.get(name, default)
    return getattr(result, name, default)


def _classification(facts: Sequence[Mapping[str, Any]]) -> str:
    for fact in facts:
        content = str(fact.get("content") or "")
        match = re.search(r"(?:classification|vulnerability_type)\s*[=:]\s*[\"']?([A-Z][A-Z_]+)", content)
        if match:
            return match.group(1)
    return "待由已验证事实确认"


_TIER_LABELS = {"simple": "简单", "medium": "中等", "complex": "复杂"}
_SEMANTIC_STATUS_LABELS = {
    "not_run": "未运行",
    "running": "进行中",
    "completed": "已完成",
    "failed": "失败",
}

# The step classifier returns stable ASCII markers; the report shows Chinese.
_STEP_KIND_LABELS = {"READ": "读取", "OBSERVE": "观察", "WRITE": "写入"}

# A fact's importance arrives as an ASCII enum; the report shows Chinese.
_IMPORTANCE_LABELS = {
    "critical": "关键",
    "high": "重要",
    "medium": "中等",
    "low": "次要",
    "info": "提示",
}


def _importance_label(importance: Any) -> str:
    """Chinese label for a fact's importance marker."""

    text = str(importance or "").strip().lower()
    return _IMPORTANCE_LABELS.get(text, str(importance or "中等"))


# Vulnerability classes are shown to readers, so each English abbreviation gets a
# Chinese gloss. The abbreviation is kept in parentheses: it is what the scanner
# emits and what a reviewer greps for, so dropping it would lose precision.
_VULN_TYPE_LABELS = {
    "SQLI": "SQL 注入",
    "SQL_INJECTION": "SQL 注入",
    "IDOR": "越权访问（IDOR）",
    "PATH_TRAVERSAL": "路径穿越",
    "CMD_INJECTION": "命令注入",
    "COMMAND_INJECTION": "命令注入",
    "SSRF": "服务端请求伪造（SSRF）",
    "XSS": "跨站脚本（XSS）",
    "SSTI": "服务端模板注入（SSTI）",
    "LFI": "本地文件包含（LFI）",
    "RFI": "远程文件包含（RFI）",
    "XXE": "XML 外部实体注入（XXE）",
    "CSRF": "跨站请求伪造（CSRF）",
    "MODEL_INVERSION": "模型反演",
    "MODEL_MEMORIZATION_EXTRACTION": "训练数据记忆提取",
    "ARBITRARY_FILE_WRITE": "任意文件写入",
    "FILE_UPLOAD": "文件上传",
    "WEAK_CREDENTIALS": "弱口令",
    "AUTH_BYPASS": "认证绕过",
    "INFO_DISCLOSURE": "信息泄露",
    "DESERIALIZATION": "反序列化",
    "LOGIC_FLAW": "业务逻辑缺陷",
    "GENERIC_WEB": "通用 Web 漏洞",
}


def _vuln_type_label(vuln_type: Any) -> str:
    """Chinese gloss for a vulnerability class, keeping the ASCII code visible.

    Handles the composite the renderer builds when the model and the heuristic
    disagree (``X（模型判定）/ Y（启发式）``) by glossing each code on its own,
    so no part of the string stays untranslated.
    """

    text = str(vuln_type or "").strip()
    if not text:
        return ""

    def _gloss(part: str) -> str:
        # Peel the "（模型判定）" / "（启发式）" annotation off before matching the
        # code, then put it back — otherwise the lookup misses on the suffix.
        match = re.match(r"^(?P<code>[^（(]+?)\s*(?P<note>[（(].*)$", part.strip())
        if not match:
            code, note = part.strip(), ""
        else:
            code, note = match.group("code").strip(), match.group("note")
        label = _VULN_TYPE_LABELS.get(code.upper())
        if not label:
            return part.strip()
        return f"{label}（{code}）{note}"

    if "（模型判定）" in text or "（启发式）" in text:
        parts = re.split(r"\s*/\s*", text)
        return " / ".join(_gloss(part) for part in parts if part.strip())
    return _gloss(text)


def _tier_label(tier: Any) -> str:
    """Chinese label for the complexity tier, keeping the raw value as a fallback."""

    text = str(tier or "").strip()
    return _TIER_LABELS.get(text, text or "未评估")


def _semantic_status_label(status: Any) -> str:
    """Chinese label for the semantic-analysis status."""

    text = str(status or "").strip() or "not_run"
    return _SEMANTIC_STATUS_LABELS.get(text, text)


def _evidence_rows(evidence: Sequence[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in evidence:
        rows.append(
            {
                "id": str(_value(item, "id", "")),
                "type": _evidence_type_label(_value(item, "evidence_type", "Evidence")),
                "status": _evidence_status_label(_value(item, "status", "")),
                "summary": _safe_text(_value(item, "summary", ""), limit=500),
                "tool_call_id": str(_value(item, "tool_call_id", "") or ""),
                "artifact_id": str(_value(item, "artifact_id", "") or ""),
            }
        )
    return rows


# Evidence type / status arrive as stable ASCII enums from the database. They are
# rendered for humans, so each known value gets a Chinese gloss; anything unknown
# passes through unchanged rather than being silently blanked.
_EVIDENCE_TYPE_LABELS = {
    "HTTP_RESPONSE": "HTTP 响应",
    "HTTP_REQUEST": "HTTP 请求",
    "MUTEKI_NATIVE_WORKER": "Muteki 原生 Worker 输出",
    "TOOL_ARTIFACT": "工具制品",
    "FILE": "文件",
    "LOG": "日志",
    "SCREENSHOT": "截图",
    "EVIDENCE": "证据",
}
_EVIDENCE_STATUS_LABELS = {
    "VERIFIED": "已验证",
    "PENDING": "待验证",
    "UNVERIFIED": "未验证",
    "REJECTED": "已否决",
    "STALE": "已过期",
    "INVALID": "无效",
}


def _evidence_type_label(evidence_type: Any) -> str:
    """Chinese gloss for an evidence type, keeping the raw enum as a fallback."""

    text = _safe_text(evidence_type, limit=120)
    return _EVIDENCE_TYPE_LABELS.get(text.strip().upper(), text)


def _evidence_status_label(status: Any) -> str:
    """Chinese gloss for an evidence status."""

    text = _safe_text(status, limit=40)
    return _EVIDENCE_STATUS_LABELS.get(text.strip().upper(), text)



def _detect_vuln_type(calls, facts):
    call_tools={str(_value(c,"tool_name","")) for c in calls}
    args_ex=[]
    for c in calls[:30]:
        args=_value(c,"normalized_arguments") or _value(c,"arguments_json") or {}
        args_ex.append(args)
    if {"sql_boolean_compare","sqlmap_detect","oracle_probe_matrix"}&call_tools: return "SQLI"
    id_pat=re.compile(r"(?i)\bid[=_](\d+)")
    id_vals=set()
    for a in args_ex:
        for v in id_pat.findall(str(a)): id_vals.add(v)
    if "http_session_request" in call_tools and len(id_vals)>=2: return "IDOR"
    for a in args_ex:
        v=str(a)
        if "../" in v or "%2e%2e" in v.lower(): return "PATH_TRAVERSAL"
    if "command_execution" in call_tools: return "CMD_INJECTION"
    for a in args_ex:
        if re.search(r"(?i)(127\\.0\\.0\\.1|localhost|169\\.254\\.169\\.254)",str(a)): return "SSRF"
    for f in facts:
        c=str(f.get("content") or "")
        m=re.search(r"(?:classification|vulnerability_type)\s*[=:]\s*['\"]?([A-Z][A-Z_]+)",c)
        if m: return m.group(1)
    return "GENERIC_WEB"

def _complexity_score(facts, calls, evidence):
    verified=[f for f in facts if _value(f,"verified") is True]
    score=len(verified)*3+len(calls)+len(evidence)
    if score<=60 and len(verified)<=5 and len(calls)<=20: return "simple",score
    if score<=200 and len(verified)<=15 and len(calls)<=50: return "medium",score
    return "complex",score

_VULN_TEMPLATES={
    "SQLI":{"recon_intro":"\u901a\u8fc7HTTP\u7aef\u70b9\u63a2\u6d4b\u53d1\u73b0\u5b58\u5728\u53c2\u6570\u5316\u67e5\u8be2\u5165\u53e3\uff0c\u5bf9\u53ef\u63a7\u53c2\u6570\u8fdb\u884c\u4e86\u5e03\u5c14\u76f2\u6ce8\u5224\u65ad\u3002","discovery":"\u4f7f\u7528\u5e03\u5c14\u76f2\u6ce8\uff08Boolean Oracle\uff09\u786e\u8ba4\u6ce8\u5165\u70b9\uff1a\u6839\u636e\u53c2\u6570\u503c\u6784\u9020true/false\u4e24\u79cd\u8868\u8fbe\u5f0f\uff0ctrue\u54cd\u5e94\u4e0efalse\u54cd\u5e94\u5b58\u5728\u53ef\u89c2\u6d4b\u5dee\u5f02\u3002","exploit":"\u901a\u8fc7\u5e03\u5c14\u76f2\u6ce8\u9010\u6b65\u679a\u4e3e\u6570\u636e\u5e93\u5185\u5bb9\uff1a\u5148\u5224\u65ad\u5f53\u524d\u5e93\u7684\u8868\u6570\u91cf\u548c\u8868\u540d\uff0c\u518d\u5b9a\u4f4d\u76ee\u6807\u8868\uff0c\u6700\u540e\u679a\u4e3e\u5b57\u6bb5\u5e76\u63d0\u53d6Flag\u503c\u3002","flag":"\u901a\u8fc7\u5e03\u5c14\u76f2\u6ce8\u679a\u4e3e\u5f97\u5230Flag\u503c\u3002"},
    "IDOR":{"recon_intro":"\u8bbf\u95ee\u76ee\u6807\u9875\u9762\u53d1\u73b0\u5b58\u5728\u5bf9\u8c61\u5f15\u7528\u53c2\u6570\uff08\u5982id\u3001user_id\u7b49\uff09\uff0c\u4e14\u670d\u52a1\u7aef\u672a\u6821\u9a8c\u5f53\u524d\u7528\u6237\u662f\u5426\u6709\u6743\u8bbf\u95ee\u8be5\u5bf9\u8c61\u3002","discovery":"\u5728\u767b\u5f55\u72b6\u6001\u4e0b\uff0c\u5c1d\u8bd5\u4fee\u6539\u8bf7\u6c42\u4e2d\u7684ID\u53c2\u6570\uff08\u9012\u589e\u6216\u66f4\u6362\u4e3a\u5176\u4ed6\u503c\uff09\uff0c\u89c2\u5bdf\u54cd\u5e94\u5185\u5bb9\u662f\u5426\u53d8\u5316\u3002","exploit":"\u901a\u8fc7\u904d\u5386ID\u503c\uff0c\u8bbf\u95ee\u5176\u4ed6\u7528\u6237\u7684\u654f\u611f\u6570\u636e\uff08\u5982\u5de5\u5355\u3001\u8ba2\u5355\u3001\u6587\u4ef6\u7b49\uff09\uff0c\u9010\u6b65\u6269\u5927\u8bbf\u95ee\u8303\u56f4\u3002","flag":"\u627e\u5230\u5305\u542bFlag\u7684\u54cd\u5e94\u5185\u5bb9\u3002"},
    "PATH_TRAVERSAL":{"recon_intro":"\u53d1\u73b0\u76ee\u6807\u5b58\u5728\u6587\u4ef6\u8bfb\u53d6\u7c7b\u63a5\u53e3\uff08\u53c2\u6570\u5305\u542bpath\u3001file\u3001dir\u3001download\u7b49\u5173\u952e\u8bcd\uff09\uff0c\u5c1d\u8bd5\u901a\u8fc7\u8def\u5f84\u7a7f\u8d8a\u8bbf\u95ee\u654f\u611f\u6587\u4ef6\u3002","discovery":"\u4f7f\u7528\u8def\u5f84\u7a7f\u8d8apayload\uff08\u5982../\u3001..%2f\u7b49\uff09\u8bfb\u53d6/etc/passwd\u3001/flag\u6216\u5176\u4ed6\u914d\u7f6e\u6587\u4ef6\uff0c\u786e\u8ba4\u5b58\u5728\u4efb\u610f\u6587\u4ef6\u8bfb\u53d6\u3002","exploit":"\u5229\u7528\u8def\u5f84\u7a7f\u8d8a\u5c3d\u91cf\u6269\u5927\u8bfb\u53d6\u8303\u56f4\uff0c\u5b9a\u4f4dFlag\u6587\u4ef6\u8def\u5f84\u5e76\u8bfb\u53d6\u5185\u5bb9\u3002","flag":"\u8bfb\u53d6Flag\u6587\u4ef6\u5185\u5bb9\u3002"},
    "CMD_INJECTION":{"recon_intro":"\u53d1\u73b0\u76ee\u6807\u5b58\u5728\u547d\u4ee4\u6267\u884c\u7c7b\u63a5\u53e3\uff0c\u53c2\u6570\u503c\u4f1a\u88ab\u62fc\u63a5\u5230\u7cfb\u7edf\u547d\u4ee4\u4e2d\u6267\u884c\u3002","discovery":"\u901a\u8fc7\u6ce8\u5165\u547d\u4ee4\u5206\u9694\u7b26\uff08;\u3001|\u3001&&\u3001$()\u7b49\uff09\u89e6\u53d1\u65f6\u95f4\u5ef6\u8fdf\u6216\u8f93\u51fa\u5dee\u5f02\uff0c\u786e\u8ba4\u5b58\u5728\u547d\u4ee4\u6ce8\u5165\u3002","exploit":"\u5229\u7528\u547d\u4ee4\u6ce8\u5165\u6267\u884c\u76ee\u6807\u547d\u4ee4\uff0c\u9010\u6b65\u63a2\u6d4b\u73af\u5883\u5e76\u5b9a\u4f4dFlag\u3002","flag":"\u901a\u8fc7\u547d\u4ee4\u6ce8\u5165\u83b7\u53d6Flag\u3002"},
    "SSRF":{"recon_intro":"\u53d1\u73b0\u76ee\u6807\u5b58\u5728URL\u53c2\u6570\uff0c\u670d\u52a1\u5668\u4f1a\u53d1\u8d77\u8bf7\u6c42\u8bbf\u95ee\u8be5URL\uff0c\u53ef\u80fd\u8bbf\u95ee\u5185\u7f51\u8d44\u6e90\u3002","discovery":"\u6784\u9020\u6307\u5411\u5185\u7f51\u5730\u5740\u7684\u8bf7\u6c42\uff08\u5982127.0.0.1\u3001169.254.169.254\uff09\uff0c\u89c2\u5bdf\u662f\u5426\u8fd4\u56de\u5185\u7f51\u670d\u52a1\u54cd\u5e94\u3002","exploit":"\u5229\u7528SSRF\u63a2\u6d4b\u5185\u7f51\u670d\u52a1\uff0c\u9010\u6b65\u6269\u5927\u8bbf\u95ee\u8303\u56f4\u76f4\u81f3\u83b7\u53d6\u654f\u611f\u4fe1\u606f\u3002","flag":"\u901a\u8fc7\u5185\u7f51\u63a2\u6d4b\u83b7\u53d6Flag\u3002"},
}

def _tool_to_zh(tool, args, path):
    url=str(args.get("url") or ""); path=path or (urlsplit(url).path if url else ""); method=str(args.get("method") or "GET").upper()
    if tool=="http_request":
        if method=="POST":
            body=args.get("json") or args.get("form") or args.get("body") or {}
            if body: return f"POST {path}\uff0c\u63d0\u4ea4\u8868\u5355 {list(body.keys())}"
            return f"POST {path}"
        return f"GET {path}"
    if tool=="http_session_request":
        id_v=re.search(r"(?i)\bid[=_](\d+)",str(args))
        if id_v: return f"\u5c1d\u8bd5ID={id_v.group(1)}\u7684\u8d8a\u6743\u8bbf\u95ee"
        return f"\u5e26\u4f1a\u8bdd\u7684HTTP\u8bf7\u6c42\u5230 {path}"
    if tool=="sql_boolean_compare":
        param=str(args.get("param") or args.get("test_field") or "?")
        expr=str(args.get("expression") or args.get("oracle_expression") or "true")
        return f"\u5e03\u5c14\u76f2\u6ce8\u6d4b\u8bd5\u53c2\u6570{param}\uff08\u8868\u8fbe\u5f0f\uff1a{expr[:30]}\uff09"
    if tool=="sqlmap_detect":
        param=str(args.get("param") or "?")
        return f"\u4f7f\u7528sqlmap\u63a2\u6d4b{param}\u7684SQL\u6ce8\u5165"
    if tool=="file_read": return f"\u8bfb\u53d6\u6587\u4ef6 {str(args.get('path') or args.get('file') or '')}"
    if tool=="command_execution": return f"\u6267\u884c\u547d\u4ee4\uff1a{str(args.get('command') or args.get('cmd') or '')[:60]}"
    return f"{tool}\uff08{path or args}\uff09"

def _group_by_phase(calls, semantic_by_card, target_url):
    phases={"recon":[],"exploit":[],"verify":[]}
    sql_tools={"sql_boolean_compare","sqlmap_detect","sqlmap_run","oracle_probe_matrix","boolean_config_extract"}
    id_pat=re.compile(r"(?i)\bid[=_](\d+)")
    seen=set()
    for i,call in enumerate(calls):
        tool=str(_value(call,"tool_name",""))
        args=_value(call,"normalized_arguments") or _value(call,"arguments_json") or {}
        url=str(args.get("url") or ""); path=urlsplit(url).path or ""
        if path and path in seen and tool in {"http_request","http_session_request"}: continue
        seen.add(path)
        if tool in sql_tools or tool.startswith("sql"): phase="exploit"
        elif id_pat.search(str(args)): phase="exploit"
        elif "flag" in str(args).lower(): phase="verify"
        elif tool in {"http_request","http_session_request"}: phase="recon" if path in {"/","/index","/login","/home"} else "exploit"
        else: phase="recon"
        card_id=f"call-{i}"; semantic=semantic_by_card.get(card_id,{})
        narrative=_safe_text(semantic.get("summary_zh") or _value(call,"reason") or _value(call,"intent") or tool, limit=300)
        phases[phase].append({"index":i+1,"tool":tool,"url":path,"args":args,"narrative":narrative,"importance":semantic.get("importance","medium")})
    return phases

def _build_semantic_narrative(phases, vuln_type, template, verified):
    lines=[]
    lines.extend(["## \u4e00\u53e5\u8bdd\u89e3\u6cd5\uff08\u89e3\u9898\u601d\u8def\uff09",""])
    if template: lines.append(template.get("recon_intro",""))
    else: lines.append(f"\u901a\u8fc7\u81ea\u52a8\u5316\u63a2\u6d4b\u4e0e\u89e3\u9898\u5f15\u64ce\uff0c\u5bf9\u76ee\u6807\u6267\u884c\u4e86{sum(len(v)for v in phases.values())}\u6b65\u64cd\u4f5c\u3002")
    lines.extend(["","## \u653b\u51fb\u94fe",""])
    if template:
        lines.extend([f"\u4fe1\u606f \u2192 \u53d1\u73b0\u6f0f\u6d1e\uff08{vuln_type}\uff09 \u2192 \u5229\u7528 \u2192 \u83b7\u53d6Flag","",
          f"**\u6f0f\u6d1e\u7c7b\u578b\uff1a**{vuln_type}",f"**\u5229\u7528\u65b9\u5f0f\uff1a**{template.get('discovery','')}",""])
    else: lines.extend(["","\u5b8c\u6574\u653b\u51fb\u94fe\u8def\u89c1\u4e0b\u65b9\u5404\u9636\u6bb5\u8be6\u7ec6\u6b65\u9aa4\u3002",""])
    pm={"recon":("\u4fe1\u606f\u9636\u6bb5","\u8bbf\u95ee\u76ee\u6807\u9875\u9762\uff0c\u53d1\u73b0\u53ef\u7528\u7aef\u70b9\u548c\u529f\u80fd\u5165\u53e3\u3002"),
        "exploit":("\u6f0f\u6d1e\u5229\u7528\u9636\u6bb5",(template.get("exploit","") if template else "\u901a\u8fc7\u5df2\u8bc6\u522b\u6f0f\u6d1e\u8fdb\u884c\u5229\u7528\u3002")),
        "verify":("\u9a8c\u8bc1\u9636\u6bb5","\u786e\u8ba4Flag\u5185\u5bb9\u548c\u683c\u5f0f\u3002")}
    for pk in ("recon","exploit","verify"):
        items=phases.get(pk,[])
        if not items: continue
        title,desc=pm.get(pk,(pk,""))
        lines.extend([f"### {title}","",desc,""])
        priority=[it for it in items if it["importance"] in {"high","critical"}]
        normal=[it for it in items if it["importance"] not in {"high","critical"}]
        for it in priority+normal[:10]:
            narrative=it["narrative"]
            if narrative==it["tool"]: narrative=_tool_to_zh(it["tool"],it["args"],it["url"])
            lines.append(f"**{it['index']}. {narrative}**")
            if it["url"]: lines.append(f"   \u7aef\u70b9\uff1a{it['url']}")
            ags=_safe_argument(it["args"],target_url="")
            if ags and ags!={}:
                try:
                    js=json.dumps(ags,ensure_ascii=False,indent=2)
                    if len(js)<400: lines.extend(["   \u53c2\u6570\uff1a","   \u60f3\u5173\u7b26","   "+js,"   \u201d\u201d\u201d"])
                except Exception: pass
            lines.append("")
    return lines

def _step_kind(tool: str, args: Any) -> str:
    """Classify one step as READ / OBSERVE / WRITE for a readable chain.

    A wall of tool names hides the shape of the attack; whether a step merely
    read target data, confirmed a behaviour, or changed target state is the one
    distinction a reader actually needs.  The marker is kept as an internal
    constant (``_STEP_KIND_LABELS`` localizes it for display).
    """

    name = str(tool or "").lower()
    arguments = args if isinstance(args, Mapping) else {}
    method = str(arguments.get("method") or "").upper()
    if name in {"file_write", "file_write_lines", "command_execution", "node_repl", "node_repl.js", "python_exec", "sqlmap_run"}:
        return "WRITE"
    if name.startswith("sql") or method in {"POST", "PUT", "PATCH", "DELETE"}:
        return "WRITE"
    if method in {"GET", "HEAD", "OPTIONS"} or name in {"file_read", "http_request", "http_session_request", "browser_navigate"}:
        return "READ"
    return "OBSERVE"


def _build_template_narrative(calls, vuln_type, template, verified, semantic_by_card, target_url):
    lines=[]
    lines.extend(["## \u4e00\u53e5\u8bdd\u89e3\u6cd5\uff08\u89e3\u9898\u601d\u8def\uff09",""])
    if template:
        disc=template.get("discovery",""); lines.append(f"\u901a\u8fc7{vuln_type}\u6f0f\u6d1e\uff0c{disc}"[:200])
    elif verified: lines.append(_safe_text(_value(verified[0],"content",""),limit=200))
    else: lines.append(f"\u5bf9\u76ee\u6807\u6267\u884c\u4e86{len(calls)}\u6b65\u81ea\u52a8\u5316\u64cd\u4f5c\u3002")
    lines.extend(["","## \u653b\u51fb\u94fe",""])
    chain=[]; sql_tools={"sql_boolean_compare","sqlmap_detect","sqlmap_run","oracle_probe_matrix"}; id_pat=re.compile(r"(?i)\bid[=_](\d+)")
    for call in calls:
        tool=str(_value(call,"tool_name","")); args=_value(call,"normalized_arguments") or _value(call,"arguments_json") or {}
        url=str(args.get("url") or ""); path=urlsplit(url).path or ""
        if tool in sql_tools: chain.append("\u5e03\u5c14\u76f2\u6ce8\u5224\u65ad")
        elif id_pat.search(str(args)): chain.append("IDOR\u8d8a\u6743\u8bbf\u95ee")
        elif tool in {"http_request","http_session_request"}:
            method=str(args.get("method","GET")).upper(); body=args.get("json") or args.get("form") or args.get("body")
            if method=="POST" or body: chain.append(f"POST {path}")
            elif path in {"/","/index",""}: chain.append("\u8bbf\u95ee\u9996\u9875")
            else: chain.append(f"GET {path}")
    seen=[]; [seen.append(s) for s in chain if s not in seen]
    lines.append(" → ".join(seen[:6]) if seen else "自动化工具调用")
    lines.extend(["", "阶段标记：读取=读取目标数据 / 观察=确认响应或现象 / 写入=对目标产生写入或状态变更。", ""])
    lines.extend(["","## \u6267\u884c\u8fc7\u7a0b",""])
    for i,call in enumerate(calls[:20]):
        tool=str(_value(call,"tool_name","")); args=_value(call,"normalized_arguments") or _value(call,"arguments_json") or {}
        url=str(args.get("url") or ""); path=urlsplit(url).path or ""
        _reason = _safe_text(args.get("reason") or args.get("intent") or "", limit=200)
        _label = _reason or _tool_to_zh(tool,args,path)
        lines.extend([f"**{i+1}. [{_STEP_KIND_LABELS.get(_step_kind(tool,args), _step_kind(tool,args))}] {_label}**",""])
        _ags = _safe_argument(args, target_url="")
        if isinstance(_ags, dict) and _ags:
            try:
                _js = json.dumps(_ags, ensure_ascii=False, indent=2)
            except Exception:
                _js = ""
            if _js and len(_js) < 600:
                lines.extend(["   参数：", "   ```", *["   " + row for row in _js.split(chr(10))], "   ```"])
    return lines

def _build_llm_narrative(
    one_liner: str,
    attack_chain: Sequence[str],
    execution: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Render the model-authored narrative sections.

    These three sections are the ones the template renderer was structurally
    unable to write: it could only quote the single promoted fact, and it had
    no record of intermediate steps at all.  Anything missing here falls through
    to the deterministic renderer for that section.
    """

    lines: list[str] = []
    lines.extend(["## 一句话解法", ""])
    lines.append(_safe_text(one_liner, limit=400) or "（模型未给出结论）")
    lines.extend(["", "## 攻击链", ""])
    if attack_chain:
        for index, step in enumerate(list(attack_chain)[:10], start=1):
            lines.append(f"{index}. {_safe_text(step, limit=300)}")
    else:
        lines.append("（模型未给出链路）")
    lines.extend(["", "## 执行过程", ""])
    if execution:
        for index, item in enumerate(list(execution)[:20], start=1):
            step = int(item.get("step") or index)
            title = _safe_text(item.get("title"), limit=120) or f"步骤 {step}"
            detail = _safe_text(item.get("detail"), limit=1200)
            lines.append(f"**{step}. {title}**")
            lines.append("")
            if detail:
                lines.append(detail)
                lines.append("")
    else:
        lines.append("（模型未给出执行过程）")
    lines.append("")
    return lines


def render_muteki_writeup(
    *,
    challenge: Any,
    run: Any,
    result: Any,
    graph_state: Mapping[str, Any],
    calls: Sequence[Any],
    observations: Sequence[Any],
    evidence: Sequence[Any],
    poc_available: bool,
    recovered_trace: Mapping[str, Any] | None = None,
    semantic_analysis: Mapping[str, Any] | None = None,
    llm_report: Mapping[str, Any] | None = None,
) -> str:
    target_url = str(getattr(challenge, "target_url", "") or "")
    facts = [item for item in (graph_state.get("facts", []) or []) if isinstance(item, Mapping)]
    dead_ends = [item for item in (graph_state.get("dead_ends", []) or []) if isinstance(item, Mapping)]
    evidence_rows = _evidence_rows(evidence)
    recovered_trace = recovered_trace or {}
    semantic_analysis = semantic_analysis or {}
    semantic_items = [item for item in (semantic_analysis.get("items") or []) if isinstance(item, Mapping) and str(item.get("card_id") or "")]
    semantic_by_card = {str(item.get("card_id")): item for item in semantic_items}
    verified = [f for f in facts if _value(f, "verified") is True]
    classification = _classification(facts)
    tier, score = _complexity_score(facts, calls, evidence)
    vuln_type = _detect_vuln_type(calls, facts)
    template = _VULN_TEMPLATES.get(vuln_type, {})
    solved = run.status == "COMPLETED_SOLVED" if run else False
    flag = ""
    if result is not None:
        if isinstance(result, Mapping):
            flag = str(result.get("flag") or result.get("candidate") or "")
        else:
            flag = str(getattr(result, "flag", "") or getattr(result, "candidate", "") or "")
    llm_report = dict(llm_report or {})
    llm_status = str(llm_report.get("status") or "not_run")
    llm_sections = dict(llm_report.get("sections") or {}) if llm_status == "completed" else {}
    llm_one_liner = _safe_text(llm_sections.get("one_liner"), limit=400)
    llm_chain = [str(item).strip() for item in (llm_sections.get("attack_chain") or []) if str(item).strip()]
    llm_execution = [item for item in (llm_sections.get("execution") or []) if isinstance(item, Mapping)]
    llm_repro = str(llm_sections.get("repro") or "").strip()
    llm_failures = [str(item).strip() for item in (llm_sections.get("failed_paths") or []) if str(item).strip()]
    llm_fixes = [str(item).strip() for item in (llm_sections.get("remediation") or []) if str(item).strip()]
    llm_notes = _safe_text(llm_sections.get("notes"), limit=600)
    llm_vuln_type = _safe_text(llm_sections.get("vuln_type"), limit=60)
    has_llm = bool(llm_one_liner or llm_chain or llm_execution or llm_repro or llm_fixes)
    if llm_vuln_type:
        vuln_type = f"{llm_vuln_type}（模型判定）/ {vuln_type}（启发式）"
    unique_tools = sorted({str(_value(c, "tool_name", "")) for c in calls if _value(c, "tool_name", "")})
    lines = ["# CTF Writeup（解题报告）", ""]
    _conv_note = (
        f"对话 {int(llm_report.get('conversation_chars') or 0)} 字符"
        if llm_report.get("conversation_available")
        else "无可用 Worker 对话"
    )
    model_note = (
        f"LLM 章节：已生成（模型 {_safe_text(llm_report.get('model') or 'reason-model', limit=40)}"
        f"；{_conv_note}）"
        if has_llm
        else "LLM 章节：未启用或生成失败，正文为确定性模板渲染"
    )
    lines.extend([
        "## 题目信息与结果", "",
        f"- **题目：**{_safe_text(getattr(challenge, 'name', '') or getattr(challenge, 'title', ''), limit=200)}",
        f"- **目标：**{_safe_text(target_url, limit=200)}",
        f"- **解题模式：**{getattr(run, 'solver_mode', 'unknown') or 'unknown'}",
        f"- **解题状态：**{'已解出' if solved else '未解出'}",
        f"- **漏洞类型：**{_vuln_type_label(vuln_type)}",
        f"- **黑板判定：**{classification}",
        f"- **复杂度：**{_tier_label(tier)}（评分 {score}）",
        f"- **语义分析：**{_semantic_status_label(semantic_analysis.get('status'))}（{len(semantic_items)} 项）",
        f"- **{model_note}**",
        "",
    ])
    phases = _group_by_phase(calls, semantic_by_card, target_url)
    narrative: list[str] = []
    if has_llm:
        narrative = _build_llm_narrative(llm_one_liner, llm_chain, llm_execution)
    if not narrative:
        if tier in ("simple", "medium"):
            narrative = list(_build_template_narrative(calls, vuln_type, template, verified, semantic_by_card, target_url))
        else:
            narrative = list(_build_semantic_narrative(phases, vuln_type, template, verified))
    # Card summaries are a UI-garde, already-model-authored projection; show them
    # whenever they exist regardless of tier — a "simple" run with semantics is
    # still more informative than the template path alone.
    if semantic_items:
        narrative.extend(["## AI 语义解析（与案情分析板同源）", ""])
        for item in semantic_items[:15]:
            cat = _safe_text(item.get("category", "已确认事实"), limit=40)
            imp = item.get("importance", "medium")
            summary = _safe_text(item.get("summary_zh", ""), limit=200)
            narrative.append(f"- **AI 语义摘要 · {cat}**（重要度：{_importance_label(imp)}）：{summary}")
        narrative.append("")
    lines.extend(narrative)
    lines.extend(["## 已确认事实与证据链", ""])
    if verified:
        for item in verified[:30]:
            refs = _value(item, "evidence_refs") or []
            card_id = f"fact-{_value(item, 'sequence', '')}"
            sem = semantic_by_card.get(card_id, {})
            summary = _safe_text(sem.get("summary_zh") or _value(item, "content", ""), limit=300)
            importance = sem.get("importance", "medium")
            lines.append(f"- **[{_importance_label(importance)}]** {summary}")
            if refs:
                lines.append("  证据引用：" + "、".join(f"{r}" for r in refs[:3]))
            lines.append("")
    else:
        lines.append("无已验证事实")
    if evidence_rows:
        lines.extend(["", "### 证据台账（Evidence Ledger）", ""])
        for row in evidence_rows[:40]:
            lines.append(f"- {row['id']} · {row['type']} · {row['status']} · {_safe_text(row['summary'] or '无摘要', limit=200)}")
        lines.append("")
    lines.extend(["## 可复现方式", ""])
    if poc_available:
        lines.extend(["已生成含证据引用的 PoC 包：`final/muteki-poc.zip`。", "解压后设置 TARGET_URL，再执行 `python reproduce.py`。", ""])
    if llm_repro:
        verified = str(llm_report.get("repro_verification") or "").strip()
        lines.append(
            f"> 以下执行命令/脚本由模型撰写，**尚未在本管线内自动执行验证**"
            + (f"（核验状态：{_safe_text(verified, limit=120)}）。" if verified else "。")
        )
        lines.append("")
        lines.append(llm_repro[:6000])
        lines.append("")
    elif not poc_available:
        lines.append(
            "当前没有足够的已验证HTTP请求和Evidence引用生成可执行PoC；"
            "本报告不会用猜测内容伪造脚本。"
        )
    lines.extend(["", "## 失败路线与策略切换", ""])
    recorded_failures = False
    if dead_ends:
        for item in dead_ends[:20]:
            lines.append(f"- 死胡同：{_safe_text(_value(item, 'description', ''), limit=300)}")
        recorded_failures = True
    if llm_failures:
        origin = "模型补充" if recorded_failures else "模型校正的失败路线"
        lines.append(f"- {origin}：")
        for item in llm_failures[:12]:
            lines.append(f"  - {_safe_text(item, limit=300)}")
        recorded_failures = True
    if not recorded_failures:
        lines.append("无记录的失败路线")
    lines.extend(["", "## 完成门禁（Completion Gate）", ""])
    _fc = "已通过" if solved else "未通过"
    _eb = "是" if evidence_rows else "否"
    lines.extend([f"- 结果判定 / Flag：{_fc}", f"- 是否有证据支撑：{_eb}"])
    if flag:
        lines.append(f"- 已验证 Flag：{_display_verified_flag(flag)}")
    lines.extend(["", "## 修复建议", ""])
    if llm_fixes:
        for item in llm_fixes[:8]:
            lines.append(f"- {_safe_text(item, limit=400)}")
    else:
        fix_map = {
            "SQLI": "对所有用户输入实施参数化查询，使用ORM也禁止字符串拼接；添加WAF规则拦截最常规的注入特征",
            "IDOR": "在服务端对所有资源访问实施基于当前用户身份的所有权校验，不依赖前端隐藏参数",
            "PATH_TRAVERSAL": "禁止路径穿越字符（../、..\\等），使用白名单限制可访问目录，或使用API抽象文件访问",
            "CMD_INJECTION": "避免将用户输入拼接到系统命令；使用参数化API或沙箱化执行环境",
            "SSRF": "禁止内网IP段（127.0.0.1、169.254.169.254等）；URL白名单校验；禁止跟随重定向到用户可控地址",
            "GENERIC_WEB": "实施严格的输入校验、授权校验、最小权限与审计日志，并对暴露面做定期攻防验证",
        }
        # Only reached with no model-authored fixes: the templated line names the
        # class it guessed, so a wrong guess is visible instead of looking curated.
        lines.append(fix_map.get(vuln_type, fix_map["GENERIC_WEB"]))
    tools_str = (", ".join(f"{t}" for t in unique_tools) if unique_tools else "未记录")
    lines.extend(["", f"- 本次实际使用工具: {tools_str}", ""])
    lines.extend(["", "## 报告来源", ""])
    if has_llm:
        conv_chars = int(llm_report.get("conversation_chars") or 0)
        if llm_report.get("conversation_available"):
            source = f"Worker 对话（{conv_chars} 字符）"
        else:
            source = (
                "黑板事实与事件时间线"
                f"（未捕获到可用的 Worker 对话，仅 {conv_chars} 字符占位记录）"
            )
        lines.append(
            f"- 叙事章节（一句话解法 / 攻击链 / 执行过程 / 可复现方式 / 修复建议）由 LLM 依据{source}生成，输入 {conv_chars} 字符。"
        )
        usage = llm_report.get("usage") if isinstance(llm_report.get("usage"), Mapping) else {}
        if usage:
            lines.append(
                f"- 生成开销：模型 {_safe_text(usage.get('model'), limit=60)}，"
                f"tokens {int(usage.get('total_tokens') or 0)}。"
            )
    else:
        reason = _safe_text(llm_report.get("error_code"), limit=120) if llm_status == "failed" else "未调用"
        lines.append(f"- 叙事章节使用确定性模板渲染（LLM 生成状态：{llm_status}；原因：{reason or 'N/A'}）。")
    lines.append("- 题目信息 / 已确认事实与证据链 / 证据台账 / 完成门禁 / 工具清单均由代码按数据库与黑板状态生成，不做改写。")
    if llm_notes:
        lines.append(f"- 模型标注的证据缺口：{llm_notes}")
    lines.append("")
    return "\n".join(lines)

__all__ = ["render_muteki_writeup"]
