---
name: credential-extraction
display_name: Credential Extraction
description: Locate and redact credential-like values in authorized traffic artifacts.
challenge_types: [TRAFFIC_ANALYSIS]
skill_kind: SPECIALIST
activation_mode: MANUAL
positive_triggers: [password, authorization, cookie, credential, login]
negative_triggers: []
required_tools: [pcap_credentials, pcap_http_objects]
recommended_tools: [pcap_query]
forbidden_tools: []
ctf_phases: [MAPPING, FLAG_SEARCH]
priority: 80
risk_level: medium
version: 1
---
适用条件：协议载荷出现认证字段。
不适用条件：无认证协议时停止。
最小验证流程：提取字段名、掩码值、关联流编号。
工具选择：pcap_credentials。
证据标准：完整值只在 Artifact，模型只收到掩码摘要。
成功条件：确认凭据用途或 Flag 线索。
失败切换条件：无有效字段后停止。
常见误区：把完整 Session 写入上下文。
停止条件：严格脱敏。
