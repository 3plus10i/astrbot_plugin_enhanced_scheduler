# 增强计划任务 / Enhanced Scheduler

未来主动发言调度器。用多个触发器 + 逻辑规则组合实现复杂定时计划，不依赖 AstrBot 原生"主动能力"功能。

---

## 适用环境

- **AstrBot** `>= 4.27.0`
- **平台**：OneBot v11（`aiocqhttp`，like napcat）
- **依赖**：`croniter`（仅 cron 触发器用到）

## 安装

从插件市场一件安装。

或在插件页面上传 `astrbot_plugin_enhanced_scheduler.zip` 安装。

或把整个 `astrbot_plugin_enhanced_scheduler` 目录放到 AstrBot 的 `data/plugins/` 下，重启或在 WebUI 插件管理页重载。


## 配置入口

WebUI → 插件管理 → 找到「增强计划任务」→ 进入 Pages 页面。所有任务的增删改查都在这个页面完成。

不支持直接对 bot 发指令来创建/管理计划任务。

### 插件级配置（`_conf_schema.json`）

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `poll_interval` | int | 60 | 兜底轮询间隔（秒）。仅在无主动触发器或调度计算失败时生效；正常时按最近触发点自适应唤醒，不受此值影响 |
| `log_retention` | int | 200 | 内存中保留最近多少条触发/执行日志。最小 10 |
| `llm_timeout` | int | 60 | AI 单次生成超时（秒）。最小 5 |

---

## 核心概念

一个**计划任务**（task）由三部分组成：

1. **触发规则** = 触发器组 + 逻辑规则
2. **任务内容** = 文本（可选 `{{...}}` 占位符）+ 执行模式（固定文本 / 独立AI / 对话AI）
3. **发送对象** = 一个或多个 UMO

### 触发器（trigger）

每个触发器有一个**正整数 id**（在任务内唯一），五种类型：

| 类型 | 形态 | 关键字段 | 说明 |
|---|---|---|---|
| `interval` | 主动-周期型 | `base_time`、`days`、`hours`、`minutes` | 从 `base_time`（基准时间）起，每 `days*86400 + hours*3600 + minutes*60` 秒累加触发。`base_time` 默认=创建当天 0:00，可在页面自定义。**注意与 cron 的区别**：设 `hours=4, minutes=21`，则相邻触发精确间隔 4h21m，而不是"每隔 4 小时的 xx:21 分触发" |
| `cron` | 主动-cron型 | `expr` | 标准 5 段 cron（分 时 日 月 周），等价于 AstrBot 原生定时器。例：`0 */4 * * *` 每 4 小时 |
| `window` | 被动-区间型 | `start`、`end`、`weekdays` | 每天 `start`–`end` 区间内为真，端点包含。`start > end` 视为跨天区间（如 `22:00-06:00`）。`weekdays` 为空则不限制星期几；非空时元素 0=周一 … 6=周日 |
| `random` | 被动-随机型 | `threshold` | 取 `U(0,1) < threshold` 为真。`threshold ∈ [0,1]`，`0` 恒假，`1` 恒真 |
| `cooldown` | 被动-冷却型 | `hours`、`minutes` | `now - 本任务上次成功时间 >= 冷却时长` 为真。**每个任务的冷却计时器独立**，不能用本任务给其它任务的 cooldown 提供基准时间。"成功"包括：发送成功、以及"内容留空"的空动作（视为成功执行）。逻辑规则未通过（skip）与发送失败不推进冷却 |

**主动 vs 被动**：只有主动触发器（`interval`/`cron`）会"到点"。被动触发器（`window`/`random`/`cooldown`）自己不到点，只在被逻辑规则引用、且至少一个主动触发器到点时被求值一次。

### 逻辑规则（logic_expr）

用类算术表达式把各触发器组合起来：

- `+` 表示**或**（OR）
- `*` 表示**且**（AND）
- `*` 优先级高于 `+`（类比乘除 vs 加减）
- 支持括号 `()`
- 数字 = 触发器 id

例：

