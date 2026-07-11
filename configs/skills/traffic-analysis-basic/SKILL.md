---
name: traffic-analysis-basic
display_name: 流量分析基础流程
description: 用于授权 CTF PCAP/PCAPNG 附件的证据驱动分析。
challenge_types:
  - TRAFFIC_ANALYSIS
allowed_tools:
  - file_read
  - file_search
  - pcap_metadata
  - pcap_protocols
  - pcap_query
risk_level: low
---

## 适用范围

仅分析当前题目附件目录中的 PCAP、PCAPNG 或 CAP 文件，不访问网络目标。

## 流量分析基本流程

先确认捕获元数据和文件哈希，再查看协议层级；随后以小范围、可复现的显示过滤器提取证据。

## 工具选择

优先使用 `pcap_metadata`、`pcap_protocols` 和受限的 `pcap_query`。只在已有文件证据需要梳理时使用文件阅读工具。

## 过滤器构建原则

从协议、端点和时间等单个维度逐步收窄。每次查询保留字段、过滤器和帧号，避免凭猜测扩大范围。

## 证据要求

结论要关联文件哈希、过滤器、帧号或提取字段；不把推测表述成已验证事实。

## Flag 识别

只将捕获内容中与配置正则匹配的字符串作为候选 Flag，并说明对应帧号或文件位置。

## 常见误判

注意重传、编码、分片、压缩、测试流量和看似 Flag 的示例文本；必要时以相邻数据包交叉验证。

## 停止条件

当已有充分可复现证据，或有限查询无法再提高置信度时停止，并清楚记录未证实部分。
