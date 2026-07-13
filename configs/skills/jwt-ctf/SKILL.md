---
name: jwt-ctf
display_name: JWT CTF
description: Inspect and validate JWT claims and authorization boundaries without brute force.
challenge_types: [WEB_TARGET]
skill_kind: SPECIALIST
activation_mode: MANUAL
positive_triggers: [jwt, token, bearer, alg, claims]
negative_triggers: []
required_tools: [http_extract, jwt_inspect]
recommended_tools: [http_session_request]
forbidden_tools: [nmap_service_probe]
ctf_phases: [MAPPING, HYPOTHESIS, TESTING, FLAG_SEARCH]
priority: 90
risk_level: medium
version: 1
---
适用条件：响应或前端存在 JWT 形态令牌。
不适用条件：无令牌证据时不猜测密钥。
最小验证流程：解码头和非敏感声明，比较权限边界。
工具选择：jwt_inspect、http_session_request。
证据标准：保存算法、声明键和响应差异，不保存完整令牌。
成功条件：确认授权缺陷或获得 Flag。
失败切换条件：无权限差异时回到会话分析。
常见误区：把完整 Authorization 放入上下文。
停止条件：禁止离线暴力破解。