| 表达式 | 语义 |
|---|---|
| `1` | 触发器 1 到点就触发 |
| `1+2` | 1 或 2 任一到点即触发 |
| `1*2` | 1 且 2 同时为真才触发（1 必须是主动到点，2 往往是被动型） |
| `(1+2)*3` | （1 或 2）且 3 —— 即"1 或 2 主动到点，且被动 3 当前为真" |
| `1*2*3*4` | 1 到点，且 2、3、4 都为真 |

页面输入框会实时校验合法性（语法、引用的 id 是否存在）并显示**下次检查触发时间**（仅考虑主动触发器，被动无法预测；触发器配置有误时不显示）。

### 合并评估与"宕机不补发"

调度器采用**自适应唤醒**：主循环计算所有启用任务里最近的主动触发点，睡到该点再醒来处理（`asyncio.Event + wait_for`），而不是固定周期轮询。任务/配置变更会立即唤醒重算。每个 tick 内对每个启用的任务：

1. 检查所有主动触发器是否到点（基于各自 `last_fired`）
2. 若无任何主动触发器到点 → 跳过
3. 若有主动触发器到点 → 构造 tof_map（到点的主动=True；未到点的主动=False；被动触发器实时求值）→ 求逻辑规则
4. 逻辑通过 → 执行任务；逻辑未通过 → 仅记一条"skipped"日志
5. **无论逻辑是否通过，到点的主动触发器的 `last_fired` 都会推进到本次触发点**，避免同一窗口内重复判定

关键正确性保证：

- **新建任务**：所有触发器 `last_fired = now`，所以首次触发从未来第一个点开始，不会一建好就立刻触发
- **更新任务**：按触发器 id 继承原 `last_fired`，编辑保存后不会重置导致重复触发
- **复制任务**：副本 `last_fired = now` 且 `enabled = false`（默认停用），避免启用后立即双触发
- **宕机恢复**：触发点取 `<= now` 的最近一个（而非所有跨越的点），所以**只触发一次，不补发历史**

### 任务内容

任务内容由一个**执行模式**（下拉菜单）+ 一段**文本**组成。

| `mode` | 行为 |
|---|---|
| `fixed`（发送文本到对话） | 直接把渲染后的文本作为消息发到所有目标，不经过 AI |
| `standalone`（调用独立 AI 回复） | 用该任务自己的「独立 AI 系统提示」（`content.system_prompt`，空则回退默认）作为 system prompt + 任务提示词，独立独调用 AI 生成回复后发送，不注入对话人格、不与其他插件互动 |
| `conversation`（调用对话配置的 AI 回复） | 把任务提示词交给目标对话配置的 AI 生成回复后发送，**携带该对话的人格 system prompt 与记忆插件注入** |

**两种空动作**：

| 情形 | 解释 | 是否推进冷却 |
|---|---|---|
| 内容留空（`text` 为空） | 空动作，视为**成功执行**了目标动作 | ✅ 是（更新 `last_success_time`） |
| 主动触发后逻辑规则未通过 | **跳过**，未执行动作 | ❌ 否 |

两者都不发送消息，只记日志（`action` 分别为 `noop` 与 `skipped`）。

每个任务的「独立 AI 系统提示」在任务内容区填写，**任务独立**；新建任务会预填默认提示（<100 字，可自行修改）。

**对话 AI（conversation）调用流程**：

1. 取目标 UMO 当前配置的 `chat_provider_id`
2. 构造 `ProviderRequest(prompt=渲染后文本, system_prompt="", session_id=umo)` 和一个轻量 `_MockEvent`
3. **广播 `on_llm_request` 给其它所有已加载插件**（人格/记忆插件有机会注入 `system_prompt` 与上下文）
4. 调用 `context.llm_generate(chat_provider_id=..., ...)`，超时由 `llm_timeout` 控制
5. AI 空回复/超时/异常 → 记该目标失败（`ok:false`），**不降级为发送原文**

**独立 AI（standalone）调用流程**：不广播 `on_llm_request`，直接用**该任务的 `system_prompt`**（空则用默认提示）+ 任务提示词调用 `context.llm_generate`；`chat_provider_id` 从第一个目标 UMO 的会话配置获取。所有目标共享同一次生成结果（不依赖会话人格）。

