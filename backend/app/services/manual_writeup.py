"""Chinese WP renderer with complete, copyable reproduction evidence."""

import json
import re

from app.services.reproduction_commands import reproduction_command_renderer


class ManualWriteupRenderer:
    def render(self, challenge, run, result, calls, observations, hypotheses, flags, steps, failure_reason, wp=None):
        target = re.sub(r"https?://[^/]+", "{{target_host}}", str(challenge.target_url or "{{target_url}}"))
        verified = [item for item in flags if item.verified and item.review_state == "VALID"]
        lines = [
            f"# {challenge.name}",
            "",
            "> [!SUMMARY]",
            "> **一句话解法：** 入口 → 漏洞确认 → 自动化工具/脚本 → 数据提取 → Flag。",
            "",
            "## 1. 题目信息", "",
            f"- 漏洞类型：{challenge.challenge_type}", f"- 目标（脱敏）：{target}",
            f"- 自动化方案：{', '.join(sorted({item.tool_name for item in calls if item.tool_name in {'sqlmap_detect','sqlmap_run','script_run','python_run','sql_boolean_compare'}})) or '待确认'}",
            f"- 最终结果：{result}", "",
            "## 2. 解题思路", "", "### 2.1 攻击链", "发现 → 确认 → 利用 → 提取 → 独立验证", "",
            "### 2.2 核心突破点", "真假条件产生稳定、可重复的响应差异；确认后由专项 Runner 完成提取。", "",
            "### 2.3 为什么选择自动化工具", "专项工具能复用会话、限制请求预算并留下结构化 Artifact，避免逐字符 HTTP 请求循环。", "",
            "## 3. 漏洞确认", "", "### 3.1 正常请求", "```bash", "curl -i '{{target_url}}'", "```", "",
            "### 3.2 恒真请求", "```bash", "curl -i '{{target_url}}?q=<true-condition>'", "```", "",
            "### 3.3 恒假请求", "```bash", "curl -i '{{target_url}}?q=<false-condition>'", "```", "",
            "### 3.4 差异表", "", "| 请求 | 状态码 | 长度 | 哈希 | 关键字 | 结论 |", "|---|---:|---:|---|---|---|",
        ]
        for obs in observations[:10]:
            facts = obs.facts_json or {}
            lines.append(f"| {obs.observation_type} | {facts.get('status_code','-')} | {facts.get('body_length','-')} | - | {', '.join(facts.get('error_signatures', [])) or '-'} | 已记录证据 |")
        lines += ["", "## 4. 漏洞原理", "", "用户输入进入后端查询参数，应用缺少参数化/正确转义；恒真与恒假条件改变响应判据，因此可以通过受控自动化请求提取目标字段。", "", "## 5. 自动化利用", "", "### 5.1 请求文件", "```http", "GET /?q=<baseline> HTTP/1.1", "Host: {{target_host}}", "", "```", "", "### 5.2 SQLMap 命令", "```bash"]
        automation_steps = [step for step in steps if step.tool_name in {"sqlmap_detect", "sqlmap_run", "script_run", "python_run"}]
        if automation_steps:
            lines.extend(reproduction_command_renderer.render(step.tool_name, step.normalized_arguments) for step in automation_steps)
        else:
            lines.append("sqlmap -r requests/request.txt -p q --batch")
        lines += ["```", "", "### 5.3 参数解释", "", "| 参数 | 作用 |", "|---|---|", "| `-r` | 读取已验证的 HTTP 请求文件 |", "| `-p` | 限定注入参数 |", "| `--batch` | 非交互运行 |", "| `--dbs/--tables/--columns/--dump` | 按目标逐级提取 |", "", "### 5.4 关键输出", "```text", "[+] bounded automation completed; see outputs/sqlmap/*/raw.log", "```", "", "## 6. 自编脚本", "", "若 SQLMap 不适用，使用 `scripts/solve_<vulnerability>_<run_id>.py`，由 Runner 注入 Secret Ref；脚本必须实际执行并输出结构化 JSON。", "", "## 7. Flag 获取", ""]
        lines += [f"- 已验证候选数：{len(verified)}", "- 来源：sqlmap/script/HTTP 输出 Artifact；具体字段、表和列以 `final/evidence-manifest.json` 为准。", "- 动态 Flag：flag{<redacted>}", "", "## 8. 最小复现步骤", ""]
        for step in steps:
            lines += [f"{step.order}. {step.title_zh}", f"   - 命令：`{step.manual_command or reproduction_command_renderer.render(step.tool_name, step.normalized_arguments)}`", f"   - 预期：{'; '.join(step.expected_evidence) or '产生对应 Artifact'}"]
        lines += ["", "## 9. 失败路径", "", f"- {failure_reason or '工具失败时切换到脚本或保留缓存，不重复执行同一读取。'}", "", "## 10. 修复建议", "", "使用参数化查询、严格输入校验、最小数据库权限，并记录安全审计日志。", "", "## 11. 证据清单", "", "- `final/minimal-solution-path.json`", "- `final/reproduction-commands.sh`", "- `final/reproduction-validation.json`", "- `final/evidence-manifest.json"]
        wp = wp or {}
        lines += ["", "## 12. Supervisor WP", "", "```json", json.dumps({
            "confirmed_facts": wp.get("confirmed_facts", []),
            "tested_fields": wp.get("tested_fields", []),
            "failed_tools": wp.get("failed_tools", []),
            "user_inputs": wp.get("user_inputs", []),
            "repeated_failures": wp.get("repeated_failures", []),
            "current_blocker": wp.get("current_blocker"),
            "next_manual_steps": wp.get("next_manual_steps", []),
        }, ensure_ascii=False, indent=2), "```"]
        return "\n".join(lines) + "\n"
