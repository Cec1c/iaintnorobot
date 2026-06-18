# 部署说明

## 前置条件

- 已安装并运行 AstrBot。
- AstrBot 已配置可用的 LLM Provider。
- 机器人已接入目标 QQ 群或其他 AstrBot 支持的群聊平台。

## 安装

将插件目录放入 AstrBot 插件目录：

```text
data/plugins/astrbot_plugin_i_aint_no_robot
```

目录结构应类似：

```text
astrbot_plugin_i_aint_no_robot/
  main.py
  metadata.yaml
  _conf_schema.json
  README.md
  docs/
```

然后在 AstrBot WebUI 中重载插件。

## 配置

推荐先配置一个或少量群：

```text
managed_groups = 已添加群聊
```

推荐在 AstrBot WebUI 的插件配置面板里先添加群聊。`managed_groups` 里启用的群都会作为目标群运行；不再需要单独选择“当前目标群”。

也可以在目标群里使用：

```text
/iar addgroup
```

原型阶段建议保持低频：

```text
scan_interval_seconds = 60
min_attempt_interval_minutes = 20
max_attempt_interval_minutes = 80
no_reply_retry_minutes = 3
min_speak_interval_minutes = 45
speak_probability = 0.35
```

这样插件会比较安静。需要测试时可以用 `/iar say` 强制尝试生成一句。

### WebUI 关键配置

```text
managed_groups
```

已添加群聊列表。插件只会在这里添加且启用的群聊中运行。每个群条目里的“是否在该群启用”就是该群的开关。

```text
min_attempt_interval_minutes
```

最小发起发言周期。达到周期后，插件仍会先判断有没有可附和或可讨论的话题。

```text
no_reply_retry_minutes
```

未触发时重试间隔。条件不满足、概率没中、已有待回复任务时，不会进入 20-80 分钟长冷却，只隔这段时间再看。

```text
allow_self_start
self_start_probability
self_start_style_examples
```

控制“无话题时自创一句”。自创句子会偏模糊感受或情绪表达，例如“ok今天已到账五个大饼”。建议概率保持较低。

```text
handle_mentions
mention_reply_max_chars
mention_reply_probability
handle_implicit_replies
implicit_reply_probability
implicit_reply_window_seconds
reply_delay_min_seconds
reply_delay_max_seconds
reply_stale_seconds
stop_default_mention_reply
mention_passthrough_patterns
```

控制群内直接艾特。开启后，被艾特时会使用插件的人味短句回复；默认阻止 AstrBot 标准 LLM 回复，避免冒出一大段客服腔。

这个插件的艾特处理器优先级会压过 AstrBot 内置空白艾特等待器，用来抢在默认 LLM 前面接管。空白艾特会直接给极短兜底，不再启动“等用户下一条消息再注入艾特”的内置逻辑。`mention_passthrough_patterns` 是命令放行规则，一行一个正则；命中后本插件不回复也不阻断，例如 `/在线`、`重启服务器`。

`handle_implicit_replies` 用于处理“没有 @，但可能是在跟机器人说话”的消息。判断依据包括机器人刚说完后的短句、二人称追问、提到 AI/机器人/插件/默认回复等。命中后仍会交给 LLM 判断，不确定就输出 `SILENT`。

主动插话触发后不会立刻回复，会在 `reply_delay_min_seconds` 到 `reply_delay_max_seconds` 之间随机等待。到点后会重新读取最新群聊；如果最近消息已经超过 `reply_stale_seconds`，就不回复旧话题。直接艾特会立即短回，用来压住 AstrBot 默认标准回复。

```text
continue_viewpoint
```

控制表达线索记忆。开启后，机器人会压缩自己最近想表达的态度，下一次短句可以顺着这个方向，不至于每条都散开。

```text
learn_slang
slang_scan_interval_minutes
max_slang_terms
```

控制黑话学习。插件只会把已确认含义的黑话交给发言模型，不确定的会避开使用。

```text
enable_insider
insider_qq
insider_question_cooldown_minutes
```

控制内线询问。内线用于解释不确定黑话、游戏机制或图片/表情包上下文。当前实现会尽量尝试 OneBot/aiocqhttp 常见私信接口；如果平台不支持或内线不回复，插件会自动降级为不使用相关黑话。

发给内线的私信由 LLM 根据上下文生成，程序不会硬拼模板话术。

## 常用指令

```text
/iar status   查看状态
/iar on       开启当前群
/iar off      关闭当前群
/iar addgroup 把当前群加入并启用
/iar groups   查看已添加群聊
/iar say      强制尝试生成一句，方便测试
/iar summary  手动刷新群语境记忆
/iar slang    查看黑话记忆
/iar learn    手动扫描黑话
/iar reset    清空当前群的插件记忆
```

## 存储与资源占用

插件使用 Python 标准库 SQLite，数据库位于 AstrBot 插件数据目录：

```text
data/plugin_data/astrbot_plugin_i_aint_no_robot/memory.sqlite3
```

默认策略：

- 每群最多保留 200 条短文本消息。
- 黑话记忆默认最多 80 条。
- 不保存图片、语音、完整事件 JSON。
- 消息监听阶段不调用 LLM。
- 只有周期检查通过本地规则后才调用 LLM。
- 黑话学习默认 180 分钟扫描一次，避免频繁调用 LLM。

轻量使用时，存储通常是 MB 级别，内存占用也很低。

## 调试建议

1. 先把插件装进一个测试群。
2. 在群里正常聊几句，让插件积累短期上下文。
3. 使用 `/iar status` 查看是否记录到群。
4. 使用 `/iar summary` 手动刷新语境摘要。
5. 使用 `/iar learn` 手动扫描黑话。
6. 使用 `/iar slang` 查看黑话记忆。
7. 使用 `/iar say` 测试短句生成。

如果 `/iar say` 一直返回“这会儿没啥好接的”，通常是模型输出被过滤掉了，比如太长、太像 AI、带 Markdown 或承认了机器人身份。
