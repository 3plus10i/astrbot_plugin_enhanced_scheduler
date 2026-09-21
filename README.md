# 增强计划任务 / Enhanced Scheduler

未来主动发言调度器。用多个触发器组合实现复杂定时计划，不依赖 AstrBot 原生"主动能力"功能。

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
| `llm_log_retention` | int | 10 | 日志目录中保留最近多少条 AI 调用记录（一条记录一个文件，含完整请求体与图片）。最小 10 |

---

## 核心概念

一个**计划任务**（task）由三部分组成：

1. **触发规则** = 主动型触发器之间取“或”OR，被动型触发器之间取“且”AND
2. **任务内容** = 执行模式（固定文本 / 独立AI / 对话AI）+ 文本
3. **发送对象** = 一个或多个 UMO

### 触发器（trigger）

每个触发器有一个**正整数 id**（在任务内唯一），五种类型：

| 类型 | 形态 | 关键字段 | 说明 |
|---|---|---|---|
| `interval` | 主动-周期型 | `base_time`、`days`、`hours`、`minutes` | 从 `base_time`（基准时间）起，每 `days*86400 + hours*3600 + minutes*60` 秒累加触发。`base_time` 默认=创建当天 0:00，可在页面自定义。**注意与 cron 的区别**：设 `hours=4, minutes=21`，则相邻触发精确间隔 4h21m，而不是"每隔 4 小时的 xx:21 分触发" |
| `cron` | 主动-cron型 | `expr` | 标准 5 段 cron（分 时 日 月 周），等价于 AstrBot 原生定时器。例：`0 */4 * * *` 每 4 小时 |
| `window` | 被动-区间型 | `start`、`end`、`weekdays` | 每天 `start`–`end` 区间内为真，端点包含。`start > end` 视为跨天区间（如 `22:00-06:00`）。`weekdays` 为空则不限制星期几；非空时元素 0=周一 … 6=周日 |
| `random` | 被动-随机型 | `threshold` | 取 `U(0,1) < threshold` 为真。`threshold ∈ [0,1]`，`0` 恒假，`1` 恒真 |
| `cooldown` | 被动-冷却型 | `hours`、`minutes` | `now - 本任务上次成功时间 >= 冷却时长` 为真。**每个任务的冷却计时器独立**，不能用本任务给其它任务的 cooldown 提供基准时间。"成功"包括：发送成功、以及"内容留空"的空动作（视为成功执行）。被动条件未全部满足（skip）与发送失败不推进冷却 |

**主动 vs 被动**：只有主动触发器（`interval`/`cron`）会"到点"。被动触发器（`window`/`random`/`cooldown`）自己不到点，只在有主动触发器到点时被求值一次。

### 触发组合：主动取或、被动取且

组合规则是固定的，不再提供逻辑表达式：

- **主动型之间取「或」**：任一到点即进入条件检查 —— 用来把多个时间源合并进同一个任务（共享同一套被动条件与同一个冷却计时器）
- **被动型之间取「且」**：全部为真任务才执行
- **主动与被动之间取「且」**：即 `（任一主动到点）且（所有被动为真）`

因此**每个任务至少要有一个主动型触发器**，否则永远不会触发（页面会拒绝保存这样的任务）。页面用框图展示这层语义：`Start` 扇出到一排并排的主动型节点（或汇聚），再串行经过被动型节点（且串联），最后到 `Target`；点击任意节点即可在下方展开该触发器的配置面板，画布可拖动平移。

### 合并评估与"宕机不补发"

调度器采用**自适应唤醒**：主循环计算所有启用任务里最近的主动触发点，睡到该点再醒来处理（`asyncio.Event + wait_for`），而不是固定周期轮询。任务/配置变更会立即唤醒重算。每个 tick 内对每个启用的任务：

1. 检查所有主动触发器是否到点（基于各自 `last_fired`）；多个主动型是「或」关系
2. 若无任何主动触发器到点 → 跳过
3. 有主动触发器到点 → 实时求值所有被动触发器
4. 所有被动为真 → 执行任务；有任一被动为假 → 仅记一条"skipped"日志
5. **无论被动条件是否满足，到点的主动触发器的 `last_fired` 都会推进到本次触发点**，避免同一窗口内重复判定

