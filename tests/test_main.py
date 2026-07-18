"""Tests for the Matrix rule-reaction plugin."""

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from astrbot.api.event import filter
from astrbot.api.message_components import At
from data.plugins.astrbot_plugin_matrix_rule_react.main import (
    MatrixRuleReactPlugin,
    MatrixRuleReactTriggerFilter,
)


class FakeEvent:
    """Minimal Matrix event used by the plugin and trigger-filter tests."""

    def __init__(
        self,
        *,
        message_text: str = "hello",
        current_text: str | None = None,
        messages: list | None = None,
        sender_id: str = "@alice:example.org",
        self_id: str = "@bot:example.org",
        event_id: str = "$event:example.org",
        is_native_wake: bool = True,
        reaction_error: Exception | None = None,
    ) -> None:
        """Build a fake event with separate original and pipeline message text.

        Args:
            message_text: Original text retained on the Matrix message object.
            current_text: Current event text after AstrBot pipeline processing.
            messages: Message components exposed by the event.
            sender_id: Matrix sender user ID.
            self_id: Matrix bot user ID.
            event_id: Matrix event ID, or an empty string to simulate a bad event.
            is_native_wake: Whether AstrBot already recognized a native wake condition.
            reaction_error: Optional exception raised by ``react``.
        """
        self.message_obj = SimpleNamespace(
            message_id=event_id,
            message_str=message_text,
            raw_message=SimpleNamespace(event_id=event_id),
        )
        self.message_str = message_text if current_text is None else current_text
        self.messages = messages or []
        self.sender_id = sender_id
        self.self_id = self_id
        self.is_at_or_wake_command = is_native_wake
        self.reaction_error = reaction_error
        self.reactions: list[str] = []

    def get_messages(self) -> list:
        """Return the fake message chain.

        Returns:
            Message components supplied at construction time.
        """
        return self.messages

    def get_message_str(self) -> str:
        """Return the pipeline's current message text.

        Returns:
            Current message text, which may have its wake prefix removed.
        """
        return self.message_str

    def get_sender_id(self) -> str:
        """Return the fake sender ID.

        Returns:
            Matrix sender user ID.
        """
        return self.sender_id

    def get_self_id(self) -> str:
        """Return the fake bot ID.

        Returns:
            Matrix bot user ID.
        """
        return self.self_id

    async def react(self, emoji: str) -> None:
        """Record a requested reaction or raise the configured error.

        Args:
            emoji: Reaction key selected by the plugin.

        Raises:
            Exception: The configured reaction failure, when present.
        """
        if self.reaction_error is not None:
            raise self.reaction_error
        self.reactions.append(emoji)

    def plain_result(self, text: str) -> str:
        """Return command text without constructing an AstrBot result object.

        Args:
            text: Command response text.

        Returns:
            Unchanged response text.
        """
        return text


class PersistedConfig(dict):
    """Dictionary config that records explicit persistence requests."""

    def __init__(self, value: dict) -> None:
        """Initialize the fake persisted configuration.

        Args:
            value: Initial plugin configuration.
        """
        super().__init__(value)
        self.save_count = 0

    def save_config(self) -> None:
        """Record one configuration persistence request."""
        self.save_count += 1


