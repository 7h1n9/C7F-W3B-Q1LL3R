"""Static validation shared by sandbox_exec and script_run.

This is deliberately conservative: a generated script is allowed to make
bounded urllib requests and write its result contract, but it cannot turn the
Runner into a shell or filesystem crawler.
"""
from __future__ import annotations

import ast
import re


ALLOWED_IMPORTS = {
    "json", "re", "sys", "time", "hashlib", "urllib", "urllib.parse",
    "urllib.request", "pathlib", "math", "statistics", "csv", "base64",
    "binascii", "collections", "itertools", "typing", "dataclasses",
}
FORBIDDEN_TEXT = (
    "subprocess", "os.system", "os.popen", "powershell", "cmd.exe", "bash -c",
    "shutil.rmtree", "os.walk", "socket", "http.client", "eval(", "exec(",
    "compile(", "__import__", "importlib", "ctypes", "ProxyHandler", "build_opener",
    "install_opener", "set_proxy", "NO_PROXY", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY",
)


def validate_python_source(source: str, *, require_result_contract: bool = True) -> list[str]:
    errors: list[str] = []
    if len(source.encode("utf-8")) > 200_000:
        errors.append("SCRIPT_TOO_LARGE")
    lowered = source.lower()
    for token in FORBIDDEN_TEXT:
        if token.lower() in lowered:
            errors.append(f"FORBIDDEN_TOKEN:{token}")
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        return [f"SYNTAX_ERROR:{error.msg}:{error.lineno}"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {
            "eval", "exec", "compile", "__import__", "breakpoint",
        }:
            errors.append(f"FORBIDDEN_CALL:{node.func.id}")
        if isinstance(node, ast.Import):
            names = [item.name for item in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [f"{node.module or ''}.{item.name}" for item in node.names]
        else:
            continue
        for name in names:
            if not any(name == allowed or name.startswith(f"{allowed}.") for allowed in ALLOWED_IMPORTS):
                errors.append(f"IMPORT_NOT_ALLOWLISTED:{name}")
    if require_result_contract and not re.search(r"result\.json|outputs[\\/]scripts", source):
        errors.append("RESULT_CONTRACT_NOT_DECLARED")
    return sorted(set(errors))


def validate_script_path(path: str) -> list[str]:
    normalized = str(path or "").replace("\\", "/")
    if not (normalized.startswith("scripts/") or normalized.startswith("scratch/scripts/")):
        return ["SCRIPT_PATH_INVALID"]
    return []
