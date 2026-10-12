# 增强计划任务 · Enhanced Scheduler

<img src="https://raw.githubusercontent.com/3plus10i/astrbot_plugin_enhanced_scheduler/refs/heads/main/logo.png" alt="增强计划任务 Logo" width="128" height="128">

**让 AstrBot 按时提醒，也能偶尔主动找你聊聊。**

从每天一句早安，到只在白天出现的随机问候：把确定时间和概率条件组合起来，发送固定文本，或让 AI 带着会话人格主动发言。独立调度，无需开启 AstrBot 原生「主动能力」。

**界面预览**
<div align="center">
  <table>
    <tr>
      <td align="center">
        <img src="https://raw.githubusercontent.com/3plus10i/astrbot_plugin_enhanced_scheduler/refs/heads/main/screenshot-page-1.5.2.png" width="500" alt="截图1"/>
        <br/>
        <sub>图 1：任务列表截图</sub>
      </td>
      <td align="center">
        <img src="https://raw.githubusercontent.com/3plus10i/astrbot_plugin_enhanced_scheduler/refs/heads/main/screenshot-task-1.5.2.png" width="500" alt="截图2"/>
        <br/>
        <sub>图 2：任务触发器配置截图</sub>
      </td>
    </tr>
  </table>
</div>


[快速开始](#快速开始) · [配置与进阶指南](docs/guide.md) · [更新日志](CHANGELOG.md)

## 可以用它做什么？

| 想要的效果 | 配置思路 |
| --- | --- |
| 每天 07:30 发一句早安 | cron 定时 + 固定文本 |
| 白天每 4 小时提醒喝水 | 周期触发 + 08:00–20:00 时间区间 |
| 偶尔主动问候，又不会太频繁 | 周期触发 + 时间区间 + 随机概率 + 冷却时间 |

## 为什么用它？

- **触发条件可组合**：周期、cron、时间区间、随机、冷却与 AI 语境判定，兼顾准时提醒与自然互动。
- **消息内容可选择**：直接发送文本、使用独立 AI 提示词，或沿用目标会话的人格和上下文。
- **全程页面配置**：在 WebUI 新建、编辑、复制任务，查看下次任务时间与执行概率。
- **执行过程可查看**：支持手动触发，运行日志可通过“查看 JSON”检查原始判断依据；另外提供 AI 请求与回复记录。

## 快速开始

需要 **AstrBot ≥ 4.27.0**；当前声明支持 **aiocqhttp**。

1. 在 AstrBot 插件市场搜索「增强计划任务」并安装，也可通过[仓库地址](https://github.com/3plus10i/astrbot_plugin_enhanced_scheduler)安装。
2. 打开 AstrBot WebUI 的 **Pages**，进入本插件页面，点击 **「+ 新建任务」**。
3. 填写名称、选择发送对象，添加一个周期或 cron 触发器，设置任务内容并保存。
4. 在任务的更多操作菜单中点击 **「触发一次」**，检查消息和运行日志。

任务管理全部在页面完成，不提供聊天指令。使用 AI 模式前，请先为目标会话配置聊天模型。

## 配置怎么选？

**什么时候发？** 周期或 cron 提供触发机会，时间区间、随机和冷却进一步筛选。多个主动触发器满足任意一个即可，被动条件则须全部满足。

![触发器组合逻辑：任一主动触发且全部被动条件满足时执行任务](https://raw.githubusercontent.com/3plus10i/astrbot_plugin_enhanced_scheduler/refs/heads/main/触发器组合逻辑示意图.png)
> *触发器组合逻辑：任一主动触发且全部被动条件满足时执行任务*

例如「随机问候」：每 2 小时检查一次，仅在 08:00–20:00 内，以 50% 概率执行；距离上次成功执行至少 4 小时。选择「对话 AI」，提示词填入「给用户发送一条简短的日常问候」，即可让 bot 用当前会话的人格生成消息。

**发什么？**

| 模式 | 适合的用途 |
| --- | --- |
| 固定文本 | 提醒、通知；直接发送填写的文字 |
| 独立 AI | 用任务自己的系统提示生成内容，不带会话人格与上下文 |
| 对话 AI | 沿用会话人格与上下文，最自然的主动发言模式 |

AI 模式可开启**时间感知**和**中国节假日感知**。每个任务对应一个发送对象，发往多个对话需分别建任务。

更详细的规则、JSON 配置和插件设置见[配置与进阶指南](docs/guide.md)。

## 升级提醒

- **仍在使用 1.3.x？** 请及时升级，该系列存在触发失败后日志持续增长、严重时占满服务器硬盘导致整个服务器异常的问题。
- **旧格式数据尚未迁移？** 请先安装 **1.4.3** 并确认任务配置，再升级。**1.5.1 起不再自动迁移旧格式任务和日志**；已迁移的当前格式数据可继续使用。新用户不受影响。
