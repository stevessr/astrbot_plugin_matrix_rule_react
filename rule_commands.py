"""Administrator command implementations for Matrix reaction rules."""

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .rules import (
    format_conditions,
    format_probability,
    normalize_rule_conditions,
    normalize_rule_match_mode,
    parse_conditions,
)


class MatrixRuleReactCommandMixin:
    """Implement persistent add, list, and remove rule operations."""

    async def cmd_add_rule(
        self,
        event: AstrMessageEvent,
        selection: str,
        reactions: str,
        condition_array: str,
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

        # Detect optional probability as the first standalone number in the string
        probability = None
        condition_text = str(condition_array or "").strip()
        if condition_text:
            first_token = condition_text.split(maxsplit=1)[0]
            try:
                # Check if the first token that isn't a parenthesized condition is a number
                if not first_token.startswith("("):
                    prob_value = float(first_token)
                    if 0.0 <= prob_value <= 1.0:
                        probability = prob_value
                        # Remove the probability token from the start
                        condition_text = condition_text[len(first_token):].strip()
                    else:
                        yield event.plain_result("概率值必须在 0.0 到 1.0 之间。")
                        return
            except (ValueError, TypeError):
                # First token is not a number — no probability
                pass

        try:
            conditions = parse_conditions(condition_text)
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

        rule = {
            "__template_key": "reaction_rule",
            "selection": normalized_selection,
            "reactions": reaction_keys,
            "match_mode": "all",
            "conditions": conditions,
        }
        if probability is not None:
            rule["probability"] = probability
        raw_rules.append(rule)
        raw_config["rules"] = raw_rules

        save_config = getattr(self.config, "save_config", None)
        if callable(save_config):
            try:
                save_config()
            except Exception as exc:
                logger.warning("Failed to persist Matrix reaction rule: %s", exc)
                yield event.plain_result(f"规则已加入内存，但持久化失败：{exc}")
                return

        prob_text = format_probability(probability) if probability is not None else ""
        prob_suffix = f" [{prob_text}]" if prob_text else ""
        state = "已启用" if bool(raw_config.get("enable", False)) else "当前未启用"
        yield event.plain_result(
            f"已添加规则 #{len(raw_rules)}（插件{state}）："
            f"[{normalized_selection}] {format_conditions(conditions)} "
            f"-> {', '.join(reaction_keys)}{prob_suffix}"
        )

    async def cmd_list_rules(self, event: AstrMessageEvent):
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
                match_mode = normalize_rule_match_mode(raw_rule)
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
                prob_text = format_probability(raw_rule.get("probability"))
                prob_suffix = f" [{prob_text}]" if prob_text else ""
                lines.append(
                    f"{index}. [{selection}] "
                    f"{format_conditions(conditions, match_mode)} "
                    f"-> {reaction_text or '-'}{prob_suffix}"
                )
        yield event.plain_result("\n".join(lines))

    async def cmd_remove_rule(self, event: AstrMessageEvent, index: int):
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
        match_mode = (
            normalize_rule_match_mode(removed_rule)
            if isinstance(removed_rule, dict)
            else "all"
        )
        summary = (
            format_conditions(conditions, match_mode) if conditions else "[无效规则]"
        )
        yield event.plain_result(f"已移除规则 #{index}：{summary}")
