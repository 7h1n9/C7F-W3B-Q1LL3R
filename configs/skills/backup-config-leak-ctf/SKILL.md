---
name: backup-config-leak-ctf
display_name: Backup Config Leak CTF
description: Validate bounded backup and configuration exposure on an authorized web target.
challenge_types: [WEB_TARGET]
skill_kind: SPECIALIST
activation_mode: MANUAL
positive_triggers: [backup, config, bak, env, source]
negative_triggers: []
required_tools: [http_request, http_extract]
recommended_tools: [content_discovery, file_read]
forbidden_tools: [nikto_scan]
ctf_phases: [MAPPING, TESTING, FLAG_SEARCH]
priority: 82
risk_level: medium
version: 1
---
适用条件：响应或源码指向备份、配置文件。
不适用条件：禁止任意路径和大范围字典。
最小验证流程：验证一个候选文件，提取键名并遮盖敏感值。
工具选择：http_request、http_extract。
证据标准：保存响应摘要、路径和哈希。
成功条件：确认泄露内容与目标功能的关联。
失败切换条件：两个候选均不存在时停止该方向。
常见误区：重复读取同一路径。
停止条件：超出题目主机时停止。
