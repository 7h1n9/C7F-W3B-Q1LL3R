# 真实题目轨迹分析与收敛改造

## 结论

本轮改造的收敛主线是：公开入口基线 → 证据归一化 → 动态攻击面分类 → 最高价值假设 → 最小验证实验 → Flag 证据。发现高价值线索后，计划不再回退到无约束扫描。

## 三类题型的验证要求

| 题型 | 必须确认的入口 | 技术栈/参数 | 能力与关键 Artifact | 失败转向 |
| --- | --- | --- | --- | --- |
| Web-Re/认证 | 登录、会话、管理员接口 | signed cookie/JWT、权限字段 | Cookie 类型、claims、重签名结果、Flag 响应 | 先做 Cookie 类型差分，再检查签名路径 |
| 文档编辑系统 | file_path/preview/download | 文件路径、目录约束、正常基线 | 路径规范化差分、目标文件、Flag Artifact | Payload Ladder：正常值→单层→编码→框架变体 |
| Vite/WebSocket | 前端脚本与 WS URL | message schema、权限/逻辑字段 | 握手、Schema、消息往返、Flag 响应 | 先完成握手，再按字段做单变量差分 |

## 运行时控制

- 每次工具调用必须携带当前 Attempt 的 30 秒 Tool Ticket；Attempt 结束或过期立即失效。
- ToolArgumentAdapter 统一 target_url/url、session_id/session_name、data/body、params/query、cookie/headers.Cookie、artifact/path。
- ToolCall、MCP Event、Runner Job 分别保留为 Trace、Logical ToolCall、Execution Trace；统计和报告只使用 Logical ToolCall。
- checkpoint 间隔为 30 步；单纯运行时间不足以触发 WAITING_USER，只有硬阻塞或达到检查点规则才暂停。

## 产物与质量门禁

报告应生成 `final/writeup.zh-CN.md`、`final/minimal-reproduction.json`、`final/full-verified-path.json`、`final/reproduction-validation.json`、`final/payloads.json` 和 `final/evidence-manifest.json`。Writeup 必须包含一句话解法、攻击链、核心突破点、漏洞根因、接口/参数、已验证 Payload、请求响应片段、最小复现、Flag 来源和修复建议；评分低于 85 不得标记为最终版。

当前仓库已提供动态链、证据管线、参数适配、短期票据和质量校验基础能力；三条真实 Run 的重新执行与 Fresh Reproduction 仍需在可用的 Runner/题目目标环境中完成，不能凭静态代码宣称 READY。