> `context.llm_generate` 自 AstrBot v4.5.7 起**强制要求** `chat_provider_id`（keyword-only）。插件通过 `context.get_current_chat_provider_id(umo=...)` 从指定 UMO 的会话配置正确获取，找不到会报失败而不是静默跳过。

### 动态参数

任务内容（无论何种模式）都支持 `{{key}}` 占位符，内置三类：

- `{{time}}` → `YYYY-MM-DD HH:MM:SS`
- `{{date}}` → `YYYY-MM-DD`
- `{{weekday}}` → 周一…周日

此外还支持**触发器结果变量** `{{n}}`（n = 触发器编号，如 `{{1}}`、`{{2}}`），替换为对应触发器本次触发的结果说明，格式随触发器类型不同：

- 周期型：`2026-09-04 16:12触发了周期型触发器（周期1天2时30分）`
- cron 型：`2026-09-04 16:12触发了CRON型触发器（12 16 * * *）`
- 区间型：`激活了区间触发器：每天08:00-20:00` / `激活了区间触发器：每周二四五08:00-20:00`
- 随机型：`激活了随机触发器：0.436321<0.5`
- 冷却型：`激活了冷却触发器：距离上次触发2时15分，大于4时`

未触发/未激活的触发器对应描述以「未触发/未激活」开头；主动型未到点时不带时间戳。随机型的采样值与逻辑真值在同一次求值中产生，两者始终一致。

页面上这些变量以按钮形式展示（带当前值预览），点击即可插入到任务内容文本框的光标处。未知占位符保留原样（不报错）。不再支持自定义参数。

### 发送对象（UMO）

AstrBot 的统一消息源字符串，格式 `<platform>:<MessageType>:<id>`，例：

```
default:FriendMessage:xxxxxx
default:GroupMessage:xxxxxx
```

页面下拉会自动枚举活跃会话（`conversation_manager.session_conversations` 的键 + `get_conversations()` 返回的 `user_id`）供选，也支持手输。一个任务可绑多个目标，逐个发送，每个独立记成功/失败。

---

## 典型示例

### 1. 喝水提醒

固定文本，每 4 小时一发，但只在白天 8:00–20:00 发。

- 触发器 1（`interval`）：`hours=4`（`base_time` 默认当天 0 点，即 0/4/8/12/16/20 点）
- 触发器 2（`window`）：`start=08:00`、`end=20:00`、`weekdays=[]`
- 逻辑规则：`1*2`
- 内容：`mode=fixed`、`text="喝水啦~"`
- 目标：你的好友/群 UMO

### 2. 随机问候

让 AI 在白天随机发条日常闲聊，每 2 小时一次机会，命中率 50%，两次发送至少间隔 4 小时。

- 触发器 1（`interval`）：`hours=2`（`base_time` 默认当天 0 点，即每 2 小时一次机会）
- 触发器 2（`window`）：`start=08:00`、`end=20:00`、`weekdays=[]`
- 触发器 3（`random`）：`threshold=0.5`
- 触发器 4（`cooldown`）：`hours=4`
- 逻辑规则：`1*2*3*4`
- 内容：`mode=conversation`、`text="现在时间是{{time}}，你被一个随机触发规则唤醒，要给用户发送一条简短的日常消息。要求就像朋友间无聊时的日常闲聊消息或者问候一样。"`
- 目标：你的好友/群 UMO

`conversation` 模式会带上当前会话配置的人格 system prompt（通过 `on_llm_request` 广播注入）。若希望 AI 不携带人格、独立生成，改用 `standalone` 模式。

### 3. cron 兼容

完全用 cron 表达式，每天 7:30 发一条固定早安：

- 触发器 1（`cron`）：`expr="30 7 * * *"`
- 逻辑规则：`1`
- 内容：`mode=fixed`、`text="早安。"`
- 目标：UMO

### 4. 空动作（仅记日志）

内容留空即为空动作：触发后仅记一条日志，不发送消息、也不推进冷却计时器。可用于观察触发器/逻辑规则是否按预期到点。

- 内容：`text=""`（任意模式均可）
- 此时 `targets` 可为空

