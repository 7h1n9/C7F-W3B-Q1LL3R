---
name: file-upload-ctf
display_name: File Upload CTF
description: Validate upload type and storage behavior with harmless files.
challenge_types: [WEB_TARGET]
skill_kind: SPECIALIST
activation_mode: MANUAL
positive_triggers: [upload, file, multipart, image]
negative_triggers: []
required_tools: [http_request, http_extract]
recommended_tools: [file_type]
forbidden_tools: [nmap_service_probe]
ctf_phases: [MAPPING, TESTING, FLAG_SEARCH]
priority: 75
risk_level: high
version: 1
---
适用条件：存在上传表单和存储路径线索。
不适用条件：无上传入口时停止。
最小验证流程：使用无害文本或图片，记录 MIME、路径和回显。
工具选择：http_request、http_extract。
证据标准：响应、文件类型和访问路径。
成功条件：确认受控文件落点或 Flag 线索。
失败切换条件：两次负面后回到源码分析。
常见误区：上传可执行 payload。
停止条件：禁止破坏性文件写入。