关键正确性保证：

- **新建任务**：所有触发器 `last_fired = now`，所以首次触发从未来第一个点开始，不会一建好就立刻触发
- **更新任务**：按触发器 id 继承原 `last_fired`，编辑保存后不会重置导致重复触发
- **复制任务**：副本 `last_fired = now` 且 `enabled = false`（默认停用），避免启用后立即双触发
- **宕机恢复**：触发点取 `<= now` 的最近一个（而非所有跨越的点），所以**只触发一次，不补发历史**

### 任务内容

任务内容由一个**执行模式**（下拉菜单）+ 一段**文本** + 两个附加信息开关（「时间感知」「节假日感知」）组成。

| `mode` | 行为 |
|---|---|
| `fixed`（发送文本到对话） | 直接把文本作为消息发到所有目标，不经过 AI；不包裹、不注入时间 |
| `standalone`（调用独立 AI 回复） | 用该任务自己的「独立 AI 系统提示」（`content.system_prompt`，可为空）作为 system prompt + 包裹后的任务提示词，单独调用 AI 生成回复后发送，不注入对话人格、不与其他插件互动 |
| `conversation`（调用对话配置的 AI 回复） | 把包裹后的任务提示词交给目标对话配置的 AI 生成回复后发送，**携带该对话的人格 system prompt 与记忆插件注入** |

**两种空动作**：

| 情形 | 解释 | 是否推进冷却 |
|---|---|---|
| 内容留空（`text` 为空） | 空动作，视为**成功执行**了目标动作 | ✅ 是（更新 `last_success_time`） |
| 主动到点但被动条件未全部满足 | **跳过**，未执行动作 | ❌ 否 |

两者都不发送消息，只记日志（`action` 分别为 `noop` 与 `skipped`）。

「独立 AI 系统提示」在任务内容区填写，**任务独立**，可为空。

**对话 AI（conversation）调用流程**（对齐 AstrBot 主 agent 的请求构建）：

1. 取目标 UMO 当前配置的 `chat_provider_id`
2. 取该会话当前 conversation：历史消息 → `contexts`，会话级 `persona_id`
3. 取该 UMO 的 `provider_settings`（应用 `prompt_prefix`、默认人格）
4. 经 `persona_manager.resolve_selected_persona` 解析人格，注入 `system_prompt`（含人格指令）与 `begin_dialogs`
5. 广播 `on_llm_request` 钩子（走框架原生 `call_event_hook`，让记忆等其它插件注入）
6. 把注入后的 `system_prompt`、`contexts` 连同 `prompt` 一起调用 `context.llm_generate(chat_provider_id=..., ...)`，超时由 `llm_timeout` 控制
7. AI 空回复/超时/异常 → 记该目标失败（`ok:false`），**不降级为发送原文**

**独立 AI（standalone）调用流程**：不注入人格、不广播 `on_llm_request`，直接用**该任务的 `system_prompt`**（可为空）+ 包裹后的任务提示词调用 `context.llm_generate`；`chat_provider_id` 从第一个目标 UMO 的会话配置获取。所有目标共享同一次生成结果（不依赖会话人格）。

> `context.llm_generate` 自 AstrBot v4.5.7 起**强制要求** `chat_provider_id`（keyword-only）。插件通过 `context.get_current_chat_provider_id(umo=...)` 从指定 UMO 的会话配置正确获取，找不到会报失败而不是静默跳过。

### 提示词包裹（标准化）

两种 AI 模式的任务提示词在发送前都会被统一包裹；**用户提示词中不再支持任何参数**（写进去的 `{{...}}` 原样保留）。两个开关独立控制附加信息：

| 开关 | 作用 |
|---|---|
| 时间感知 | 附加「现在时间是 …」（年月日 + 周几 + 时分秒），默认开启 |
| 节假日感知 | 附加「今天是 …」（工作日/周末/节日/调休），默认关闭，需联网查询 |

四个开关组合下的实际文本（任务提示词为「提醒喝水」）：

