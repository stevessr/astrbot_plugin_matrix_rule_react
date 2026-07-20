"""Parse, normalize, and evaluate Matrix reaction rules."""

import random
import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

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
NEGATED_MATCH_TYPES = frozenset(f"not_{match_type}" for match_type in MATCH_TYPES)
ALL_MATCH_TYPES = MATCH_TYPES | NEGATED_MATCH_TYPES
MATCH_TYPE_ALIASES = {
    "user": "user_id",
    "userid": "user_id",
    "bot": "bot_id",
    "botid": "bot_id",
    "group": "group_id",
    "groupid": "group_id",
    "message": "message_type",
    "msg_type": "message_type",
    "not_user": "not_user_id",
    "not_userid": "not_user_id",
    "not_bot": "not_bot_id",
    "not_botid": "not_bot_id",
    "not_group": "not_group_id",
    "not_groupid": "not_group_id",
    "not_message": "not_message_type",
    "not_msg_type": "not_message_type",
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
RULE_TEMPLATE_DEFINITIONS = {
    "single_rule": ("all", ("condition",)),
    "a_and_b": ("all", ("condition_a", "condition_b")),
    "a_or_b": ("any", ("condition_a", "condition_b")),
    "all_rule": ("all", "conditions"),
    "any_rule": ("any", "conditions"),
}
MATCH_MODE_ALIASES = {
    "and": "all",
    "or": "any",
    "all": "all",
    "any": "any",
}
_CONDITION_MARKER_PATTERN = re.compile(
    rf"(?<!\S)(?:{'|'.join(sorted(ALL_MATCH_TYPES, key=len, reverse=True))})(?=\s|$)",
    re.IGNORECASE,
)


def parse_conditions(value: object) -> list[dict[str, str]]:
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
                "匹配条件必须以 keyword、regex、user_id、bot_id、group_id、"
                "message_type 或其 not_ 反义变种开始。"
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
        if match_type not in ALL_MATCH_TYPES:
            raise ValueError(f"规则类型无效：{parts[0]}。")
        pattern = parts[1].strip() if len(parts) > 1 else ""
        if len(pattern) >= 2 and pattern[0] == pattern[-1] and pattern[0] in {'"', "'"}:
            pattern = pattern[1:-1].strip()
        if not pattern:
            raise ValueError(f"{match_type} 的匹配内容不能为空。")
        if match_type in {"regex", "not_regex"}:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"正则表达式无效：{exc}") from exc
        conditions.append({"match_type": match_type, "pattern": pattern})

    if not conditions:
        raise ValueError("至少需要提供一个匹配条件。")
    return conditions


def normalize_rule_conditions(raw_rule: dict) -> list[dict[str, str]] | None:
    """Normalize simple templates, condition arrays, and legacy rules.

    Args:
        raw_rule: Persisted reaction rule from plugin configuration.

    Returns:
        Normalized conditions, or ``None`` when the rule is invalid.
    """
    template_key = str(raw_rule.get("__template_key") or "").strip()
    template_definition = RULE_TEMPLATE_DEFINITIONS.get(template_key)
    if template_definition:
        condition_keys = template_definition[1]
        if isinstance(condition_keys, str):
            raw_conditions = raw_rule.get(condition_keys)
            if not isinstance(raw_conditions, list) or not raw_conditions:
                return None
        else:
            raw_conditions = []
            for condition_key in condition_keys:
                raw_condition = raw_rule.get(condition_key)
                if not isinstance(raw_condition, dict):
                    return None
                raw_conditions.append(raw_condition)
    else:
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
                conditions.extend(parse_conditions(raw_condition))
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
        if match_type not in ALL_MATCH_TYPES or not pattern:
            return None
        conditions.append({"match_type": match_type, "pattern": pattern})

    return conditions or None


