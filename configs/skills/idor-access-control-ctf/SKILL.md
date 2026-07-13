---
name: idor-access-control-ctf
display_name: IDOR Access Control CTF
description: Compare authorization responses for two evidence-backed object identifiers.
challenge_types: [WEB_TARGET]
skill_kind: SPECIALIST
activation_mode: MANUAL
positive_triggers: [id, user, object, permission, access]
negative_triggers: []
required_tools: [http_session_request, http_extract]
recommended_tools: []
forbidden_tools: [nmap_service_probe]
ctf_phases: [MAPPING, TESTING, FLAG_SEARCH]
priority: 78
risk_level: medium
version: 1
---
适用条件：存在对象 ID 和身份边界证据。
不适用条件：没有合法测试账户时停止。
最小验证流程：固定会话，仅替换一个 ID，记录差异。
工具选择：http_session_request。
证据标准：身份、对象 ID、状态码和正文摘要。
成功条件：确认越权访问并找到 Flag。
失败切换条件：两次无差异后停止。
常见误区：枚举大量用户对象。
停止条件：仅测试题目提供的对象。
