---
name: api-graphql-ctf
display_name: API GraphQL CTF
description: Map a GraphQL endpoint and test one authorized query or mutation boundary.
challenge_types: [WEB_TARGET]
skill_kind: SPECIALIST
activation_mode: MANUAL
positive_triggers: [graphql, query, mutation, introspection]
negative_triggers: []
required_tools: [http_request, http_extract]
recommended_tools: []
forbidden_tools: [nmap_service_probe]
ctf_phases: [MAPPING, TESTING, FLAG_SEARCH]
priority: 72
risk_level: medium
version: 1
---
适用条件：响应或路径显示 GraphQL。
不适用条件：无 GraphQL 入口时停止。
最小验证流程：单查询、字段错误和授权差异。
工具选择：http_request。
证据标准：查询摘要、状态码和错误字段。
成功条件：确认越权字段或 Flag。
失败切换条件：两次无新事实后停止。
常见误区：无限制 introspection。
停止条件：遵守请求预算。
