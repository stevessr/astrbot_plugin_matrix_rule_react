"""Send Matrix reactions for messages that match configured rules."""

import random
import re

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr

MATCH_TYPES = frozenset(
    {
        "keyword",
        "regex",
        "user_id",
        "bot_id",
        "group_id",
        "message_type",
    }
)
MATCH_TYPE_ALIASES = {
    "user": "user_id",
    "userid": "user_id",
    "bot": "bot_id",
    "botid": "bot_id",
    "group": "group_id",
    "groupid": "group_id",
    "message": "message_type",
    "msg_type": "message_type",
}
MESSAGE_TYPE_ALIASES = {
    "group": "group",
    "groupmessage": "group",
    "group_message": "group",
    "private": "private",
    "private_message": "private",
    "friend": "private",
    "friendmessage": "private",
    "friend_message": "private",
    "direct": "private",
    "dm": "private",
    "other": "other",
    "othermessage": "other",
    "other_message": "other",
}
_CONDITION_MARKER_PATTERN = re.compile(
    rf"(?<!\S)(?:{'|'.join(sorted(MATCH_TYPES, key=len, reverse=True))})(?=\s|$)",
    re.IGNORECASE,
)


def _parse_conditions(value: object) -> list[dict[str, str]]:
    """Parse the variable-length condition tail of the ``add`` command.

    Parenthesized conditions are parsed with balanced parentheses so regular
    expressions can contain capture groups. A flat sequence is also accepted and
    split whenever another supported condition type begins.

    Args:
        value: Raw trailing command text captured by ``GreedyStr``.

    Returns:
        Normalized condition dictionaries in their command order.

    Raises:
        ValueError: The condition syntax, type, content, or regex is invalid.
    """
    raw_value = str(value or "").strip()
    if not raw_value:
        raise ValueError("至少需要提供一个匹配条件。")

    if raw_value.startswith("("):
        raw_conditions: list[str] = []
        index = 0
        while index < len(raw_value):
            while index < len(raw_value) and raw_value[index].isspace():
                index += 1
            if index >= len(raw_value):
                break
            if raw_value[index] != "(":
                raise ValueError("括号条件之间不能包含其他内容。")

            start = index + 1
            index += 1
            depth = 1
            quote = ""
            escaped = False
            while index < len(raw_value):
                char = raw_value[index]
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif quote:
                    if char == quote:
                        quote = ""
                elif char in {'"', "'"}:
                    quote = char
                elif char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        break
                index += 1

            if depth != 0:
                raise ValueError("匹配条件括号未闭合。")
            condition_text = raw_value[start:index].strip()
            if not condition_text:
                raise ValueError("匹配条件不能为空。")
            raw_conditions.append(condition_text)
            index += 1
    else:
        masked_value = list(raw_value)
        quote = ""
        escaped = False
        for index, char in enumerate(raw_value):
            if escaped:
                if quote and not char.isspace():
                    masked_value[index] = "_"
                escaped = False
                continue
            if char == "\\":
                if quote:
                    masked_value[index] = "_"
                escaped = True
                continue
            if quote:
                if char == quote:
                    quote = ""
                if not char.isspace():
                    masked_value[index] = "_"
                continue
            if char in {'"', "'"}:
                quote = char
                masked_value[index] = "_"
        if quote:
            raise ValueError("匹配内容的引号未闭合。")

        matches = list(_CONDITION_MARKER_PATTERN.finditer("".join(masked_value)))
        if not matches or raw_value[: matches[0].start()].strip():
            raise ValueError(
                "匹配条件必须以 keyword、regex、user_id、bot_id、group_id "
                "或 message_type 开始。"
            )
        raw_conditions = []
        for index, current in enumerate(matches):
            end = (
                matches[index + 1].start()
                if index + 1 < len(matches)
                else len(raw_value)
            )
            raw_conditions.append(raw_value[current.start() : end].strip())

    conditions: list[dict[str, str]] = []
    for raw_condition in raw_conditions:
        parts = raw_condition.split(maxsplit=1)
        match_type = str(parts[0] or "").strip().lower()
        match_type = MATCH_TYPE_ALIASES.get(match_type, match_type)
        if match_type not in MATCH_TYPES:
            raise ValueError(f"规则类型无效：{parts[0]}。")
        pattern = parts[1].strip() if len(parts) > 1 else ""
        if len(pattern) >= 2 and pattern[0] == pattern[-1] and pattern[0] in {'"', "'"}:
            pattern = pattern[1:-1].strip()
        if not pattern:
            raise ValueError(f"{match_type} 的匹配内容不能为空。")
        if match_type == "regex":
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"正则表达式无效：{exc}") from exc
        conditions.append({"match_type": match_type, "pattern": pattern})

    if not conditions:
        raise ValueError("至少需要提供一个匹配条件。")
    return conditions


