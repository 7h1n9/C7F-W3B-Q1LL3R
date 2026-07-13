---
name: file-carving
display_name: File Carving
description: Identify embedded file signatures in authorized traffic or workspace artifacts.
challenge_types: [TRAFFIC_ANALYSIS]
skill_kind: SPECIALIST
activation_mode: MANUAL
positive_triggers: [file, magic, archive, carving]
negative_triggers: []
required_tools: [pcap_http_objects, file_type]
recommended_tools: [strings_extract]
forbidden_tools: []
ctf_phases: [MAPPING, FLAG_SEARCH]
priority: 72
risk_level: medium
version: 1
---
适用条件：对象或文件签名证据明确。
不适用条件：无二进制对象时停止。
最小验证流程：识别魔数、保存单个对象、计算哈希。
工具选择：pcap_http_objects、file_type。
证据标准：对象路径、类型、大小和哈希。
成功条件：还原可验证文件或 Flag。
失败切换条件：无签名后停止。
常见误区：递归提取未知压缩包。
停止条件：禁止无限递归和大文件输出。