class TriggerFilterTests(unittest.TestCase):
    """Verify that the handler cannot wake unrelated Matrix messages."""

    def setUp(self) -> None:
        """Create a fresh trigger filter for each test."""
        self.trigger_filter = MatrixRuleReactTriggerFilter(False)
        self.config = {"wake_prefix": ["/", "!"]}

    def test_matches_targeted_bot_mention(self) -> None:
        """A direct mention of the Matrix bot should match."""
        event = FakeEvent(messages=[At(qq="@bot:example.org")])

        self.assertTrue(self.trigger_filter.filter(event, self.config))

    def test_matches_original_wake_prefix_after_pipeline_strips_it(self) -> None:
        """The filter should read the original text retained by the adapter."""
        event = FakeEvent(message_text=" /status", current_text="status")

        self.assertTrue(self.trigger_filter.filter(event, self.config))

    def test_rejects_private_message_without_explicit_rule(self) -> None:
        """An implicitly woken private message should not trigger a reaction."""
        event = FakeEvent(message_text="hello", is_native_wake=True)

        self.assertFalse(self.trigger_filter.filter(event, self.config))

    def test_rejects_rule_when_astrbot_did_not_accept_the_wake(self) -> None:
        """The plugin must not broaden AstrBot's own wake decision."""
        event = FakeEvent(
            message_text="/status",
            messages=[At(qq="@other:example.org")],
            is_native_wake=False,
        )

        self.assertFalse(self.trigger_filter.filter(event, self.config))

    def test_rejects_other_user_mention_and_empty_prefix(self) -> None:
        """Another user's mention and an empty prefix should not match."""
        event = FakeEvent(
            message_text="hello",
            messages=[At(qq="@other:example.org")],
        )

        self.assertFalse(self.trigger_filter.filter(event, {"wake_prefix": [""]}))


