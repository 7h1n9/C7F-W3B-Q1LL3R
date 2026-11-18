# C7F-W3B-Q1LL3R 知识库统一模板

## 设计原则

1. **所有知识条目统一为 JSON 格式** (便于检索和解析)
2. **分层存储** (理论 / POC / Payload / Writeup 各自独立但可关联)
3. **强制字段** (确保每个条目都有完整的元信息)
4. **关键词索引** (支持多维度查询)

---

## 模板定义

### 1. 理论知识模板 (theory/*.json)

```json
{
  "id": "theory-sql-injection-001",
  "type": "theory",
  "category": "injection",
  "name": "SQL 注入基础原理",
  "aliases": ["SQL注入", "SQLi", "数据库注入"],
  "severity": "high",
  "description": "通过构造恶意 SQL 语句绕过应用层验证,直接操作数据库",
  
  "attack_flow": [
    "1. 发现注入点 (参数未过滤)",
    "2. 判断注入类型 (数字型/字符型/搜索型)",
    "3. 绕过 WAF (编码/注释/大小写)",
    "4. 获取敏感信息 (联合查询/报错注入/盲注)"
  ],
  
  "key_concepts": {
    "union_injection": "使用 UNION 联合查询获取数据",
    "boolean_blind": "通过布尔逻辑判断数据库响应",
    "time_blind": "利用延时函数判断注入成功",
    "error_based": "触发数据库报错获取敏感信息"
  },
  
  "common_bypasses": [
    "空格 → /**/ or %0a or %09",
    "单引号 → %27 or char(39)",
    "关键词 → SeLeCt or /*!50000select*/",
    "WAF规则 → 双写 (selselectect)"
  ],
  
  "related_cves": [
    "CVE-2022-21661",
    "CVE-2021-24499"
  ],
  
  "related_pocs": [
    "pocs/high_value/wordpress-sql-injection.yaml",
    "pocs/high_value/thinkphp-sql-injection.yaml"
  ],
  
  "related_payloads": [
    "payloads/sql_injection/union_based.txt",
    "payloads/sql_injection/boolean_blind.txt"
  ],
  
  "related_writeups": [
    "writeups/by_category/sql_injection/buuctf_sqli_labs.md"
  ],
  
  "tags": ["sql", "injection", "database", "web", "ctf"],
  "references": [
    "https://portswigger.net/web-security/sql-injection",
    "OWASP SQL Injection Guide"
  ],
  
  "source": "Des-CTF-Knowledge/SQL.md",
  "last_updated": "2026-10-22"
}
```

### 2. Payload 模板 (payloads/*.json)

```json
{
  "id": "payload-sqli-union-001",
  "type": "payload",
  "category": "injection",
  "subcategory": "sql_injection",
  "technique": "union_based",
  
  "name": "MySQL UNION 联合查询",
  "description": "利用 UNION 查询获取数据库敏感信息",
  
  "payloads": [
    {
      "payload": "' UNION SELECT 1,2,database()--+",
      "target": "MySQL",
      "purpose": "获取当前数据库名",
      "context": "字符型注入"
    },
    {
      "payload": "' UNION SELECT 1,group_concat(table_name),3 FROM information_schema.tables WHERE table_schema=database()--+",
      "target": "MySQL",
      "purpose": "获取所有表名",
      "context": "字符型注入"
    },
    {
      "payload": "-1' UNION SELECT 1,group_concat(column_name),3 FROM information_schema.columns WHERE table_name='users'--+",
      "target": "MySQL",
      "purpose": "获取字段名",
      "context": "字符型注入"
    }
  ],
  
  "waf_bypasses": [
    {
      "original": "UNION SELECT",
      "bypassed": "/*!50000UNION*/ /*!50000SELECT*/",
      "waf": "通用 WAF"
    },
    {
      "original": "UNION SELECT",
      "bypassed": "UnIoN SeLeCt",
      "waf": "大小写过滤"
    }
  ],
  
  "related_theory": ["theory-sql-injection-001"],
  "related_pocs": ["pocs/high_value/mysql-union-injection.yaml"],
  
  "tags": ["sql", "union", "mysql", "information_schema"],
  "severity": "high",
  "source": "Des-CTF-Knowledge/SQL.md + PAYLOAD-CHEATSHEET.md",
  "last_updated": "2026-10-22"
}
```

### 3. POC 规则扩展字段 (已有 YAML → 补充 JSON 索引)

**原有**: `pocs/high_value/*.yaml` (保持不变)

**新增**: `pocs/high_value/index.json` (增强检索)

```json
{
  "apache-hadoop-yarn-unauth-rce.yaml": {
    "id": "poc-xray-hadoop-001",
    "cve_id": "CVE-2021-25646",
    "name": "Apache Hadoop YARN 未授权 RCE",
    "category": "auth_bypass",
    "subcategory": "rce",
    "severity": "critical",
    "engine": "xray",
    
    "related_theory": ["theory-command-injection-001", "theory-auth-bypass-002"],
    "related_payloads": ["payload-rce-reverse-shell-001"],
    
    "tags": ["hadoop", "yarn", "rce", "unauth", "java"],
    "last_updated": "2026-10-22"
  }
}
```