```
# 时间感知开、节假日感知关
<scheduled_task>这是由计划任务自动触发的一次会话，现在时间是 2026年09月21日 星期一 20:34:57。请根据以下指令进行回复。你的回复将被直接发送给用户。提醒喝水</scheduled_task>

# 两个都开
<scheduled_task>这是由计划任务自动触发的一次会话，现在时间是 2026年09月21日 星期一 20:34:57，今天是工作日。请根据以下指令进行回复。你的回复将被直接发送给用户。提醒喝水</scheduled_task>

# 时间感知关、节假日感知开
<scheduled_task>这是由计划任务自动触发的一次会话，今天是“中秋节”假期。请根据以下指令进行回复。你的回复将被直接发送给用户。提醒喝水</scheduled_task>
```

包裹模板内的参数（仅模板自身使用，用户无需关心）：

| 参数 | 含义 | 示例 |
|---|---|---|
| `{{time}}` | 年月日 + 周几 + 时分秒 | `2026年09月21日 星期一 20:34:57` |
| `{{holiday_clause}}` | 节假日子句（含前导逗号，关闭或无数据时为空串） | `，今天是工作日` |
| `{{task_prompt}}` | 任务提示词原文 | — |

替换只对包裹模板生效一次，不二次扫描，因此用户提示词里的花括号不会被解析。「固定文本」模式直接发送原文，既不包裹也不注入附加信息（该模式下两个开关均为禁用状态）。

#### 节假日信息

数据来自第三方接口 `https://timor.tech/api/holiday/info/<YYYY-MM-DD>`（按天缓存，同一天只查一次），返回结果转成一句话：

| 接口返回 | 生成文本 |
|---|---|
| `holiday` 为 `null` 且 `type.type == 0`（工作日） | 今天是工作日 |
| `holiday` 为 `null` 且 `type.type == 1`（周末） | 今天是周六（取 `type.name`） |
| `holiday.holiday == false`（调休上班） | 今天是工作日，是“国庆节后补班”（取 `holiday.name`） |
| `holiday.holiday == true`（放假） | 今天是“中秋节”假期（取 `holiday.name`） |

查询失败（超时、限流、无网络）时该条消息不含节假日子句，不影响任务本身执行；只有查询成功的结果才会被缓存，下次触发会自动重试。

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

- 触发器 1（主动-`interval`）：`hours=4`（`base_time` 默认当天 0 点，即 0/4/8/12/16/20 点）
- 触发器 2（被动-`window`）：`start=08:00`、`end=20:00`、`weekdays=[]`
- 内容：`mode=fixed`、`text="喝水啦~"`
- 目标：你的好友/群 UMO

### 2. 随机问候

让 AI 在白天随机发条日常闲聊，每 2 小时一次机会，命中率 50%，两次发送至少间隔 4 小时。

- 触发器 1（主动-`interval`）：`hours=2`（每 2 小时一次机会）
- 触发器 2（被动-`window`）：`start=08:00`、`end=20:00`、`weekdays=[]`
- 触发器 3（被动-`random`）：`threshold=0.5`
- 触发器 4（被动-`cooldown`）：`hours=4`
- 内容：`mode=conversation`、开启「时间感知」、`text="给用户发送一条简短的日常消息，要求就像朋友间无聊时的日常闲聊消息或者问候一样。"`
- 目标：你的好友/群 UMO

`conversation` 模式会带上当前会话配置的人格 system prompt（通过 `on_llm_request` 广播注入）。若希望 AI 不携带人格、独立生成，改用 `standalone` 模式。

### 3. 多个时间源（主动取或）

每 6 小时一次机会，另外每天 12:00 必须有一次；只在 09:00–23:00 之间发。

- 触发器 1（主动-`interval`）：`hours=6`
- 触发器 2（主动-`cron`）：`expr="0 12 * * *"`
- 触发器 3（被动-`window`）：`start=09:00`、`end=23:00`
- 内容：`mode=conversation`、开启「时间感知」
- 目标：UMO

两个主动型是「或」关系：任一到点都会检查窗口；由于在**同一个任务**里，它们共享同一个 `cooldown` 计时器——这正是把多个时间源合并进一个任务的意义。

### 4. cron 兼容

完全用 cron 表达式，每天 7:30 发一条固定早安：

