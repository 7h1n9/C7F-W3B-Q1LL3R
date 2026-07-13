---
name: tcp-stream-reassembly
display_name: TCP Stream Reassembly
description: Reassemble one bounded TCP stream and classify its protocol content.
challenge_types: [TRAFFIC_ANALYSIS]
skill_kind: SPECIALIST
activation_mode: MANUAL
positive_triggers: [tcp, stream, payload, session]
negative_triggers: []
required_tools: [pcap_tcp_stream, pcap_query]
recommended_tools: [pcap_http_objects]
forbidden_tools: []
ctf_phases: [MAPPING, TESTING, FLAG_SEARCH]
priority: 76
risk_level: low
version: 1
---
适用条件：协议层级显示 TCP 会话。
不适用条件：无可定位流编号时停止。
最小验证流程：选择一个流、限长重组、识别方向。
工具选择：pcap_tcp_stream。
证据标准：流编号、端点、长度和摘要哈希。
成功条件：得到可验证明文或 Flag。
失败切换条件：无法解码时转协议对象。
常见误区：导出全部流量。
停止条件：限制单流输出。
