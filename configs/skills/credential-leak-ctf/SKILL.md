---
name: credential-leak-ctf
display_name: Credential Leak CTF
description: Find and validate exposed credentials without retaining secrets in model context.
challenge_types: [WEB_TARGET]
skill_kind: SPECIALIST
activation_mode: MANUAL
positive_triggers: [password, secret, token, api_key, comment]
negative_triggers: []
required_tools: [http_extract, file_read]
recommended_tools: [http_request, strings_extract]
forbidden_tools: [nmap_service_probe]
ctf_phases: [MAPPING, TESTING, FLAG_SEARCH]
priority: 85
risk_level: low
version: 1
---
适用条件：HTML 注释、源码或配置出现凭据线索。
不适用条件：无凭据迹象时不盲目枚举。
最小验证流程：提取字段、脱敏保存、仅向当前目标做一次验证。
工具选择：http_extract、file_read。
证据标准：凭据类型和验证结果可复核，原值只保存在 Artifact。
成功条件：确认凭据作用并获得授权 Flag 线索。
失败切换条件：验证失败后回到入口和权限假设。
常见误区：把完整 Cookie 或 Authorization 放入模型上下文。
停止条件：发现目标不在白名单时停止。
