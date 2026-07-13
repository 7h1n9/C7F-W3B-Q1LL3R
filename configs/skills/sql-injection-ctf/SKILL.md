---
name: sql-injection-ctf
display_name: SQL Injection CTF
description: Perform low-risk, evidence-backed SQL injection detection on one parameter.
challenge_types: [WEB_TARGET]
skill_kind: SPECIALIST
activation_mode: MANUAL
positive_triggers: [sql, query, database, error, parameter]
negative_triggers: []
required_tools: [http_extract, http_request]
recommended_tools: [sqlmap_detect]
forbidden_tools: [nmap_service_probe, nikto_scan]
ctf_phases: [HYPOTHESIS, TESTING, FLAG_SEARCH]
priority: 76
risk_level: high
version: 1
---
适用条件：源码或错误信息支持 SQL 假设。
不适用条件：无参数证据时不扫描。
最小验证流程：单参数、低风险布尔差异、限制请求数。
工具选择：优先 http_request，必要时 sqlmap_detect。
证据标准：响应差异与错误签名。
成功条件：确认注入并在授权范围取证。
失败切换条件：两次负面验证后停止。
常见误区：dump、os-shell、批量目标。
停止条件：严格禁止数据倾倒。
