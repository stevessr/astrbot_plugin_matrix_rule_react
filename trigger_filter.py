"""AstrBot wake-condition filter used by the Matrix reaction plugin."""

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At


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
