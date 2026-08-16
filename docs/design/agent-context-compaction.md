# Agent 上下文压缩（Context Compaction）

参照 OpenAI codex 的本地压缩策略（codex-rs `core/src/compact.rs`）为 Agent 子系统
补上上下文管理：用量逼近模型窗口时自动生成「交接摘要」并重建历史，另提供手动
压缩接口。本文记录设计定案与取舍。

## 1. 背景与目标

此前 Agent 完全没有上下文管理：`AgentRunner` 每步全量重发
`system + 历史 + 工具往返`，工具结果全文入上下文（bash/mclaw 单次可达 50KB）；
发送后续消息时 `build_history()` 全量重放转录。长会话最终触发供应商的上下文超长错误
（不可重试、无恢复路径），运行直接失败。

目标：会话可以无限继续，压缩对任务的破坏最小，全过程对用户可见、可解释。

## 2. 触发策略

| 检查点 | 时机 | 用量来源 |
|---|---|---|
| pre-run | 组装完消息、进入循环之前 | 字节启发式（约 4 字节 ≈ 1 token）——冷启动没有服务端数据 |
| mid-run | 每步的全部工具结果回喂之后 | 服务端上报的 `usage.prompt_tokens + completion_tokens`，仅对尚未发送的新工具结果补估 |
| 手动 | `POST /sessions/{id}/compact-context` | 不判水位，无条件执行 |

- 水位线：`context_window × 0.9`（`COMPACT_TRIGGER_RATIO`，codex 同款比例）。
- 窗口来自模型目录 `ModelInfo.context_window`（预设全有、自定义模型入口强制
  声明）；**未声明窗口的目录外模型自动压缩停用**（打 info 日志），宁可放行到
  供应商报错也不做瞎猜的预算。
- mid-run 检查点选在工具结果配对回喂完成之后：此刻 call/output 必然完整，
  丢弃历史不会产生孤儿调用，转录里压缩行也落在回执之后，中断收尾语义不变。

## 3. 压缩流程（`movieclaw_agent/compaction.py`）

1. 把交接摘要指令（`COMPACT_PROMPT`）作为一条普通 user 消息追加到**全量现场**
   （system + 用户输入 + assistant 往返 + 工具调用与结果）末尾；
2. `tools=None` 发起一次普通模型调用——不带工具定义，模型只能输出文本，
   天然保证这次调用只写摘要；模型、系统提示词、采样参数都沿用会话本身的；
3. 取最终 content 作为摘要；任何失败（流错误、异常、空摘要）返回 None。

**降级原则**：压缩是优化路径。自动压缩失败只记日志、跳过本次压缩、运行继续
（靠工具层截断撑到下一个检查点）；手动压缩失败返回 502 让用户重试。

## 4. 历史重建规则（宽进严出）

生成摘要时模型看得到全部现场；压缩落地后新上下文只剩：

```
[预算内逆序保留的用户原话] + [SUMMARY_PREFIX + 摘要（user 消息收尾）]
```

- 用户原话按 `RETAINED_USER_TOKEN_BUDGET`（20k token）从最新往旧装入，装不下
  的最旧一条中部截断（头尾各半 + 截断标记）；**用户原话永不经过有损摘要**，
  防多次压缩后忘记原始意图；
- 旧压缩摘要（以 `SUMMARY_PREFIX` 开头的 user 消息）不算用户原话，不会被
  套娃保留；
- assistant / 思考内容 / 工具轨迹全部丢弃，信息由摘要承载；
- system 不在重建历史里——它从不入库，每次运行由 runner 重拼。

## 5. 持久化（转录 v2）

`SESSION_FORMAT_VERSION = 2`，新增压缩行：

```json
{"type": "compaction", "uuid": "...", "parent_uuid": "...", "timestamp": "...",
 "summary": "...", "replacement_history": [...], "tokens_before": 0, "tokens_after": 0}
```

- `replacement_history` 是压缩后上下文的**精确内容**（codex CompactedItem
  同款）：resume 时从最后一条压缩行起步、追加其后的增量消息即可，不存在
  「按摘要重算」的歧义；
- parent 链线性穿过压缩行；压缩前的原始消息完整留在文件里（回放展示用），
  只是不再进入模型上下文；
- **老读者容错**：v1 读端把压缩行当坏行跳过，重建出未压缩的全量历史——更大
  但仍合法，属可接受降级，无需迁移；
- `seal_pending_tool_calls` 只处理最后一条压缩行之后的消息（更早的往返是死
  上下文，补回执毫无意义）；
- `session get-transcript` 返回包括 `replacement_history` 在内的完整压缩轨迹；
  该命令不分页、不截断，调用方需自行权衡长会话的数据体量。

## 6. 事件与前端

- 新事件 `context_compacted`，载荷 `{summary, tokens_before, tokens_after}`
  （替换历史不进 SSE）；
- Web 时间线新 segment `kind: "compaction"`：横线分隔卡片 + 可展开摘要，实时
  （事件）与回放（压缩行）共用同一渲染路径；卡片同时是「多次压缩会降低准确
  性」的可见信号；
- CLI `_render_event` 对该事件打一行 stderr。

## 7. 手动压缩接口

`POST /sessions/{session_id}/compact-context`（`session.compact-context`）：
404（会话不存在）/ 400（正在运行——运行内自有 mid-run 压缩，并发写转录会破坏
链尾）→ 与自动压缩共用 `movieclaw_agent.compact()` → 压缩行落盘、刷新索引 →
返回 `{summary, tokens_before, tokens_after, compaction_id}`。模型沿用会话最近
一次运行的模型（转录 assistant 行的 model 元数据），无记录时走默认路由。

## 8. 已知取舍与后续工作

- **无溢出重试**：codex 在压缩请求本身超窗时从最旧端逐条删除重试；本实现的
  openai_chat 协议 error 事件无类型分类，无法可靠识别「上下文超长」，v1 不做。
  后续给 LlmError 补错误分类后可加。
- **token 数是估算值**：`tokens_before/after` 用 4 字节/token 启发式，仅供
  展示与观测；触发决策以服务端 usage 为准。
- **手动压缩的 TOCTOU 窗口**：`is_running` 检查与执行之间理论上可插入新运行，
  与删除会话相同的已接受取舍。
- 工具层通常有源头截断（bash/mclaw 尾部保留 2000 行 / 50KB）；
  `session get-transcript` 明确豁免该限制，确保模型需要时能取得完整轨迹。

## 9. 测试矩阵

| 层 | 文件 | 覆盖 |
|---|---|---|
| 纯函数 | `tests/agent/unit/test_compaction.py` | token 估算、水位边界、重建规则（预算/截断/旧摘要排除） |
| loop 集成 | `tests/agent/unit/test_runner.py` | mid-run / pre-run 触发、失败降级、无窗口停用、回调载荷 |
| 存储 | `tests/api/test_agent_sessions.py` | 压缩行链条、build_history 重建、seal 范围、summarize 语义、未知类型容错 |
| API e2e | `tests/api/test_agent.py` | 手动端点、完整替换历史投影、后续消息使用压缩后上下文 |
| 前端 | 无 JS 测试设施 | `pnpm lint + build` + 手工验证卡片（实时 + 回放） |
