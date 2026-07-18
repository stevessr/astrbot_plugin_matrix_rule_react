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

## 触发规则

以下任一条件成立时，从配置列表随机选择一个 Reaction key：

- 消息链明确 `@` 当前 Matrix 机器人；
- 原始消息以当前会话使用的 AstrBot 全局 `wake_prefix` 开头，并且 AstrBot 已将它识别为
  有效唤醒消息。

插件使用适配器提供的标准 `event.react()` 接口，不访问适配器私有配置。普通消息、仅在
私聊中隐式唤醒的消息、机器人自身消息、缺少 Matrix event ID 的消息，以及空 Reaction
列表都不会触发。发送失败只记录日志，不中断后续消息处理。

## 配置

```json
{
  "matrix_rule_react": {
    "enable": true,
    "emojis": ["🤗", "🐟", "🍞", "mxc://example.org/media-id"]
  }
}
```

`emojis` 同时支持普通 Unicode 表情和 Matrix 客户端支持的自定义 Reaction key。运行时
会去除首尾空白、忽略空值并去重。

## 从 Matrix 适配器迁移

适配器原配置项 `matrix_pre_ack_emoji` 已移除。升级后请在本插件配置中将：

- `matrix_pre_ack_emoji.enable` 迁移为 `matrix_rule_react.enable`；
- `matrix_pre_ack_emoji.emojis` 迁移为 `matrix_rule_react.emojis`。

不要在两个插件中重复配置；Reaction 的条件判断与随机选择现只由本插件负责。
