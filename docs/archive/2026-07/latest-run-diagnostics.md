# 最新解题记录诊断

本文基于当前数据库中的真实解题记录整理，重点覆盖两类典型失败：

- `3db1cafa-c300-46cb-bd3b-65569ee1490a`，引擎：`codex_sdk`
- `ddba86fe-9117-4e81-a93a-a903438bdb08`，引擎：`openai_compatible`

## 结论

当前系统里最有价值的恢复信号有两类：

1. 工具契约不匹配
   - 典型表现：`python_run only accepts existing scripts/*.py files`
   - 含义：agent 把“临时命令”当成了“脚本文件”，违反了工具接口约束。

2. 认证边界反复试探
   - 典型表现：`/profile`、`/admin` 反复命中 `302 /login`
   - 含义：agent 已经进入登录重定向循环，没有回到可验证的最小路径。

## 真实记录摘要

### 1) `3db1cafa-c300-46cb-bd3b-65569ee1490a`

- 引擎：`codex_sdk`
- 当前状态：`FAILED_ENGINE`
- 主要特征：
  - 记录中出现多次 `/profile`、`/admin` 与 `302 /login`
  - 说明 agent 在认证边界外反复撞击
- 推荐恢复动作：
  - 先确认授权态的会话 / Token 形态
  - 再做一次最小验证
  - 避免继续在登录重定向上做同类试探

### 2) `ddba86fe-9117-4e81-a93a-a903438bdb08`

- 引擎：`openai_compatible`
- 当前状态：`COMPLETED_UNSOLVED`
- 主要特征：
  - 早期失败信号是 `python_run only accepts existing scripts/*.py files`
  - 说明方法论没有先把逻辑落到仓库脚本里
- 推荐恢复动作：
  - 先把可复用逻辑写成 `scripts/*.py`
  - 再调用 `python_run`
  - 对一次性命令执行使用更合适的工具，而不是硬塞给脚本工具

## 已落地的系统修复

### 动态 Skill 调用

- Skill 已升级为一等动作类型：
  - `activate`
  - `deactivate`
  - `inspect`
- 运行上下文会注入：
  - 活跃技能
  - 候选技能目录
  - 技能推荐结果

### 任务列表与工作区摘要

- 任务列表新增：
  - 题目名称
  - 题型
  - 活跃技能
  - 诊断标签
  - 诊断摘要
- 题目列表新增：
  - 任务数
  - 已解出数
  - 最近任务状态

### 异常恢复

- 新增运行诊断接口：
  - `GET /api/v1/runs/{run_id}/diagnostics`
  - `GET /api/v1/diagnostics/runs`
- 诊断结果会返回：
  - 异常代码
  - 严重级别
  - 证据
  - 恢复建议

## 方法论调整建议

1. 先确认工具契约，再调用工具。
2. 先确认认证边界，再做高频请求。
3. 出现连续无进展时，优先切换阶段而不是重复相同动作。
4. Skill 不再只是隐式上下文，应该显式推荐、显式激活、显式停用。

