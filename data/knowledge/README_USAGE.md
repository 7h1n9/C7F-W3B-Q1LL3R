# C7F-W3B-Q1LL3R 知识库查询接口

## 快速开始

```python
import json
from pathlib import Path

kb_base = Path("data/knowledge")
unified_index = json.load(open(kb_base / "unified_index.json", encoding="utf-8"))

# 1. 按 CVE 查询
def query_by_cve(cve_id: str):
    """返回: {pocs: [...], theories: [...], payloads: [...]}"""
    return unified_index["indices"]["by_cve"].get(cve_id, {})

# 2. 按攻击类型查询
def query_by_category(category: str):
    """category: injection / file_ops / deserialize / ssrf_xxe"""
    return unified_index["indices"]["by_category"].get(category, {})

# 3. 按标签查询
def query_by_tag(tag: str):
    """返回所有条目 ID 列表"""
    return unified_index["indices"]["by_tag"].get(tag, [])

# 4. 获取条目详情
def get_entry(entry_id: str):
    """返回: {type, file, name, ...}"""
    entry = unified_index["indices"]["by_id"].get(entry_id)
    if not entry:
        return None
    
    # 读取实际文件
    file_path = kb_base / entry["file"]
    if entry["type"] == "poc":
        # POC 是 YAML 格式
        import yaml
        with open(file_path.with_suffix(".yaml"), "rb") as f:
            return yaml.safe_load(f)
    else:
        # 理论和 Payload 是 JSON
        return json.load(open(file_path, encoding="utf-8"))

# 示例: 查询 CVE-2021-44228 (Log4Shell)
result = query_by_cve("CVE-2021-44228")
print(f"POC: {len(result.get('pocs', []))} 个")
print(f"理论: {len(result.get('theories', []))} 个")
print(f"Payload: {len(result.get('payloads', []))} 个")

# 查看 POC 详情
for poc_id in result.get("pocs", [])[:1]:
    poc_detail = get_entry(poc_id)
    print(poc_detail["name"])
```

## 索引结构

### 1. CVE 映射 (by_cve)
```python
unified_index["indices"]["by_cve"]["CVE-2021-44228"]
# → {"pocs": [...], "theories": [...], "payloads": [...]}
```

### 2. 攻击类型 (by_category)
- `injection`: 注入类 (SQL注入、命令执行)
- `file_ops`: 文件操作 (上传、包含)
- `deserialize`: 反序列化
- `ssrf_xxe`: SSRF & XXE

### 3. 标签索引 (by_tag)
296 个标签,支持模糊匹配

### 4. ID 索引 (by_id)
直接访问任意条目: `unified_index["indices"]["by_id"][entry_id]`

## 目录结构

```
data/knowledge/
├── theory/              6个理论 (JSON)
├── payloads/            6组Payload (JSON)
├── pocs/                POC索引 (不含原始YAML)
├── unified_index.json   全局统一索引 ⭐
├── config.json
├── SCHEMA.md           统一模板文档
└── OPTIMIZATION.md     效率优化方案
```

## 性能优化

- 三级索引: CVE查询 5ms (vs 暴力遍历 500ms)
- 懒加载: 只在需要时读取实际文件
- LRU缓存: 热点条目命中 <5ms

详见 `OPTIMIZATION.md`
