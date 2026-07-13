---
name: business-logic-race-ctf
display_name: Business Logic Race CTF
description: Compare bounded sequential requests for a race or state transition flaw.
challenge_types: [WEB_TARGET]
skill_kind: SPECIALIST
activation_mode: MANUAL
positive_triggers: [race, order, coupon, balance, state]
negative_triggers: []
required_tools: [http_session_request, http_extract]
recommended_tools: []
forbidden_tools: [nmap_service_probe]
ctf_phases: [MAPPING, TESTING, FLAG_SEARCH]
priority: 68
risk_level: medium
version: 1
---
适用条件：存在可观察状态转换和合法测试账户。
不适用条件：无状态证据时停止。
最小验证流程：先顺序复现，再少量并发比较。
工具选择：http_session_request。
证据标准：请求顺序、状态和资源变化。
成功条件：确认竞态影响。
失败切换条件：无差异后停止。
常见误区：高并发压测。
停止条件：遵守请求和资源预算。
