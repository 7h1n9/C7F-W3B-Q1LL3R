---
name: http-traffic-analysis
display_name: HTTP Traffic Analysis
description: Analyze HTTP objects, headers, sessions and credentials in an authorized PCAP.
challenge_types: [TRAFFIC_ANALYSIS]
skill_kind: SPECIALIST
activation_mode: MANUAL
positive_triggers: [http, request, response, cookie, credential]
negative_triggers: []
required_tools: [pcap_protocols, pcap_http_objects, pcap_query]
recommended_tools: [pcap_credentials]
forbidden_tools: []
ctf_phases: [BASELINE, MAPPING, FLAG_SEARCH]
priority: 82
risk_level: low
version: 1
---
适用条件：PCAP 出现 HTTP 流量。
不适用条件：无 HTTP 证据时转向 DNS/TCP。
最小验证流程：协议层级、对象、关键头和 Flag 线索。
工具选择：pcap_http_objects、pcap_query。
证据标准：流编号、时间、请求路径和脱敏字段。
成功条件：确认关键 HTTP 对象或 Flag。
失败切换条件：无对象后转向 TCP 流。
常见误区：直接把完整 Cookie 放入上下文。
停止条件：保留完整原始 PCAP，仅传递摘要。
