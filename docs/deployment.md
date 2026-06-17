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

推荐先只配置一个群：

```text
target_group_id = 目标群号
```

如果留空，插件会自动锁定第一个观察到的群。

原型阶段建议保持低频：

```text
scan_interval_seconds = 60
min_attempt_interval_minutes = 20
max_attempt_interval_minutes = 80
min_speak_interval_minutes = 45
speak_probability = 0.35
```

这样插件会比较安静。需要测试时可以用 `/iar say` 强制尝试生成一句。

### WebUI 关键配置

```text
target_group_id
```

目标群号。当前原型只服务一个群。

```text
min_attempt_interval_minutes
```

最小发起发言周期。达到周期后，插件仍会先判断有没有可附和或可讨论的话题。

```text
allow_self_start
self_start_probability
self_start_style_examples
```

控制“无话题时自创一句”。自创句子会偏模糊感受或情绪表达，例如“ok今天已到账五个大饼”。建议概率保持较低。

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

## 常用指令

```text
/iar status   查看状态
/iar on       开启当前群
/iar off      关闭当前群
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

单群使用时，存储通常是 MB 级别，内存占用也很低。

## 调试建议

1. 先把插件装进一个测试群。
2. 在群里正常聊几句，让插件积累短期上下文。
3. 使用 `/iar status` 查看是否记录到群。
4. 使用 `/iar summary` 手动刷新语境摘要。
5. 使用 `/iar learn` 手动扫描黑话。
6. 使用 `/iar slang` 查看黑话记忆。
7. 使用 `/iar say` 测试短句生成。

如果 `/iar say` 一直返回“这会儿没啥好接的”，通常是模型输出被过滤掉了，比如太长、太像 AI、带 Markdown 或承认了机器人身份。
