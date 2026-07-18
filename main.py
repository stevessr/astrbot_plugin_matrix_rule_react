"""Send Matrix reactions for messages that match configured rules."""

import random
import re

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr


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

        raw_rules = raw_config.get("rules", [])
        if isinstance(raw_rules, list):
            for raw_rule in raw_rules:
                if not isinstance(raw_rule, dict):
                    continue

                match_type = str(raw_rule.get("match_type") or "").strip().lower()
                pattern = str(raw_rule.get("pattern") or "").strip()
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
                    match_type not in {"keyword", "regex", "user_id"}
                    or selection not in {"fixed", "random"}
                    or not pattern
                    or not reactions
                ):
                    continue

                matched = False
                if match_type == "keyword":
                    matched = pattern in message_text
                elif match_type == "user_id":
                    matched = pattern == sender_id
                else:
                    try:
                        matched = re.search(pattern, message_text) is not None
                    except re.error as exc:
                        logger.debug(
                            "Skipping invalid Matrix reaction regex %r: %s",
                            pattern,
                            exc,
                        )

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
        match_type: str,
        selection: str,
        reactions: str,
        pattern: GreedyStr,
    ):
        """Add and persist a Matrix reaction rule.

        Args:
            event: Administrator command event.
            match_type: ``keyword``, ``regex``, or ``user_id``.
            selection: ``fixed`` or ``random`` reaction selection.
            reactions: Comma-separated reaction keys.
            pattern: Keyword, regular expression, or exact Matrix user ID.

        Yields:
            Command result describing the added rule or validation error.
        """
        normalized_type = str(match_type or "").strip().lower()
        normalized_type = {
            "user": "user_id",
            "userid": "user_id",
        }.get(normalized_type, normalized_type)
        normalized_selection = str(selection or "").strip().lower()
        normalized_pattern = str(pattern or "").strip()
        if normalized_type not in {"keyword", "regex", "user_id"}:
            yield event.plain_result("规则类型无效，请使用 keyword、regex 或 user_id。")
            return
        if normalized_selection not in {"fixed", "random"}:
            yield event.plain_result("选取模式无效，请使用 fixed 或 random。")
            return
        if not normalized_pattern:
            yield event.plain_result("匹配内容不能为空。")
            return
        if normalized_type == "regex":
            try:
                re.compile(normalized_pattern)
            except re.error as exc:
                yield event.plain_result(f"正则表达式无效：{exc}")
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
                "match_type": normalized_type,
                "pattern": normalized_pattern,
                "selection": normalized_selection,
                "reactions": reaction_keys,
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
            f"{normalized_type}/{normalized_selection} "
            f"{normalized_pattern!r} -> {', '.join(reaction_keys)}"
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
                match_type = str(raw_rule.get("match_type") or "")
                selection = str(raw_rule.get("selection") or "")
                pattern = str(raw_rule.get("pattern") or "")
                raw_reactions = raw_rule.get("reactions", [])
                if isinstance(raw_reactions, str):
                    raw_reactions = [raw_reactions]
                if not isinstance(raw_reactions, list):
                    raw_reactions = []
                reaction_text = ", ".join(
                    str(item).strip() for item in raw_reactions if str(item).strip()
                )
                lines.append(
                    f"{index}. [{match_type}/{selection}] "
                    f"{pattern!r} -> {reaction_text or '-'}"
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

        pattern = (
            str(removed_rule.get("pattern") or "")
            if isinstance(removed_rule, dict)
            else ""
        )
        yield event.plain_result(f"已移除规则 #{index}：{pattern or '[无效规则]'}")
