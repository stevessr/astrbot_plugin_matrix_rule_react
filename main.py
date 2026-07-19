"""Send Matrix reactions for messages that match configured rules."""

import random

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr

from .rules import (
    format_conditions,
    normalize_rule_conditions,
    parse_conditions,
    select_dynamic_reaction,
)
from .trigger_filter import MatrixRuleReactTriggerFilter


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
        dynamic_reaction = select_dynamic_reaction(event, raw_config.get("rules", []))
        if dynamic_reaction:
            return dynamic_reaction

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
            conditions = parse_conditions(condition_array)
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
            f"[{normalized_selection}] {format_conditions(conditions)} "
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
                conditions = normalize_rule_conditions(raw_rule)
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
                    f"{index}. [{selection}] {format_conditions(conditions)} "
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
            normalize_rule_conditions(removed_rule)
            if isinstance(removed_rule, dict)
            else None
        )
        summary = format_conditions(conditions) if conditions else "[无效规则]"
        yield event.plain_result(f"已移除规则 #{index}：{summary}")