def normalize_rule_match_mode(raw_rule: dict) -> str:
    """Return the ``all`` or ``any`` relation used by a persisted rule.

    Simple template semantics are fixed by their template key. Custom and legacy
    rules may provide ``all``/``and`` or ``any``/``or`` and safely default to
    ``all``.

    Args:
        raw_rule: Persisted reaction rule from plugin configuration.

    Returns:
        ``all`` for AND semantics or ``any`` for OR semantics.
    """
    template_key = str(raw_rule.get("__template_key") or "").strip()
    template_definition = RULE_TEMPLATE_DEFINITIONS.get(template_key)
    if template_definition:
        return template_definition[0]

    match_mode = str(raw_rule.get("match_mode") or "all").strip().lower()
    match_mode = MATCH_MODE_ALIASES.get(match_mode, match_mode)
    return match_mode if match_mode in {"all", "any"} else "all"


def format_conditions(conditions: list[dict[str, str]], match_mode: str = "all") -> str:
    """Format normalized conditions for command responses.

    Args:
        conditions: Normalized condition dictionaries.
        match_mode: ``all`` for AND or ``any`` for OR.

    Returns:
        A compact expression suitable for add, list, and remove results.
    """
    separator = " OR " if match_mode == "any" else " AND "
    return separator.join(
        f"{condition['match_type']}={condition['pattern']!r}"
        for condition in conditions
    )


def _check_rule_probability(raw_rule: dict) -> bool:
    """Check whether a rule's probability gate allows it to fire.

    Args:
        raw_rule: Persisted reaction rule dictionary.

    Returns:
        ``True`` when the rule should fire (random roll passed or no probability
        set), ``False`` when the probability gate blocked it.
    """
    probability = raw_rule.get("probability")
    if probability is None:
        return True
    try:
        prob = float(probability)
    except (ValueError, TypeError):
        return True
    if prob >= 1.0:
        return True
    if prob <= 0.0:
        return False
    return random.random() < prob


def format_probability(probability: object) -> str:
    """Format a rule's probability value for display.

    Args:
        probability: Raw probability value from the rule dict.

    Returns:
        Human-readable probability string, or an empty string for 1.0 (always).
    """
    try:
        prob = float(probability) if probability is not None else 1.0
    except (ValueError, TypeError):
        return ""
    if prob >= 1.0:
        return ""
    if prob <= 0.0:
        return "0%"
    return f"{round(prob * 100)}%"


def select_dynamic_reaction(event: AstrMessageEvent, raw_rules: object) -> str:
    """Select a reaction from the first matching dynamic rule.

    Args:
        event: Incoming Matrix message event.
        raw_rules: Persisted rule list from plugin configuration.

    Returns:
        The selected reaction key, or an empty string when no rule matches.
    """
    if not isinstance(raw_rules, list):
        return ""

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

    for raw_rule in raw_rules:
        if not isinstance(raw_rule, dict):
            continue

        conditions = normalize_rule_conditions(raw_rule)
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
        if not conditions or selection not in {"fixed", "random"} or not reactions:
            continue

        match_mode = normalize_rule_match_mode(raw_rule)
        matched = match_mode == "all"
        for condition in conditions:
            match_type = condition["match_type"]
            negated = match_type.startswith("not_")
            base_type = match_type[4:] if negated else match_type
            pattern = condition["pattern"]
            if base_type == "keyword":
                base_matches = pattern in message_text
            elif base_type == "user_id":
                base_matches = pattern == sender_id
            elif base_type == "bot_id":
                base_matches = pattern == bot_id
            elif base_type == "group_id":
                base_matches = pattern == group_id
            elif base_type == "message_type":
                normalized_pattern = pattern.lower().replace("-", "_")
                normalized_pattern = MESSAGE_TYPE_ALIASES.get(
                    normalized_pattern,
                    normalized_pattern,
                )
                base_matches = normalized_pattern in message_types
            else:
                try:
                    base_matches = re.search(pattern, message_text) is not None
                except re.error as exc:
                    logger.debug(
                        "Skipping invalid Matrix reaction regex %r: %s",
                        pattern,
                        exc,
                    )
                    base_matches = False
            condition_matches = not base_matches if negated else base_matches
            if match_mode == "all" and not condition_matches:
                matched = False
                break
            if match_mode == "any" and condition_matches:
                matched = True
                break

        if matched:
            if not _check_rule_probability(raw_rule):
                logger.debug(
                    "Matrix rule skipped by probability gate: %r",
                    raw_rule.get("probability"),
                )
                continue
            return random.choice(reactions) if selection == "random" else reactions[0]

    return ""