def _format_conditions(conditions: list[dict[str, str]]) -> str:
    """Format normalized conditions for command responses.

    Args:
        conditions: Normalized condition dictionaries.

    Returns:
        A compact conjunction suitable for add, list, and remove results.
    """
    return " & ".join(
        f"{condition['match_type']}={condition['pattern']!r}"
        for condition in conditions
    )


class MatrixRuleReactTriggerFilter(filter.CustomFilter):
    """Match an explicit bot mention or a valid AstrBot wake-prefix message."""

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        """Check whether an event matches the original AstrBot wake rule.

        Args:
            event: Incoming AstrBot message event.
            cfg: Active AstrBot configuration for the event.

        Returns:
            Whether the event is an explicit bot mention or valid wake-prefix message.
        """
        if not getattr(event, "is_at_or_wake_command", False):
            return False

        self_id = str(event.get_self_id() or "")
        if self_id:
            for segment in event.get_messages() or ():
                if not isinstance(segment, At):
                    continue
                target = getattr(segment, "qq", None)
                if target is None:
                    target = getattr(segment, "user_id", None)
                if target is not None and str(target) == self_id:
                    return True

        message_obj = getattr(event, "message_obj", None)
        message_text = getattr(message_obj, "message_str", None)
        if message_text is None:
            message_text = event.get_message_str()
        message_text = str(message_text or "").strip()
        if not message_text:
            return False

        wake_prefixes = cfg.get("wake_prefix", ["/"])
        if isinstance(wake_prefixes, str):
            wake_prefixes = [wake_prefixes]
        elif not isinstance(wake_prefixes, (list, tuple, set)):
            wake_prefixes = ["/"]

        return any(
            message_text.startswith(prefix)
            for prefix in (str(item or "") for item in wake_prefixes)
            if prefix
        )


