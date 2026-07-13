---
name: deserialization-ctf
display_name: Deserialization CTF
description: Identify unsafe serialized input with non-invasive format and error analysis.
challenge_types: [WEB_TARGET]
skill_kind: SPECIALIST
activation_mode: MANUAL
positive_triggers: [pickle, serialize, object, deserialize]
negative_triggers: []
required_tools: [http_request, http_extract]
recommended_tools: []
forbidden_tools: [nmap_service_probe]
ctf_phases: [HYPOTHESIS, TESTING, FLAG_SEARCH]
priority: 70
risk_level: high
version: 1
---
适用条件：序列化格式和错误信息支持假设。
不适用条件：无输入点时停止。
最小验证流程：格式识别和无害字段变化。
工具选择：http_request。
证据标准：错误签名和状态变化。
成功条件：确认不安全反序列化边界。
失败切换条件：两次负面后停止。
常见误区：执行任意代码 payload。
停止条件：禁止持久化和破坏性动作。