- 触发器 1（主动-`cron`）：`expr="30 7 * * *"`
- 内容：`mode=fixed`、`text="早安。"`
- 目标：UMO

### 5. 空动作（仅记日志）

内容留空即为空动作：触发后仅记一条日志，不发送消息、也不推进冷却计时器。可用于观察触发器是否按预期到点、被动条件是否满足。

- 内容：`text=""`（任意模式均可）
- 此时发送对象可为空

> 注意：每个任务的 `cooldown` 计时器是独立的，不能用本任务给另一个任务的 cooldown 提供基准时间。

---

## 日志

每个任务在**主动触发器到点时**都会记一条日志（无论被动条件是否全部满足、是否发送成功）。此外，点击任务列表的「触发」按钮会记一条 `source="manual"` 的手动触发日志（`logic_result` 为 `null`）：

```json
{
  "time": 1725412300.0,
  "task_id": "uuid",
  "task_name": "喝水提醒",
  "trigger_tof": {"1": true, "2": true},      // 各触发器本次 ToF
  "logic_result": true,                        // 被动条件是否全部满足（手动触发为 null；字段名为历史遗留）
  "action": "send_fixed",                      // send_llm | send_fixed | noop | partial | failed | skipped
  "targets_result": [{"umo":"default:FriendMessage:xxx","ok":true,"error":""}],
  "detail": "成功",
  "source": "manual"                           // 可选，手动触发时存在
}
```

日志存在内存里（持久化到 `data_dir/tasks.json` 的 `logs` 字段），保留最近 `log_retention` 条。页面有日志查看面板。

---

## LLM 调用日志（一记录一文件）

当任务内容使用「独立 AI」或「对话 AI」模式时，每次真正调用 AI 都会在 `data_dir/llm_logs/rec/` 下写**一条记录一个文件**，成功与失败都记。文件包含：

- **首行**：元数据（`time`、`task_id`、`task_name`、`source`、`mode`、`umo`、`ok`、`duration_ms`、`error`、`usage`、正文体积、图片清单、单行预览），列表只需读这一行
- **次行起**：正文（`request` 完整请求体 + `response` AI 回复，图片位置为 `[img:<md5>.<ext>:<字节数>]` 占位标记，图片本体存放在 `data_dir/llm_logs/img/`）

文件名形如 `20260920-205903.123_随机问候_000042.json`：时间前缀（毫秒精度、定宽）保证字典序即时间序，任务名便于肉眼识别，序号保证同毫秒同名不冲突。**没有单独的索引文件**——"最近 N 条"直接由文件名排序得到。

**图片内容寻址（去重）**：写入时把记录中的 base64 图片抽离为 `img/<md5>.<ext>`，同一张图片在任何记录、任何任务中只存一份；正文里只留占位标记。因此相邻记录（如 `B = A + 几段新对话`）共用的图片不会重复占用磁盘，也不会重复传输。解码失败的段落原样保留，不丢数据。

说明：

- 记录的是**广播注入后、真正交给 `context.llm_generate` 的最终请求体**——`conversation` 模式在调用前会注入人格 + 会话历史并广播 `on_llm_request`，故 `system_prompt`/`contexts` 已包含人格、记忆等插件注入；`standalone` 模式不注入人格、不广播，`system_prompt` 为该任务自己的提示。
- **过期管理**：记录数超过 2 倍 `llm_log_retention` 时裁剪到最近 N 条（删除最旧的记录文件），随后扫描剩余记录首行元数据，回收不再被任何记录引用的图片。
- **旧数据迁移**：启动时若发现旧版单文件 `llm_calls.jsonl`，会自动拆分为逐条记录文件并抽离图片，迁移完成后原文件改名为 `llm_calls.jsonl.migrated` 保留（可手动删除）。

### 页面行为

「LLM 调用日志」标签页按页浏览（每页 10 条，可上一页/下一页）：

