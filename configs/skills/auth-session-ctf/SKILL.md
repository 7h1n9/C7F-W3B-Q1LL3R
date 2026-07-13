---
name: auth-session-ctf
display_name: Auth Session CTF
description: Analyze login, session, cookie and access-control behavior with bounded evidence.
challenge_types: [WEB_TARGET]
skill_kind: SPECIALIST
activation_mode: MANUAL
positive_triggers: [login, session, cookie, redirect, 302]
negative_triggers: [pcap]
required_tools: [http_request, http_extract]
recommended_tools: [http_session_request, jwt_inspect]
forbidden_tools: [nmap_service_probe, nikto_scan]
ctf_phases: [MAPPING, TESTING, FLAG_SEARCH]
priority: 80
risk_level: low
version: 1
---
适用条件：出现登录、会话或重定向证据。
不适用条件：纯流量题或无授权目标。
最小验证流程：记录状态码、Location、Cookie 名称和表单字段，再做一次最小化会话验证。
工具选择：优先 http_extract/http_session_request。
证据标准：保存脱敏响应和可复核请求摘要。
成功条件：确认认证边界或获得可验证 Flag 线索。
失败切换条件：连续两次验证无新事实时转向源码或备份线索。
常见误区：重复提交相同凭据。
停止条件：超出 allowed_hosts 或需要未授权身份时停止。
