# astrbot_plugin_matrix_rule_react

独立的 Matrix 条件 Reaction 插件。它承接原先位于
`astrbot_plugin_matrix_adapter` 中的预回应逻辑，让适配器只负责 Matrix
事件转换与 Reaction 发送能力。

## 依赖与安装

- AstrBot `>=4.16.0`
- 已安装并启用 `astrbot_plugin_matrix_adapter`
- 仅处理 `matrix` 平台的群聊与私聊消息

将本目录作为 AstrBot 插件安装后，在插件配置页显式开启本插件；默认关闭，不会发送
Reaction。

## 代码结构

- `main.py`：插件入口、消息处理流程和管理员指令；
- `rules.py`：条件数组解析、旧规则兼容、规则匹配和动态 Reaction 选择；
- `trigger_filter.py`：原有 `@机器人` / `wake_prefix` 唤醒条件过滤。

## 触发规则

动态规则按配置顺序匹配，首个命中的规则生效，每条消息最多发送一个
Reaction。同一规则内可以放置任意多个条件，所有条件都命中（AND）后规则才生效：

- `keyword`：原始消息文本包含指定关键字（区分大小写）；
- `regex`：正则表达式在原始消息文本中匹配；
- `user_id`：发件人完整 Matrix ID 与配置值相同；
- `bot_id`：当前 Matrix 机器人 ID 与配置值相同；
- `group_id`：群聊的完整 Matrix room ID 与配置值相同；
- `message_type`：匹配 `group` / `private` 等 AstrBot 消息类型，也可以匹配
  Matrix 原始 `msgtype`（例如 `m.text`）。

规则可以使用 `fixed` 固定 Reaction，或使用 `random` 从规则的 Reaction
列表中随机选取。如果没有动态规则命中，继续使用原有唤醒规则，从全局
`emojis` 列表随机选择：

- 消息链明确 `@` 当前 Matrix 机器人；
- 原始消息以当前会话使用的 AstrBot 全局 `wake_prefix` 开头，并且 AstrBot 已将它识别为
  有效唤醒消息。

插件使用适配器提供的标准 `event.react()` 接口，不访问适配器私有配置。不匹配动态规则且未
显式唤醒的普通消息、机器人自身消息、缺少 Matrix event ID 的消息，以及空
Reaction 列表都不会触发。发送失败只记录日志，不中断后续消息处理。

## 管理员指令

所有动态规则指令均位于 `/matrix rules react` 指令组下，并且需要 AstrBot
管理员权限。

```text
/matrix rules react add <fixed|random> <Reaction 列表> (<keyword|regex|user_id|bot_id|group_id|message_type> <匹配内容>)[]
/matrix rules react list
/matrix rules react remove <规则编号>
```

`Reaction 列表` 使用英文逗号分隔。`fixed` 模式必须且只能提供一项；
`random` 模式可以提供多项。后面的条件数组至少需要一项，数量不设上限；推荐用
`(...)` 包住每一项，这样带空格或括号的关键字、正则也可以被准确解析。不写括号的
`keyword 部署完成 group_id !room:example.org` 形式也受支持；如果匹配内容本身包含一个
完整的条件类型单词，请用括号或引号避免歧义。

```text
/matrix rules react add fixed 👍 (keyword 部署完成)
/matrix rules react add random 👍,🎉 (regex ^build\s+(passed|success)$) (group_id !ci:example.org)
/matrix rules react add fixed 👋 (user_id @alice:example.org) (bot_id @helper:example.org) (message_type group)
/matrix rules react list
/matrix rules react remove 2
```

指令会立即更新并持久化插件配置。动态规则仍受 `matrix_rule_react.enable`
总开关控制；如果插件未启用，`add` 的返回消息会明确提示。

## 配置

```json
{
  "matrix_rule_react": {
    "enable": true,
    "emojis": ["🤗", "🐟", "🍞", "mxc://example.org/media-id"],
    "rules": [
      {
        "selection": "fixed",
        "reactions": ["👍"],
        "conditions": [
          {"match_type": "keyword", "pattern": "部署完成"},
          {"match_type": "group_id", "pattern": "!ci:example.org"}
        ]
      },
      {
        "selection": "random",
        "reactions": ["👍", "🎉"],
        "conditions": [
          {"match_type": "regex", "pattern": "^build\\s+(passed|success)$"},
          {"match_type": "message_type", "pattern": "group"}
        ]
      }
    ]
  }
}
```

`emojis` 同时支持普通 Unicode 表情和 Matrix 客户端支持的自定义 Reaction key。运行时
会去除首尾空白、忽略空值并去重。旧版单条件规则中的 `match_type` / `pattern`
仍可继续运行；新指令统一写入 `conditions` 数组。

## 从 Matrix 适配器迁移

适配器原配置项 `matrix_pre_ack_emoji` 已移除。升级后请在本插件配置中将：

- `matrix_pre_ack_emoji.enable` 迁移为 `matrix_rule_react.enable`；
- `matrix_pre_ack_emoji.emojis` 迁移为 `matrix_rule_react.emojis`。

不要在两个插件中重复配置；Reaction 的条件判断与随机选择现只由本插件负责。
