---
name: xxe-ctf
display_name: XXE CTF
description: Identify XML external entity behavior using non-invasive local fixtures.
challenge_types: [WEB_TARGET]
skill_kind: SPECIALIST
activation_mode: MANUAL
positive_triggers: [xml, entity, doctype, parser]
negative_triggers: []
required_tools: [http_request, http_extract]
recommended_tools: []
forbidden_tools: [nmap_service_probe]
ctf_phases: [HYPOTHESIS, TESTING, FLAG_SEARCH]
priority: 73
risk_level: high
version: 1
---
适用条件：XML 解析入口和实体错误证据。
不适用条件：无 XML 入口时停止。
最小验证流程：无害实体和响应差异。
工具选择：http_request。
证据标准：请求体、响应和解析错误。
成功条件：确认实体解析行为。
失败切换条件：两次无差异后换方向。
常见误区：读取未授权文件。
停止条件：禁止外带请求。
