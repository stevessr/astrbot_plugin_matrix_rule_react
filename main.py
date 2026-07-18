"""Send Matrix reactions for messages that match AstrBot wake rules."""

import random

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At
from astrbot.api.star import Context, Star


class MatrixRuleReactTriggerFilter(filter.CustomFilter):
    """Match an explicit bot mention or a valid AstrBot wake-prefix message."""

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        """Check the reaction rule before AstrBot activates the plugin handler.

        Requiring AstrBot's own wake decision keeps this passive reaction plugin from
        waking ordinary Matrix traffic merely because it has a message handler.

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
    """React to Matrix messages that explicitly wake AstrBot."""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        """Initialize the plugin with its persisted configuration.

        Args:
            context: AstrBot plugin context.
            config: Plugin configuration loaded from ``_conf_schema.json``.
        """
        super().__init__(context, config)
        self.config = config if isinstance(config, dict) else {}

    @filter.platform_adapter_type("matrix")
    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE | filter.EventMessageType.PRIVATE_MESSAGE
    )
    @filter.custom_filter(MatrixRuleReactTriggerFilter, False)
    async def on_message(self, event: AstrMessageEvent) -> None:
        """Request one configured reaction for a matching Matrix event.

        Args:
            event: Matrix message event that passed the trigger filter.

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

        raw_emojis = raw_config.get("emojis", [])
        if isinstance(raw_emojis, str):
            raw_emojis = [raw_emojis]
        elif not isinstance(raw_emojis, (list, tuple)):
            return

        emojis: list[str] = []
        for item in raw_emojis:
            emoji = str(item or "").strip()
            if emoji and emoji not in emojis:
                emojis.append(emoji)
        if not emojis:
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

        emoji = random.choice(emojis)
        try:
            await event.react(emoji)
            logger.debug("Requested Matrix rule reaction: emoji=%r", emoji)
        except Exception as exc:
            logger.warning("Failed to request Matrix rule reaction: %s", exc)
