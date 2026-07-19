"""AstrBot entrypoint and handler bindings for Matrix rule reactions.

Decorated handlers remain in this module so AstrBot registers them under the plugin
entrypoint; message and command implementations live in dedicated mixins.
"""

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr

from .message_handler import MatrixRuleReactMessageMixin
from .rule_commands import MatrixRuleReactCommandMixin
from .trigger_filter import MatrixRuleReactTriggerFilter as MatrixRuleReactTriggerFilter


class MatrixRuleReactPlugin(
    Star,
    MatrixRuleReactMessageMixin,
    MatrixRuleReactCommandMixin,
):
    """React to Matrix messages that match wake or configured dynamic rules."""

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
    async def on_message(self, event: AstrMessageEvent) -> None:
        """Delegate an incoming Matrix message to the message processor.

        Args:
            event: Incoming Matrix message event.

        Returns:
            None.
        """
        await self.handle_message(event)

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
        """Delegate a validated add command to the rule command implementation.

        Args:
            event: Administrator command event.
            selection: ``fixed`` or ``random`` reaction selection.
            reactions: Comma-separated reaction keys.
            condition_array: Variable-length sequence of ``(type value)`` conditions.

        Yields:
            Command result describing the added rule or validation error.
        """
        async for result in self.cmd_add_rule(
            event,
            selection,
            reactions,
            condition_array,
        ):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @matrix_rules_react.command("list")
    async def list_rules(self, event: AstrMessageEvent):
        """Delegate rule listing to the command implementation.

        Args:
            event: Administrator command event.

        Yields:
            Formatted rule list and current enabled state.
        """
        async for result in self.cmd_list_rules(event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @matrix_rules_react.command("remove")
    async def remove_rule(self, event: AstrMessageEvent, index: int):
        """Delegate indexed rule removal to the command implementation.

        Args:
            event: Administrator command event.
            index: One-based index shown by the ``list`` command.

        Yields:
            Command result describing the removed rule or index error.
        """
        async for result in self.cmd_remove_rule(event, index):
            yield result