class MatrixRuleReactPlugin(Star):
    """React to Matrix messages that match wake or administrator-defined rules."""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        """Initialize the plugin with its persisted configuration.

        Args:
            context: AstrBot plugin context.
            config: Plugin configuration loaded from ``_conf_schema.json``.
        """
        super().__init__(context, config)
        self.config = config if isinstance(config, dict) else {}

    @staticmethod
    def _rule_conditions(raw_rule: dict) -> list[dict[str, str]] | None:
        """Normalize new multi-condition and legacy single-condition rules.

        Args:
            raw_rule: Persisted reaction rule from plugin configuration.

        Returns:
            Normalized conditions, or ``None`` when the rule is invalid.
        """
        raw_conditions = raw_rule.get("conditions")
        if not isinstance(raw_conditions, list) or not raw_conditions:
            legacy_type = raw_rule.get("match_type")
            legacy_pattern = raw_rule.get("pattern")
            if legacy_type is None and legacy_pattern is None:
                return None
            raw_conditions = [
                {"match_type": legacy_type, "pattern": legacy_pattern},
            ]

        conditions: list[dict[str, str]] = []
        for raw_condition in raw_conditions:
            if isinstance(raw_condition, str):
                try:
                    conditions.extend(_parse_conditions(raw_condition))
                except ValueError:
                    return None
                continue
            if not isinstance(raw_condition, dict):
                return None

            match_type = (
                str(raw_condition.get("match_type", raw_condition.get("type")) or "")
                .strip()
                .lower()
            )
            match_type = MATCH_TYPE_ALIASES.get(match_type, match_type)
            pattern = str(
                raw_condition.get("pattern", raw_condition.get("value")) or ""
            ).strip()
            if match_type not in MATCH_TYPES or not pattern:
                return None
            conditions.append({"match_type": match_type, "pattern": pattern})

        return conditions or None

    def _select_reaction(
        self,
        event: AstrMessageEvent,
        raw_config: dict,
    ) -> str:
        """Select the reaction for the first matching dynamic or wake rule.

        Dynamic rules take precedence over the original AstrBot wake rule. This keeps
        each Matrix event limited to one reaction even when multiple rules match.

        Args:
            event: Incoming Matrix message event.
            raw_config: ``matrix_rule_react`` plugin configuration.

        Returns:
            Selected reaction key, or an empty string when no rule matches.
        """
        message_obj = getattr(event, "message_obj", None)
        message_text = getattr(message_obj, "message_str", None)
        if message_text is None:
            message_text = event.get_message_str()
        message_text = str(message_text or "")
        sender_id = str(event.get_sender_id() or "")
        bot_id = str(event.get_self_id() or "")
        get_group_id = getattr(event, "get_group_id", None)
        group_id = str(get_group_id() or "") if callable(get_group_id) else ""
        raw_message_types: list[object] = []
        get_message_type = getattr(event, "get_message_type", None)
        if callable(get_message_type):
            try:
                raw_message_types.append(get_message_type())
            except Exception as exc:
                logger.debug("Failed to read Matrix event message type: %s", exc)
        raw_message_types.append(getattr(message_obj, "type", None))
        raw_message = getattr(message_obj, "raw_message", None)
        if isinstance(raw_message, dict):
            raw_message_types.append(raw_message.get("msgtype"))
            raw_content = raw_message.get("content")
        else:
            raw_message_types.append(getattr(raw_message, "msgtype", None))
            raw_content = getattr(raw_message, "content", None)
        if isinstance(raw_content, dict):
            raw_message_types.append(raw_content.get("msgtype"))

        message_types: set[str] = set()
        for raw_type in raw_message_types:
            if raw_type is None:
                continue
            for candidate in (
                getattr(raw_type, "value", None),
                getattr(raw_type, "name", None),
                raw_type,
            ):
                normalized_type = str(candidate or "").strip().lower().replace("-", "_")
                if normalized_type:
                    message_types.add(
                        MESSAGE_TYPE_ALIASES.get(normalized_type, normalized_type)
                    )

        raw_rules = raw_config.get("rules", [])
        if isinstance(raw_rules, list):
            for raw_rule in raw_rules:
                if not isinstance(raw_rule, dict):
                    continue

                conditions = self._rule_conditions(raw_rule)
                selection = str(raw_rule.get("selection") or "").strip().lower()
                raw_reactions = raw_rule.get("reactions", [])
                if isinstance(raw_reactions, str):
                    raw_reactions = [raw_reactions]
                elif not isinstance(raw_reactions, list):
                    continue

                reactions: list[str] = []
                for item in raw_reactions:
                    reaction = str(item or "").strip()
                    if reaction and reaction not in reactions:
                        reactions.append(reaction)
                if (
                    not conditions
                    or selection not in {"fixed", "random"}
                    or not reactions
                ):
                    continue

                matched = True
                for condition in conditions:
                    match_type = condition["match_type"]
                    pattern = condition["pattern"]
                    if match_type == "keyword":
                        condition_matches = pattern in message_text
                    elif match_type == "user_id":
                        condition_matches = pattern == sender_id
                    elif match_type == "bot_id":
                        condition_matches = pattern == bot_id
                    elif match_type == "group_id":
                        condition_matches = pattern == group_id
                    elif match_type == "message_type":
                        normalized_pattern = pattern.lower().replace("-", "_")
                        normalized_pattern = MESSAGE_TYPE_ALIASES.get(
                            normalized_pattern,
                            normalized_pattern,
                        )
                        condition_matches = normalized_pattern in message_types
                    else:
                        try:
                            condition_matches = (
                                re.search(pattern, message_text) is not None
                            )
                        except re.error as exc:
                            logger.debug(
                                "Skipping invalid Matrix reaction regex %r: %s",
                                pattern,
                                exc,
                            )
                            condition_matches = False
                    if not condition_matches:
                        matched = False
                        break

                if matched:
                    return (
                        random.choice(reactions)
                        if selection == "random"
                        else reactions[0]
                    )

        get_config = getattr(self.context, "get_config", None)
        if callable(get_config):
            try:
                astrbot_config = get_config(getattr(event, "unified_msg_origin", None))
            except Exception as exc:
                logger.debug("Failed to read AstrBot wake configuration: %s", exc)
                astrbot_config = {"wake_prefix": ["/"]}
        else:
            astrbot_config = {"wake_prefix": ["/"]}
        if not isinstance(astrbot_config, dict):
            astrbot_config = {"wake_prefix": ["/"]}
        if not MatrixRuleReactTriggerFilter(False).filter(event, astrbot_config):
            return ""

        raw_emojis = raw_config.get("emojis", [])
        if isinstance(raw_emojis, str):
            raw_emojis = [raw_emojis]
        elif not isinstance(raw_emojis, (list, tuple)):
            return ""

        emojis: list[str] = []
        for item in raw_emojis:
            emoji = str(item or "").strip()
            if emoji and emoji not in emojis:
                emojis.append(emoji)
        return random.choice(emojis) if emojis else ""

    @filter.platform_adapter_type("matrix")
    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE | filter.EventMessageType.PRIVATE_MESSAGE
    )
    async def on_message(self, event: AstrMessageEvent) -> None:
        """Request one reaction for the first matching Matrix rule.

        Args:
            event: Incoming Matrix message event.

        Returns:
            None.
        """
        raw_config = self.config.get("matrix_rule_react", {})
        if not isinstance(raw_config, dict):
            return

        raw_enabled = raw_config.get("enable", False)
        if isinstance(raw_enabled, bool):
            enabled = raw_enabled
        elif isinstance(raw_enabled, str):
            enabled = raw_enabled.strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
                "enable",
                "enabled",
            }
        else:
            enabled = False
        if not enabled:
            return

        if str(event.get_sender_id() or "") == str(event.get_self_id() or ""):
            return

        message_obj = getattr(event, "message_obj", None)
        event_id = getattr(message_obj, "message_id", None)
        if not event_id:
            raw_message = getattr(message_obj, "raw_message", None)
            if isinstance(raw_message, dict):
                event_id = raw_message.get("event_id")
            else:
                event_id = getattr(raw_message, "event_id", None)
        if not str(event_id or "").strip():
            logger.debug(
                "Skipping Matrix rule reaction because the event ID is missing."
            )
            return

        emoji = self._select_reaction(event, raw_config)
        if not emoji:
            return

        try:
            await event.react(emoji)
            logger.debug("Requested Matrix rule reaction: emoji=%r", emoji)
        except Exception as exc:
            logger.warning("Failed to request Matrix rule reaction: %s", exc)

    @filter.command_group("matrix")
    def matrix(self) -> None:
        """Matrix plugin commands."""
        pass

    @matrix.group("rules")
    def matrix_rules(self) -> None:
        """Matrix rule-management commands."""
        pass

    @matrix_rules.group("react")
    def matrix_rules_react(self) -> None:
        """Matrix reaction-rule commands."""
        pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @matrix_rules_react.command("add")
    async def add_rule(
        self,
        event: AstrMessageEvent,
        selection: str,
        reactions: str,
        condition_array: GreedyStr,
    ):
        """Add and persist a Matrix reaction rule.

        Args:
            event: Administrator command event.
            selection: ``fixed`` or ``random`` reaction selection.
            reactions: Comma-separated reaction keys.
            condition_array: Variable-length sequence of ``(type value)`` conditions.

        Yields:
            Command result describing the added rule or validation error.
        """
        normalized_selection = str(selection or "").strip().lower()
        if normalized_selection not in {"fixed", "random"}:
            yield event.plain_result("选取模式无效，请使用 fixed 或 random。")
            return

        reaction_keys: list[str] = []
        for item in str(reactions or "").split(","):
            reaction = item.strip()
            if reaction and reaction not in reaction_keys:
                reaction_keys.append(reaction)
        if not reaction_keys:
            yield event.plain_result("Reaction 列表不能为空。")
            return
        if normalized_selection == "fixed" and len(reaction_keys) != 1:
            yield event.plain_result("fixed 模式必须且只能提供一个 Reaction。")
            return

        try:
            conditions = _parse_conditions(condition_array)
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return

        raw_config = self.config.get("matrix_rule_react")
        if not isinstance(raw_config, dict):
            raw_config = {}
            self.config["matrix_rule_react"] = raw_config
        raw_rules = raw_config.get("rules", [])
        if not isinstance(raw_rules, list):
            raw_rules = []
        raw_rules.append(
            {
                "__template_key": "reaction_rule",
                "selection": normalized_selection,
                "reactions": reaction_keys,
                "conditions": conditions,
            }
        )
        raw_config["rules"] = raw_rules

        save_config = getattr(self.config, "save_config", None)
        if callable(save_config):
            try:
                save_config()
            except Exception as exc:
                logger.warning("Failed to persist Matrix reaction rule: %s", exc)
                yield event.plain_result(f"规则已加入内存，但持久化失败：{exc}")
                return

        state = "已启用" if bool(raw_config.get("enable", False)) else "当前未启用"
        yield event.plain_result(
            f"已添加规则 #{len(raw_rules)}（插件{state}）："
            f"[{normalized_selection}] {_format_conditions(conditions)} "
            f"-> {', '.join(reaction_keys)}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @matrix_rules_react.command("list")
    async def list_rules(self, event: AstrMessageEvent):
        """List persisted Matrix reaction rules.

        Args:
            event: Administrator command event.

        Yields:
            Formatted rule list and current enabled state.
        """
        raw_config = self.config.get("matrix_rule_react", {})
        if not isinstance(raw_config, dict):
            raw_config = {}
        raw_rules = raw_config.get("rules", [])
        if not isinstance(raw_rules, list):
            raw_rules = []

        lines = [
            "Matrix Reaction 规则："
            + ("已启用" if bool(raw_config.get("enable", False)) else "未启用")
        ]
        if not raw_rules:
            lines.append("暂无动态规则。")
        else:
            for index, raw_rule in enumerate(raw_rules, start=1):
                if not isinstance(raw_rule, dict):
                    lines.append(f"{index}. [无效规则]")
                    continue
                selection = str(raw_rule.get("selection") or "")
                conditions = self._rule_conditions(raw_rule)
                raw_reactions = raw_rule.get("reactions", [])
                if isinstance(raw_reactions, str):
                    raw_reactions = [raw_reactions]
                if not isinstance(raw_reactions, list):
                    raw_reactions = []
                reaction_text = ", ".join(
                    str(item).strip() for item in raw_reactions if str(item).strip()
                )
                if not conditions:
                    lines.append(f"{index}. [无效规则] -> {reaction_text or '-'}")
                    continue
                lines.append(
                    f"{index}. [{selection}] {_format_conditions(conditions)} "
                    f"-> {reaction_text or '-'}"
                )
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @matrix_rules_react.command("remove")
    async def remove_rule(self, event: AstrMessageEvent, index: int):
        """Remove and persist a Matrix reaction rule by its displayed index.

        Args:
            event: Administrator command event.
            index: One-based index shown by the ``list`` command.

        Yields:
            Command result describing the removed rule or index error.
        """
        raw_config = self.config.get("matrix_rule_react", {})
        if not isinstance(raw_config, dict):
            raw_config = {}
        raw_rules = raw_config.get("rules", [])
        if not isinstance(raw_rules, list) or index < 1 or index > len(raw_rules):
            yield event.plain_result(
                "规则编号无效，请使用 /matrix rules react list 查看。"
            )
            return

        removed_rule = raw_rules.pop(index - 1)
        raw_config["rules"] = raw_rules
        save_config = getattr(self.config, "save_config", None)
        if callable(save_config):
            try:
                save_config()
            except Exception as exc:
                logger.warning(
                    "Failed to persist Matrix reaction rule removal: %s", exc
                )
                yield event.plain_result(f"规则已从内存移除，但持久化失败：{exc}")
                return

        conditions = (
            self._rule_conditions(removed_rule)
            if isinstance(removed_rule, dict)
            else None
        )
        summary = _format_conditions(conditions) if conditions else "[无效规则]"
        yield event.plain_result(f"已移除规则 #{index}：{summary}")
