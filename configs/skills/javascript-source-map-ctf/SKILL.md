---
name: javascript-source-map-ctf
display_name: JavaScript Source Map CTF
description: Analyze authorized JavaScript assets and source maps for routes and secrets.
challenge_types: [WEB_TARGET]
skill_kind: SPECIALIST
activation_mode: MANUAL
positive_triggers: [javascript, source map, map, bundle, webpack]
negative_triggers: []
required_tools: [js_asset_analyze, source_map_analyze, http_extract]
recommended_tools: [file_search]
forbidden_tools: [nmap_service_probe]
ctf_phases: [MAPPING, FLAG_SEARCH]
priority: 84
risk_level: low
version: 1
---
适用条件：脚本引用或 sourceMappingURL 证据。
不适用条件：无脚本资产时停止。
最小验证流程：提取路由、参数名和脱敏常量。
工具选择：js_asset_analyze、source_map_analyze。
证据标准：脚本 URL、路由和关键字符串。
成功条件：确认隐藏入口或 Flag 线索。
失败切换条件：无新路由后换方向。
常见误区：把完整密钥放入上下文。
停止条件：仅访问授权资产。