1. 进入页面只请求当页元数据，立即渲染整页折叠卡片骨架（时间、任务名、模式、UMO、体积、耗时、预览），显示"加载中"状态位
2. 随后**串行逐条**获取正文，加载一条即就绪一条、可展开一条，直到本页全部就绪
3. 正文文本在卡片**首次展开**时才渲染进页面，避免多条大体积文本同时渲染造成卡顿
4. 图片位置显示徽标（格式 + 体积），**点击徽标才真正加载图片**；同一图片第二次出现直接命中浏览器缓存，不再请求
5. 已加载的正文与图片在浏览器端按内容缓存：翻回看过的页、重复的图片零请求；切换页码或刷新会作废在途请求

---

## 文件结构

```
astrbot_plugin_enhanced_scheduler/
├── metadata.yaml              # 插件元数据（版本、平台、作者）
├── _conf_schema.json          # 插件级配置 schema
├── requirements.txt           # croniter
├── scheduler_core.py          # 纯逻辑核心：触发器校验/求值、下次触发计算、合并评估
├── main.py                    # AstrBot 集成：持久化、轮询、执行、LLM 调用、Web API
└── pages/enhanced_scheduler/
    ├── index.html             # 配置页面骨架
    ├── index.js               # 前端逻辑：任务列表、编辑弹窗、触发器框图编排与实时校验
    └── style.css              # 亮/暗主题适配
```

持久化数据写在 `data/plugins/astrbot_plugin_enhanced_scheduler/` 下（AstrBot 推荐的 `StarTools.get_data_dir` 路径），不在插件自身目录里——升级/重装不会丢配置：

- `tasks.json`：任务与内存日志
- `llm_logs/rec/<时间>_<任务名>_<序号>.json`：LLM 调用记录，一条一个文件（首行元数据 + 正文）
- `llm_logs/img/<md5>.<ext>`：记录中的图片，按内容寻址，跨记录复用
- `llm_calls.jsonl.migrated`：旧版单文件日志的迁移备份（仅升级时出现，可手动删除）

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
| `/validate` | POST | 校验触发器组，返回每个主动触发器的未来触发点与整体下次检查时间 |
| `/preview_next` | POST | 给定触发器列表，返回下次触发时间 |
| `/trigger_now` | POST | 立即手动触发一次任务（忽略触发规则与启用状态，仅执行内容并发送） |
| `/get_llm_logs` | GET | 轻量元数据分页（`limit` 默认 10、`offset`，最新在前），只读记录文件首行 |
| `/get_llm_log_detail` | GET | 按文件名取单条记录正文（图片为占位标记，已被裁剪时返回过期错误） |
| `/get_llm_image` | GET | 按内容寻址取图片（`name=<md5>.<ext>`），返回 data URL |
| `/clear_llm_logs` | POST | 清空全部 LLM 调用记录文件与图片池 |

## 已知限制

- 组合规则固定为「主动取或、被动取且」，不支持更复杂的布尔组合（如需多组条件并联，请拆成多个任务）
- 每个任务必须至少有一个主动型触发器，否则不会触发
- 被动触发器（`random`/`cooldown`）的"下次触发时间"无法预测，页面预览只考虑主动触发器
- `random` 在每次主动触发器到点时都会被求值一次，采样本身无副作用；阈值设置过高会显著降低该任务的执行频率
- `cooldown` 依赖**本任务**的 `last_success_time`（各任务独立）。新任务该值为 0，所以新任务首次触发时 `cooldown` 恒为真；"内容留空"空动作视为成功会推进冷却，而"被动条件未满足"（skip）与发送失败不推进冷却
- 「节假日感知」依赖第三方接口 `timor.tech`：查询失败（限流/无网络）时该条消息不含节假日子句；接口按天缓存，同一天只查一次；仅支持中国的法定节假日与调休
- 触发时间精度为秒级（自适应调度按最近触发点唤醒，误差在秒级）
- 跨天 `window` 区间（如 `22:00-06:00`）解析为 `22:00-23:59` ∪ `00:00-06:00` 两段
- `conversation` 模式由本插件自行解析人格（`persona_manager`）+ 会话历史并广播 `on_llm_request` 钩子；若会话从未创建过对话，则回退到该 UMO 配置里的默认人格，`contexts` 为空。该模式不注入 skills/tools。`standalone` 模式不注入人格、不广播，恒使用本插件的 system prompt

## 版本

- `1.3.0`