### 4. Writeup 模板 (writeups/index.json)

```json
{
  "id": "wp-buuctf-sqli-001",
  "type": "writeup",
  "competition": "BUUCTF",
  "challenge": "sqli-labs",
  "category": "web",
  "subcategory": "sql_injection",
  
  "title": "BUUCTF sqli-labs 全关卡通关",
  "difficulty": "medium",
  "solved_date": "2023-05-15",
  
  "summary": "通过 UNION 注入和盲注技巧完成 20 个关卡",
  "key_techniques": [
    "union_injection",
    "boolean_blind",
    "time_blind",
    "waf_bypass"
  ],
  
  "related_theory": ["theory-sql-injection-001"],
  "related_pocs": ["pocs/high_value/mysql-union-injection.yaml"],
  "related_payloads": ["payload-sqli-union-001"],
  
  "file_path": "writeups/by_category/sql_injection/buuctf_sqli_labs.md",
  "tags": ["sqli", "union", "blind", "ctf", "buuctf"],
  "last_updated": "2026-10-22"
}
```

---

## 统一检索接口

### 查询示例

```python
# 1. 按 CVE 查询 (返回完整知识链)
query_by_cve("CVE-2021-44228")
→ {
    "poc": "pocs/high_value/log4j-rce.yaml",
    "theory": "theory/command_injection.json",
    "payloads": ["payload-rce-reverse-shell-001.json"],
    "writeups": ["wp-log4shell-exploitation.json"]
}

# 2. 按攻击类型查询
query_by_category("sql_injection")
→ {
    "theories": [3个理论],
    "pocs": [158个POC],
    "payloads": [12组Payload],
    "writeups": [45个WP]
}

# 3. 按关键词查询
query_by_tags(["unauth", "rce"])
→ 返回所有包含这两个标签的条目

# 4. 跨层关联查询
query_related("theory-sql-injection-001")
→ 返回该理论相关的 POC + Payload + WP
```

---

## 目录结构

```
c7f_knowledge_base/
├── theory/                    理论层 (JSON 格式)
│   ├── sql_injection.json
│   ├── command_injection.json
│   ├── php_deserialize.json
│   └── ...
│
├── payloads/                  Payload 库 (JSON 格式)
│   ├── sql_injection/
│   │   ├── union_based.json
│   │   ├── boolean_blind.json
│   │   └── ...
│   ├── command_injection/
│   └── ...
│
├── pocs/high_value/           POC 规则 (保持 YAML)
│   ├── *.yaml                 实际规则文件
│   └── index.json             增强索引
│
├── writeups/                  精选 WP
│   ├── by_category/
│   │   ├── sql_injection/
│   │   └── ...
│   └── index.json             WP 索引
│
├── unified_index.json         全局统一索引
├── config.json
├── README.md
└── SCHEMA.md                  本文档
```

---

## 实施步骤

### Phase 1: 提取并结构化理论 (P0)
1. 解析 Des-CTF-Knowledge 的 8 个核心 MD
2. 提取关键信息 (原理/绕过方法/关键词)
3. 转换为统一 JSON 格式
4. 存入 `theory/*.json`

### Phase 2: 结构化 Payload (P0)
1. 从理论文档中提取所有 Payload 示例
2. 按攻击类型分类
3. 转换为统一 JSON 格式 (含目标/用途/绕过方法)
4. 存入 `payloads/*/*.json`

### Phase 3: 增强 POC 索引 (P1)
1. 为现有 500 个 POC 生成 `index.json`
2. 补充关键字段 (related_theory, related_payloads)
3. 建立与理论层的双向关联

### Phase 4: 精选 Writeup (P1)
1. 从 500+ 篇 WP 中筛选 50-100 篇高质量的
2. 生成 `writeups/index.json`
3. 建立与理论/POC 的关联

### Phase 5: 生成全局索引 (P0)
1. 创建 `unified_index.json`
2. 支持跨层查询 (CVE → 理论 → POC → Payload → WP)
3. 实现反向索引 (标签 → 所有相关条目)

---

## 优势对比

| 维度 | 原始 MD (Des-CTF) | 统一 JSON 模板 |
|------|-------------------|---------------|
| **检索速度** | 需全文搜索 (500ms+) | 索引查询 (5ms) |
| **关联查询** | 手动翻文档 | 自动返回关联内容 |
| **字段提取** | 需正则解析 MD | 直接读 JSON 字段 |
| **标签过滤** | 不支持 | 支持多标签组合 |
| **跨层查询** | 不支持 | 支持 CVE → 全链路 |

---

## 下一步

需要我现在开始执行 Phase 1 (提取并结构化 Des-CTF-Knowledge 的理论文档) 吗?

包括:
1. 解析 `SQL.md` / `命令执行.md` / `PHP反序列化.md` 等 8 个核心文档
2. 提取关键信息转换为 JSON
3. 建立与现有 POC 的关联
4. 生成 `theory/*.json` + `payloads/*.json`

预计 15-20 分钟完成。