> 注意：每个任务的 `cooldown` 计时器是独立的，不能用本任务给另一个任务的 cooldown 提供基准时间。

---

## 日志

每个任务在**主动触发器到点时**都会记一条日志（无论逻辑是否通过、是否发送成功）。此外，点击任务列表的「触发」按钮会记一条 `source="manual"` 的手动触发日志（`logic_result` 为 `null`）：

```json
{
  "time": 1725412300.0,
  "task_id": "uuid",
  "task_name": "喝水提醒",
  "trigger_tof": {"1": true, "2": true},      // 各触发器本次 ToF
  "logic_result": true,                        // 逻辑规则结果（手动触发为 null）
  "action": "send_fixed",                      // send_llm | send_fixed | noop | partial | failed | skipped
  "targets_result": [{"umo":"default:FriendMessage:xxx","ok":true,"error":""}],
  "detail": "成功",
  "source": "manual"                           // 可选，手动触发时存在
}
```

日志存在内存里（持久化到 `data_dir/tasks.json` 的 `logs` 字段），保留最近 `log_retention` 条。页面有日志查看面板。

---

## 文件结构

```
astrbot_plugin_enhanced_scheduler/
├── metadata.yaml              # 插件元数据（版本、平台、作者）
├── _conf_schema.json          # 插件级配置 schema
├── requirements.txt           # croniter
├── scheduler_core.py          # 纯逻辑核心：触发器校验/求值、下次触发计算、逻辑规则解析、合并评估
├── main.py                    # AstrBot 集成：持久化、轮询、执行、LLM 调用、Web API
└── pages/enhanced_scheduler/
    ├── index.html             # 配置页面骨架
    ├── index.js               # 前端逻辑：任务列表、编辑弹窗、触发器组管理、逻辑规则实时校验
    └── style.css              # 亮/暗主题适配
```

持久化数据写在 `data/plugins/astrbot_plugin_enhanced_scheduler/tasks.json`（AstrBot 推荐的 `StarTools.get_data_dir` 路径），不在插件自身目录里——升级/重装不会丢配置。

## Web API

前端通过 `AstrBotPluginPage` bridge 调用以下端点（前缀 `/astrbot_plugin_enhanced_scheduler`）：

| 路径 | 方法 | 用途 |
|---|---|---|
| `/get_data` | GET | 取全部任务、日志、配置（含预计算的 `_next_fire`） |
| `/save_config` | POST | 保存插件级配置 |
| `/get_sessions` | GET | 枚举活跃会话 UMO，供发送对象下拉 |
| `/upsert_task` | POST | 新增/更新任务 |
| `/delete_task` | POST | 删除任务 |
| `/copy_task` | POST | 复制任务（副本默认停用） |
| `/validate` | POST | 校验触发器组与逻辑规则，返回下次触发预览 |
| `/preview_next` | POST | 给定触发器列表，返回下次触发时间 |
| `/trigger_now` | POST | 立即手动触发一次任务（忽略触发规则与启用状态，仅执行内容并发送） |

## 已知限制

- 被动触发器（`random`/`cooldown`）的"下次触发时间"无法预测，页面预览只考虑主动触发器
- `random` 在每次主动触发器到点时都会被求值一次（即使逻辑表达式没引用它），但仅在被引用时才影响结果。采样本身无副作用
- `cooldown` 依赖**本任务**的 `last_success_time`（各任务独立）。新任务该值为 0，所以新任务首次触发时 `cooldown` 恒为真；"内容留空"空动作视为成功会推进冷却，而"逻辑未通过"（skip）与发送失败不推进冷却
- 触发时间精度为秒级（自适应调度按最近触发点唤醒，误差在秒级）
- 跨天 `window` 区间（如 `22:00-06:00`）解析为 `22:00-23:59` ∪ `00:00-06:00` 两段
- `conversation` 模式的人格注入依赖其它插件实现 `on_llm_request` 钩子；若没有插件注入 `system_prompt`，LLM 收到的是裸 user prompt。`standalone` 模式不广播，恒使用本插件的 system prompt

## 版本

- `1.0.0`
