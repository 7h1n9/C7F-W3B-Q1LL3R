---
name: ssrf-ctf
display_name: SSRF CTF
description: Validate server-side request behavior only against explicitly authorized hosts.
challenge_types: [WEB_TARGET]
skill_kind: SPECIALIST
activation_mode: MANUAL
positive_triggers: [url, fetch, webhook, callback, proxy]
negative_triggers: [169.254.169.254]
required_tools: [http_request, http_extract]
recommended_tools: []
forbidden_tools: [nmap_service_probe]
ctf_phases: [HYPOTHESIS, TESTING, FLAG_SEARCH]
priority: 80
risk_level: high
version: 1
---
适用条件：参数触发服务端请求且目标主机已授权。
不适用条件：云元数据和未授权内网地址。
最小验证流程：使用题目允许的回环或辅助端点。
工具选择：http_request。
证据标准：请求方向、状态码和响应差异。
成功条件：确认 SSRF 行为并在白名单内取证。
失败切换条件：无可控差异时停止。
常见误区：访问云元数据地址。
停止条件：任何越权网络访问立即停止。
