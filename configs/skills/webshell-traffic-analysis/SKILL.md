---
name: webshell-traffic-analysis
display_name: Webshell Traffic Analysis
description: Detect bounded webshell-like request and response indicators in authorized traffic.
challenge_types: [TRAFFIC_ANALYSIS]
skill_kind: SPECIALIST
activation_mode: MANUAL
positive_triggers: [webshell, cmd, upload, eval, exec]
negative_triggers: []
required_tools: [pcap_http_objects, pcap_query]
recommended_tools: [pcap_credentials]
forbidden_tools: []
ctf_phases: [MAPPING, TESTING, FLAG_SEARCH]
priority: 78
risk_level: medium
version: 1
---
适用条件：HTTP 参数或响应出现命令执行指标。
不适用条件：无 HTTP 证据时停止。
最小验证流程：按路径和参数聚合，提取脱敏命令片段。
工具选择：pcap_http_objects、pcap_query。
证据标准：流编号、路径、参数名和响应差异。
成功条件：确认可疑 webshell 行为或 Flag。
失败切换条件：无重复模式后换方向。
常见误区：把完整命令或 Cookie 放入上下文。
停止条件：只分析已授权 PCAP。
