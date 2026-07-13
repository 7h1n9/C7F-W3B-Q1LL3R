---
name: ssti-ctf
display_name: SSTI CTF
description: Identify template expression behavior with harmless bounded probes.
challenge_types: [WEB_TARGET]
skill_kind: SPECIALIST
activation_mode: MANUAL
positive_triggers: [template, render, expression, jinja]
negative_triggers: []
required_tools: [http_request, http_extract]
recommended_tools: []
forbidden_tools: [nmap_service_probe]
ctf_phases: [HYPOTHESIS, TESTING, FLAG_SEARCH]
priority: 74
risk_level: high
version: 1
---
适用条件：模板错误或输入回显支持假设。
不适用条件：无回显时不盲试。
最小验证流程：使用无副作用表达式并比较响应。
工具选择：http_request。
证据标准：输入、输出和响应差异。
成功条件：确认模板解释行为。
失败切换条件：两次负面后换方向。
常见误区：执行破坏性表达式。
停止条件：禁止越过授权边界。