class MatrixRuleReactPluginTests(unittest.IsolatedAsyncioTestCase):
    """Verify configuration guards and reaction dispatch."""

    @staticmethod
    def make_plugin(config: dict) -> MatrixRuleReactPlugin:
        """Create a plugin instance with a minimal context.

        Args:
            config: Plugin configuration to inject.

        Returns:
            Configured plugin instance.
        """
        return MatrixRuleReactPlugin(SimpleNamespace(), config)

    async def test_disabled_plugin_does_not_react(self) -> None:
        """The default disabled state should be side-effect free."""
        plugin = self.make_plugin(
            {"matrix_rule_react": {"enable": False, "emojis": ["👍"]}}
        )
        event = FakeEvent()

        await plugin.on_message(event)

        self.assertEqual(event.reactions, [])

    async def test_enabled_plugin_ignores_an_unmatched_ordinary_message(self) -> None:
        """The all-message handler should stay passive when no rule matches."""
        plugin = self.make_plugin(
            {"matrix_rule_react": {"enable": True, "emojis": ["👍"], "rules": []}}
        )
        event = FakeEvent(message_text="hello", is_native_wake=False)

        await plugin.on_message(event)

        self.assertEqual(event.reactions, [])

    async def test_reacts_once_with_normalized_unique_keys(self) -> None:
        """Enabled configuration should trim, de-duplicate, and select one key."""
        plugin = self.make_plugin(
            {
                "matrix_rule_react": {
                    "enable": "enabled",
                    "emojis": [" 👍 ", "👍", "", "🤔"],
                }
            }
        )
        event = FakeEvent(message_text="/hello")

        with mock.patch(
            "data.plugins.astrbot_plugin_matrix_rule_react.main.random.choice",
            return_value="🤔",
        ) as choice:
            await plugin.on_message(event)

        choice.assert_called_once_with(["👍", "🤔"])
        self.assertEqual(event.reactions, ["🤔"])

    async def test_ignores_self_messages_missing_ids_and_empty_keys(self) -> None:
        """Invalid or loop-prone events should not request a reaction."""
        plugin = self.make_plugin(
            {"matrix_rule_react": {"enable": True, "emojis": ["👍"]}}
        )
        self_event = FakeEvent(sender_id="@bot:example.org")
        missing_id_event = FakeEvent(event_id="")
        empty_plugin = self.make_plugin(
            {"matrix_rule_react": {"enable": True, "emojis": ["", " "]}}
        )

        await plugin.on_message(self_event)
        await plugin.on_message(missing_id_event)
        await empty_plugin.on_message(FakeEvent())

        self.assertEqual(self_event.reactions, [])
        self.assertEqual(missing_id_event.reactions, [])

    async def test_reaction_failure_does_not_break_message_processing(self) -> None:
        """A platform reaction failure should be contained by the plugin."""
        plugin = self.make_plugin(
            {"matrix_rule_react": {"enable": True, "emojis": ["👍"]}}
        )
        event = FakeEvent(
            message_text="/hello",
            reaction_error=RuntimeError("send failed"),
        )

        await plugin.on_message(event)

        self.assertEqual(event.reactions, [])

    async def test_keyword_rule_reacts_without_an_astrbot_wake(self) -> None:
        """A keyword rule should handle an otherwise ordinary Matrix message."""
        plugin = self.make_plugin(
            {
                "matrix_rule_react": {
                    "enable": True,
                    "emojis": ["🤗"],
                    "rules": [
                        {
                            "match_type": "keyword",
                            "pattern": "build passed",
                            "selection": "fixed",
                            "reactions": ["👍"],
                        },
                        {
                            "match_type": "keyword",
                            "pattern": "build",
                            "selection": "fixed",
                            "reactions": ["🎉"],
                        },
                    ],
                }
            }
        )
        event = FakeEvent(message_text="the build passed now", is_native_wake=False)

        await plugin.on_message(event)

        self.assertEqual(event.reactions, ["👍"])

    async def test_regex_rule_randomly_selects_a_reaction(self) -> None:
        """A regex rule should randomly select from its normalized reaction list."""
        plugin = self.make_plugin(
            {
                "matrix_rule_react": {
                    "enable": True,
                    "rules": [
                        {
                            "match_type": "regex",
                            "pattern": r"^deploy\s+success$",
                            "selection": "random",
                            "reactions": [" 🎉 ", "🎉", "🚀"],
                        }
                    ],
                }
            }
        )
        event = FakeEvent(message_text="deploy success", is_native_wake=False)

        with mock.patch(
            "data.plugins.astrbot_plugin_matrix_rule_react.main.random.choice",
            return_value="🚀",
        ) as choice:
            await plugin.on_message(event)

        choice.assert_called_once_with(["🎉", "🚀"])
        self.assertEqual(event.reactions, ["🚀"])

    async def test_user_id_rule_matches_the_exact_sender(self) -> None:
        """A user-ID rule should match only the configured Matrix sender."""
        plugin = self.make_plugin(
            {
                "matrix_rule_react": {
                    "enable": True,
                    "rules": [
                        {
                            "match_type": "user_id",
                            "pattern": "@alice:example.org",
                            "selection": "fixed",
                            "reactions": ["👋"],
                        }
                    ],
                }
            }
        )
        matching_event = FakeEvent(is_native_wake=False)
        other_event = FakeEvent(
            sender_id="@bob:example.org",
            is_native_wake=False,
        )

        await plugin.on_message(matching_event)
        await plugin.on_message(other_event)

        self.assertEqual(matching_event.reactions, ["👋"])
        self.assertEqual(other_event.reactions, [])

    async def test_add_list_and_remove_commands_persist_rules(self) -> None:
        """Administrator commands should mutate and persist the rule list."""
        config = PersistedConfig(
            {
                "matrix_rule_react": {
                    "enable": True,
                    "emojis": ["🤗"],
                    "rules": [],
                }
            }
        )
        plugin = MatrixRuleReactPlugin(SimpleNamespace(), config)
        event = FakeEvent()

        add_results = [
            result
            async for result in plugin.add_rule(
                event,
                "user",
                "random",
                "👋,🎉",
                "@alice:example.org",
            )
        ]
        added_template_key = config["matrix_rule_react"]["rules"][0]["__template_key"]
        list_results = [result async for result in plugin.list_rules(event)]
        remove_results = [result async for result in plugin.remove_rule(event, 1)]

        self.assertIn("已添加规则 #1", add_results[0])
        self.assertEqual(added_template_key, "reaction_rule")
        self.assertIn("[user_id/random]", list_results[0])
        self.assertIn("已移除规则 #1", remove_results[0])
        self.assertEqual(config["matrix_rule_react"]["rules"], [])
        self.assertEqual(config.save_count, 2)

    async def test_add_command_rejects_invalid_regex_and_fixed_list(self) -> None:
        """The add command should reject unsafe or ambiguous rule definitions."""
        config = PersistedConfig({"matrix_rule_react": {"enable": True, "rules": []}})
        plugin = MatrixRuleReactPlugin(SimpleNamespace(), config)
        event = FakeEvent()

        regex_results = [
            result
            async for result in plugin.add_rule(
                event,
                "regex",
                "fixed",
                "👍",
                "[",
            )
        ]
        fixed_results = [
            result
            async for result in plugin.add_rule(
                event,
                "keyword",
                "fixed",
                "👍,🎉",
                "done",
            )
        ]

        self.assertIn("正则表达式无效", regex_results[0])
        self.assertIn("fixed 模式", fixed_results[0])
        self.assertEqual(config["matrix_rule_react"]["rules"], [])
        self.assertEqual(config.save_count, 0)


