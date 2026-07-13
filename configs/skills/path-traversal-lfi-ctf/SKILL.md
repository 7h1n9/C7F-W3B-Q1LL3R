---
name: path-traversal-lfi-ctf
display_name: Path Traversal LFI CTF
description: Test a single evidence-backed file inclusion or traversal hypothesis.
challenge_types: [WEB_TARGET]
skill_kind: SPECIALIST
activation_mode: MANUAL
positive_triggers: [path, file, include, traversal, ../]
negative_triggers: []
required_tools: [http_request, http_extract]
recommended_tools: [file_read]
forbidden_tools: [nmap_service_probe]
ctf_phases: [HYPOTHESIS, TESTING, FLAG_SEARCH]
priority: 78
risk_level: medium
version: 1
---
适用条件：参数名或错误信息支持文件路径假设。
不适用条件：没有参数证据时不盲试。
最小验证流程：单参数、单编码变体、记录差异。
工具选择：http_request 和 http_extract。
证据标准：状态码、响应差异和最小泄露片段。
成功条件：确认受控文件读取或 Flag 线索。
失败切换条件：两次负面验证后转向其它假设。
常见误区：递归读取响应文件。
停止条件：禁止读取工作区外文件。
