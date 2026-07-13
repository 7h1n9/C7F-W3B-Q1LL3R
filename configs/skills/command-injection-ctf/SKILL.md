---
name: command-injection-ctf
display_name: Command Injection CTF
description: Test command interpretation with harmless bounded timing or echo probes.
challenge_types: [WEB_TARGET]
skill_kind: SPECIALIST
activation_mode: MANUAL
positive_triggers: [command, ping, shell, exec]
negative_triggers: []
required_tools: [http_request, http_extract]
recommended_tools: []
forbidden_tools: [nmap_service_probe]
ctf_phases: [HYPOTHESIS, TESTING, FLAG_SEARCH]
priority: 72
risk_level: high
version: 1
---
适用条件：参数和错误信息支持命令解释假设。
不适用条件：无输入点时停止。
最小验证流程：无害回显或短超时探针。
工具选择：http_request。
证据标准：响应差异、时间和错误签名。
成功条件：确认命令解释并限定在题目范围。
失败切换条件：两次负面后停止。
常见误区：执行破坏性命令。
停止条件：禁止系统持久化和横向访问。
