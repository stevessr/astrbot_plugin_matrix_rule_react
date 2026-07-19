"""Matrix message handling for the rule-reaction plugin."""

import random

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .rules import select_dynamic_reaction
from .trigger_filter import MatrixRuleReactTriggerFilter


class MatrixRuleReactMessageMixin:
    """Provide reaction selection and Matrix message processing."""

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

    async def handle_message(self, event: AstrMessageEvent) -> None:
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
