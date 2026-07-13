---
name: dns-covert-channel
display_name: DNS Covert Channel
description: Identify bounded DNS query patterns and possible encoded data in a PCAP.
challenge_types: [TRAFFIC_ANALYSIS]
skill_kind: SPECIALIST
activation_mode: MANUAL
positive_triggers: [dns, query, subdomain, encoded]
negative_triggers: []
required_tools: [pcap_dns_summary, pcap_query]
recommended_tools: []
forbidden_tools: []
ctf_phases: [BASELINE, MAPPING, FLAG_SEARCH]
priority: 78
risk_level: low
version: 1
---
适用条件：DNS 查询数量或子域模式异常。
不适用条件：无 DNS 流量时停止。
最小验证流程：按域名聚合、长度和字符集比较。
工具选择：pcap_dns_summary、pcap_query。
证据标准：域名计数、时间序列和编码特征。
成功条件：还原可验证的隐藏数据。
失败切换条件：无异常模式后换方向。
常见误区：无边界地解码所有域名。
停止条件：限制字段和输出量。