class PluginFileTests(unittest.TestCase):
    """Check the plugin's user-facing configuration contract."""

    def test_handler_registration_accepts_ordinary_matrix_messages(self) -> None:
        """The message handler must see ordinary messages for dynamic matching."""
        from astrbot.core.star.star_handler import star_handlers_registry

        handler_name = f"{MatrixRuleReactPlugin.__module__}_on_message"
        handler = star_handlers_registry.star_handlers_map[handler_name]

        self.assertEqual(
            {type(item).__name__ for item in handler.event_filters},
            {
                "EventMessageTypeFilter",
                "PlatformAdapterTypeFilter",
            },
        )

    def test_rule_commands_use_the_nested_group_and_admin_permission(self) -> None:
        """Every rule-management command should be nested and administrator-only."""
        from astrbot.core.star.star_handler import star_handlers_registry

        expected_commands = {
            "add_rule": "matrix rules react add",
            "list_rules": "matrix rules react list",
            "remove_rule": "matrix rules react remove",
        }
        for handler_suffix, command_name in expected_commands.items():
            handler_name = f"{MatrixRuleReactPlugin.__module__}_{handler_suffix}"
            handler = star_handlers_registry.star_handlers_map[handler_name]
            filter_names = {type(item).__name__ for item in handler.event_filters}
            command_filter = next(
                item
                for item in handler.event_filters
                if type(item).__name__ == "CommandFilter"
            )
            permission_filter = next(
                item
                for item in handler.event_filters
                if type(item).__name__ == "PermissionTypeFilter"
            )

            self.assertEqual(
                command_filter.get_complete_command_names(),
                [command_name],
            )
            self.assertIn("PermissionTypeFilter", filter_names)
            self.assertEqual(
                permission_filter.permission_type,
                filter.PermissionType.ADMIN,
            )

    def test_metadata_declares_version_and_matrix_support(self) -> None:
        """Metadata should expose the plugin contract used by AstrBot's loader."""
        from astrbot.core.star.star_manager import PluginManager

        plugin_root = Path(__file__).resolve().parents[1]
        metadata = PluginManager._load_plugin_metadata(str(plugin_root))

        self.assertIsNotNone(metadata)
        self.assertEqual(metadata.version, "0.3.0")
        self.assertEqual(metadata.support_platforms, ["matrix"])

    def test_schema_defaults_are_safe(self) -> None:
        """The installed plugin should remain disabled until explicitly enabled."""
        plugin_root = Path(__file__).resolve().parents[1]
        schema = json.loads((plugin_root / "_conf_schema.json").read_text("utf-8"))

        config_items = schema["matrix_rule_react"]["items"]
        self.assertFalse(config_items["enable"]["default"])
        self.assertTrue(config_items["emojis"]["default"])
        self.assertEqual(config_items["rules"]["type"], "template_list")
        self.assertEqual(config_items["rules"]["default"], [])


if __name__ == "__main__":
    unittest.main()
