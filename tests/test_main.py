"""Tests for the Matrix rule-reaction plugin."""

import functools
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from astrbot.api.event import filter
from astrbot.api.message_components import At
from astrbot.core.platform.message_type import MessageType
from astrbot.core.star.filter.command import GreedyStr
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
        group_id: str = "!room:example.org",
        message_type: MessageType = MessageType.GROUP_MESSAGE,
        raw_msgtype: str = "m.text",
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
            group_id: Matrix room ID for a group message.
            message_type: AstrBot conversation message type.
            raw_msgtype: Matrix-native event ``msgtype``.
            event_id: Matrix event ID, or an empty string to simulate a bad event.
            is_native_wake: Whether AstrBot already recognized a native wake condition.
            reaction_error: Optional exception raised by ``react``.
        """
        self.message_obj = SimpleNamespace(
            message_id=event_id,
            message_str=message_text,
            type=message_type,
            raw_message=SimpleNamespace(
                event_id=event_id,
                msgtype=raw_msgtype,
                content={"msgtype": raw_msgtype},
            ),
        )
        self.message_str = message_text if current_text is None else current_text
        self.messages = messages or []
        self.sender_id = sender_id
        self.self_id = self_id
        self.group_id = group_id
        self.message_type = message_type
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

    def get_group_id(self) -> str:
        """Return the fake Matrix room ID.

        Returns:
            Matrix room ID supplied at construction time.
        """
        return self.group_id

    def get_message_type(self) -> MessageType:
        """Return the fake AstrBot message type.

        Returns:
            AstrBot message type supplied at construction time.
        """
        return self.message_type

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

    async def test_repeated_astrbot_4266_handler_binding_does_not_stack(self) -> None:
        """Repeated legacy-core loads should retain exactly one bound instance."""
        from astrbot.core.star.star_handler import star_handlers_registry

        handler_name = f"{MatrixRuleReactPlugin.__module__}_on_message"
        handler = star_handlers_registry.star_handlers_map[handler_name]
        original_handler = handler.handler
        try:
            for _ in range(11):
                plugin = self.make_plugin(
                    {"matrix_rule_react": {"enable": False}}
                )
                handler.handler = functools.partial(handler.handler, plugin)

            self.assertIsInstance(handler.handler, functools.partial)
            self.assertEqual(len(handler.handler.args), 1)
            await handler.handler(FakeEvent())
        finally:
            handler.handler = original_handler

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
            "data.plugins.astrbot_plugin_matrix_rule_react.message_handler.random.choice",
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
                            "selection": "fixed",
                            "reactions": ["👍"],
                            "conditions": [
                                {
                                    "match_type": "keyword",
                                    "pattern": "build passed",
                                }
                            ],
                        },
                        {
                            "selection": "fixed",
                            "reactions": ["🎉"],
                            "conditions": [
                                {"match_type": "keyword", "pattern": "build"}
                            ],
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
                            "selection": "random",
                            "reactions": [" 🎉 ", "🎉", "🚀"],
                            "conditions": [
                                {
                                    "match_type": "regex",
                                    "pattern": r"^deploy\s+success$",
                                }
                            ],
                        }
                    ],
                }
            }
        )
        event = FakeEvent(message_text="deploy success", is_native_wake=False)

        with mock.patch(
            "data.plugins.astrbot_plugin_matrix_rule_react.rules.random.choice",
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
                            "selection": "fixed",
                            "reactions": ["👋"],
                            "conditions": [
                                {
                                    "match_type": "user_id",
                                    "pattern": "@alice:example.org",
                                }
                            ],
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

    async def test_multi_condition_rule_requires_every_supported_condition(
        self,
    ) -> None:
        """All six supported condition types should combine with AND semantics."""
        plugin = self.make_plugin(
            {
                "matrix_rule_react": {
                    "enable": True,
                    "rules": [
                        {
                            "selection": "fixed",
                            "reactions": ["✅"],
                            "conditions": [
                                {"match_type": "keyword", "pattern": "deploy"},
                                {
                                    "match_type": "regex",
                                    "pattern": r"passed$",
                                },
                                {
                                    "match_type": "user_id",
                                    "pattern": "@alice:example.org",
                                },
                                {
                                    "match_type": "bot_id",
                                    "pattern": "@bot:example.org",
                                },
                                {
                                    "match_type": "group_id",
                                    "pattern": "!ci:example.org",
                                },
                                {
                                    "match_type": "message_type",
                                    "pattern": "group",
                                },
                            ],
                        }
                    ],
                }
            }
        )
        matching_event = FakeEvent(
            message_text="deploy passed",
            group_id="!ci:example.org",
            is_native_wake=False,
        )
        wrong_group_event = FakeEvent(
            message_text="deploy passed",
            group_id="!other:example.org",
            is_native_wake=False,
        )

        await plugin.on_message(matching_event)
        await plugin.on_message(wrong_group_event)

        self.assertEqual(matching_event.reactions, ["✅"])
        self.assertEqual(wrong_group_event.reactions, [])

    async def test_simple_rule_templates_apply_boolean_semantics(self) -> None:
        """The five dashboard templates should implement their named relations."""
        template_cases = [
            (
                "all_rule",
                {
                    "conditions": [
                        {"match_type": "keyword", "pattern": "deploy"},
                    ],
                },
                FakeEvent(message_text="deploy passed", is_native_wake=False),
                ["🅰️"],
            ),
            (
                "all_rule",
                {
                    "conditions": [
                        {"match_type": "keyword", "pattern": "deploy"},
                        {"match_type": "user_id", "pattern": "@alice:example.org"},
                    ],
                },
                FakeEvent(message_text="deploy passed", is_native_wake=False),
                ["🅰️"],
            ),
            (
                "all_rule",
                {
                    "conditions": [
                        {"match_type": "keyword", "pattern": "deploy"},
                        {"match_type": "user_id", "pattern": "@bob:example.org"},
                    ],
                },
                FakeEvent(message_text="deploy passed", is_native_wake=False),
                [],
            ),
            (
                "any_rule",
                {
                    "conditions": [
                        {"match_type": "keyword", "pattern": "failed"},
                        {"match_type": "user_id", "pattern": "@alice:example.org"},
                    ],
                },
                FakeEvent(message_text="deploy passed", is_native_wake=False),
                ["🅰️"],
            ),
            (
                "any_rule",
                {
                    "conditions": [
                        {"match_type": "keyword", "pattern": "failed"},
                        {"match_type": "user_id", "pattern": "@bob:example.org"},
                    ],
                },
                FakeEvent(message_text="deploy passed", is_native_wake=False),
                [],
            ),
            (
                "any_rule",
                {
                    "conditions": [
                        {"match_type": "keyword", "pattern": "failed"},
                        {"match_type": "group_id", "pattern": "!other:example.org"},
                        {"match_type": "message_type", "pattern": "group"},
                    ],
                },
                FakeEvent(message_text="deploy passed", is_native_wake=False),
                ["🅰️"],
            ),
            (
                "all_rule",
                {
                    "conditions": [
                        {"match_type": "keyword", "pattern": "deploy"},
                        {"match_type": "user_id", "pattern": "@alice:example.org"},
                        {"match_type": "group_id", "pattern": "!room:example.org"},
                    ],
                },
                FakeEvent(message_text="deploy passed", is_native_wake=False),
                ["🅰️"],
            ),
            (
                "all_rule",
                {
                    "conditions": [
                        {"match_type": "keyword", "pattern": "deploy"},
                        {"match_type": "user_id", "pattern": "@alice:example.org"},
                        {"match_type": "group_id", "pattern": "!other:example.org"},
                    ],
                },
                FakeEvent(message_text="deploy passed", is_native_wake=False),
                [],
            ),
        ]

        for template_key, conditions, event, expected_reactions in template_cases:
            with self.subTest(template_key=template_key, expected=expected_reactions):
                rule = {
                    "__template_key": template_key,
                    "selection": "fixed",
                    "reactions": ["🅰️"],
                    **conditions,
                }
                plugin = self.make_plugin(
                    {"matrix_rule_react": {"enable": True, "rules": [rule]}}
                )

                await plugin.on_message(event)

                self.assertEqual(event.reactions, expected_reactions)

    async def test_custom_condition_array_supports_or_mode(self) -> None:
        """An advanced rule may explicitly combine its condition array with OR."""
        plugin = self.make_plugin(
            {
                "matrix_rule_react": {
                    "enable": True,
                    "rules": [
                        {
                            "__template_key": "any_rule",
                            "selection": "fixed",
                            "reactions": ["🟢"],
                            "match_mode": "any",
                            "conditions": [
                                {"match_type": "keyword", "pattern": "failed"},
                                {
                                    "match_type": "user_id",
                                    "pattern": "@alice:example.org",
                                },
                            ],
                        }
                    ],
                }
            }
        )
        event = FakeEvent(message_text="deploy passed", is_native_wake=False)

        await plugin.on_message(event)
        list_results = [result async for result in plugin.list_rules(event)]

        self.assertEqual(event.reactions, ["🟢"])
        self.assertIn(" OR ", list_results[0])

    async def test_message_type_accepts_matrix_native_msgtype(self) -> None:
        """A message-type condition may target Matrix's native ``msgtype``."""
        plugin = self.make_plugin(
            {
                "matrix_rule_react": {
                    "enable": True,
                    "rules": [
                        {
                            "selection": "fixed",
                            "reactions": ["📝"],
                            "conditions": [
                                {"match_type": "message_type", "pattern": "m.text"}
                            ],
                        }
                    ],
                }
            }
        )
        text_event = FakeEvent(raw_msgtype="m.text", is_native_wake=False)
        image_event = FakeEvent(raw_msgtype="m.image", is_native_wake=False)

        await plugin.on_message(text_event)
        await plugin.on_message(image_event)

        self.assertEqual(text_event.reactions, ["📝"])
        self.assertEqual(image_event.reactions, [])

    async def test_legacy_single_condition_rule_remains_compatible(self) -> None:
        """Rules persisted before the conditions-array change should still work."""
        plugin = self.make_plugin(
            {
                "matrix_rule_react": {
                    "enable": True,
                    "rules": [
                        {
                            "match_type": "keyword",
                            "pattern": "legacy",
                            "selection": "fixed",
                            "reactions": ["♻️"],
                        }
                    ],
                }
            }
        )
        event = FakeEvent(message_text="legacy rule", is_native_wake=False)

        await plugin.on_message(event)

        self.assertEqual(event.reactions, ["♻️"])

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
                "random",
                "👋,🎉",
                "(user_id @alice:example.org) (group_id !room:example.org)",
            )
        ]
        added_rule = config["matrix_rule_react"]["rules"][0]
        list_results = [result async for result in plugin.list_rules(event)]
        remove_results = [result async for result in plugin.remove_rule(event, 1)]

        self.assertIn("已添加规则 #1", add_results[0])
        self.assertEqual(added_rule["__template_key"], "all_rule")
        self.assertEqual(added_rule["match_mode"], "all")
        self.assertEqual(
            added_rule["conditions"],
            [
                {"match_type": "user_id", "pattern": "@alice:example.org"},
                {"match_type": "group_id", "pattern": "!room:example.org"},
            ],
        )
        self.assertIn("[random]", list_results[0])
        self.assertIn("user_id='@alice:example.org'", list_results[0])
        self.assertIn("group_id='!room:example.org'", list_results[0])
        self.assertIn(" AND ", list_results[0])
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
                "fixed",
                "👍",
                "(regex [)",
            )
        ]
        fixed_results = [
            result
            async for result in plugin.add_rule(
                event,
                "fixed",
                "👍,🎉",
                "(keyword done)",
            )
        ]

        self.assertIn("正则表达式无效", regex_results[0])
        self.assertIn("fixed 模式", fixed_results[0])
        self.assertEqual(config["matrix_rule_react"]["rules"], [])
        self.assertEqual(config.save_count, 0)

    async def test_add_command_parses_grouped_regex_and_quoted_content(self) -> None:
        """Grouped conditions should preserve regex syntax and quoted type words."""
        config = PersistedConfig({"matrix_rule_react": {"enable": True, "rules": []}})
        plugin = MatrixRuleReactPlugin(SimpleNamespace(), config)
        event = FakeEvent()

        results = [
            result
            async for result in plugin.add_rule(
                event,
                "random",
                "👍,🎉",
                r"(regex ^build\s+(passed|success)$) "
                r'(keyword "contains user_id word") (message_type m.text)',
            )
        ]

        self.assertIn("已添加规则 #1", results[0])
        self.assertEqual(
            config["matrix_rule_react"]["rules"][0]["conditions"],
            [
                {
                    "match_type": "regex",
                    "pattern": r"^build\s+(passed|success)$",
                },
                {"match_type": "keyword", "pattern": "contains user_id word"},
                {"match_type": "message_type", "pattern": "m.text"},
            ],
        )
        self.assertEqual(config.save_count, 1)

    async def test_add_command_accepts_flat_variable_length_conditions(self) -> None:
        """The condition tail should also accept an unparenthesized sequence."""
        config = PersistedConfig({"matrix_rule_react": {"enable": True, "rules": []}})
        plugin = MatrixRuleReactPlugin(SimpleNamespace(), config)
        event = FakeEvent()

        results = [
            result
            async for result in plugin.add_rule(
                event,
                "fixed",
                "✅",
                "keyword build passed user_id @alice:example.org "
                "bot_id @bot:example.org group_id !room:example.org "
                "message_type group",
            )
        ]

        conditions = config["matrix_rule_react"]["rules"][0]["conditions"]
        self.assertIn("已添加规则 #1", results[0])
        self.assertEqual(len(conditions), 5)
        self.assertEqual(
            conditions[0], {"match_type": "keyword", "pattern": "build passed"}
        )
        self.assertEqual(
            conditions[-1], {"match_type": "message_type", "pattern": "group"}
        )

    async def test_add_command_requires_valid_condition_array(self) -> None:
        """Empty, unsupported, and malformed condition arrays must be rejected."""
        config = PersistedConfig({"matrix_rule_react": {"enable": True, "rules": []}})
        plugin = MatrixRuleReactPlugin(SimpleNamespace(), config)
        event = FakeEvent()

        empty_results = [
            result async for result in plugin.add_rule(event, "fixed", "👍", "")
        ]
        unknown_results = [
            result
            async for result in plugin.add_rule(
                event,
                "fixed",
                "👍",
                "(unknown value)",
            )
        ]
        unclosed_results = [
            result
            async for result in plugin.add_rule(
                event,
                "fixed",
                "👍",
                "(keyword value",
            )
        ]

        self.assertIn("至少需要提供一个", empty_results[0])
        self.assertIn("规则类型无效", unknown_results[0])
        self.assertIn("括号未闭合", unclosed_results[0])
        self.assertEqual(config["matrix_rule_react"]["rules"], [])
        self.assertEqual(config.save_count, 0)

    async def test_add_command_accepts_probability_flag(self) -> None:
        """The add command should parse and store an optional probability prefix."""
        config = PersistedConfig({"matrix_rule_react": {"enable": True, "rules": []}})
        plugin = MatrixRuleReactPlugin(SimpleNamespace(), config)
        event = FakeEvent()

        results = [
            result
            async for result in plugin.add_rule(
                event,
                "fixed",
                "👍",
                "0.5 (keyword hello)",
            )
        ]

        rule = config["matrix_rule_react"]["rules"][0]
        self.assertIn("已添加规则 #1", results[0])
        self.assertIn("50%", results[0])
        self.assertEqual(rule["probability"], 0.5)
        self.assertEqual(config.save_count, 1)

    async def test_add_command_rejects_out_of_range_probability(self) -> None:
        """Probability values outside 0.0~1.0 must be rejected."""
        config = PersistedConfig({"matrix_rule_react": {"enable": True, "rules": []}})
        plugin = MatrixRuleReactPlugin(SimpleNamespace(), config)
        event = FakeEvent()

        too_high = [
            result
            async for result in plugin.add_rule(
                event,
                "fixed",
                "👍",
                "1.5 (keyword hello)",
            )
        ]
        too_low = [
            result
            async for result in plugin.add_rule(
                event,
                "fixed",
                "👍",
                "-0.1 (keyword hello)",
            )
        ]

        self.assertIn("必须在 0.0 到 1.0 之间", too_high[0])
        self.assertIn("必须在 0.0 到 1.0 之间", too_low[0])
        self.assertEqual(config["matrix_rule_react"]["rules"], [])
        self.assertEqual(config.save_count, 0)

    async def test_add_command_with_probability_in_flat_syntax(self) -> None:
        """Probability prefix should also work with flat (unparenthesized) syntax."""
        config = PersistedConfig({"matrix_rule_react": {"enable": True, "rules": []}})
        plugin = MatrixRuleReactPlugin(SimpleNamespace(), config)
        event = FakeEvent()

        results = [
            result
            async for result in plugin.add_rule(
                event,
                "fixed",
                "👍",
                "0.3 keyword hello",
            )
        ]

        rule = config["matrix_rule_react"]["rules"][0]
        self.assertIn("已添加规则 #1", results[0])
        self.assertIn("30%", results[0])
        self.assertEqual(rule["probability"], 0.3)
        self.assertEqual(config.save_count, 1)

    async def test_probability_gate_blocks_zero_probability_rules(self) -> None:
        """A rule with probability 0.0 should never fire."""
        from data.plugins.astrbot_plugin_matrix_rule_react.rules import (
            select_dynamic_reaction,
        )

        rules = [
            {
                "selection": "fixed",
                "reactions": ["👍"],
                "probability": 0.0,
                "conditions": [{"match_type": "keyword", "pattern": "hello"}],
            }
        ]
        event = FakeEvent()

        reaction = select_dynamic_reaction(event, rules)

        self.assertEqual(reaction, "")

    async def test_probability_gate_always_passes_at_one(self) -> None:
        """A rule with probability 1.0 should always fire when conditions match."""
        from data.plugins.astrbot_plugin_matrix_rule_react.rules import (
            select_dynamic_reaction,
        )

        rules = [
            {
                "selection": "fixed",
                "reactions": ["👍"],
                "probability": 1.0,
                "conditions": [{"match_type": "keyword", "pattern": "hello"}],
            }
        ]
        event = FakeEvent()

        reaction = select_dynamic_reaction(event, rules)

        self.assertEqual(reaction, "👍")

    async def test_probability_gate_respects_random_roll(self) -> None:
        """A rule with partial probability should respect the random roll."""
        from data.plugins.astrbot_plugin_matrix_rule_react.rules import (
            select_dynamic_reaction,
        )

        rules = [
            {
                "selection": "fixed",
                "reactions": ["👍"],
                "probability": 0.5,
                "conditions": [{"match_type": "keyword", "pattern": "hello"}],
            }
        ]
        event = FakeEvent()

        with mock.patch(
            "data.plugins.astrbot_plugin_matrix_rule_react.rules.random.random",
            side_effect=[0.3, 0.7],
        ):
            first = select_dynamic_reaction(event, rules)
            second = select_dynamic_reaction(event, rules)

        self.assertEqual(first, "👍")
        self.assertEqual(second, "")

    async def test_list_command_shows_probability(self) -> None:
        """The list command should display probability when set."""
        from data.plugins.astrbot_plugin_matrix_rule_react.rules import (
            format_probability,
        )

        self.assertEqual(format_probability(None), "")
        self.assertEqual(format_probability(1.0), "")
        self.assertEqual(format_probability(0.5), "50%")
        self.assertEqual(format_probability(0.0), "0%")
        self.assertEqual(format_probability(0.333), "33%")
        self.assertEqual(format_probability("invalid"), "")

        config = PersistedConfig(
            {
                "matrix_rule_react": {
                    "enable": True,
                    "rules": [
                        {
                            "selection": "fixed",
                            "reactions": ["👍"],
                            "probability": 0.75,
                            "conditions": [
                                {"match_type": "keyword", "pattern": "hello"}
                            ],
                        }
                    ],
                }
            }
        )
        plugin = MatrixRuleReactPlugin(SimpleNamespace(), config)
        event = FakeEvent()

        list_results = [result async for result in plugin.list_rules(event)]

        self.assertIn("75%", list_results[0])

    # --- Dynamic probability (built-in streak adjustment) tests ---

    async def test_calc_dynamic_probability_formula(self) -> None:
        """The dynamic probability formula should compute correct values."""
        from data.plugins.astrbot_plugin_matrix_rule_react.rules import (
            _calc_dynamic_probability,
        )

        # n=0.5: max_failures=4, max_successes=2
        self.assertAlmostEqual(_calc_dynamic_probability(0.5, 0, 0), 0.5)
        self.assertAlmostEqual(_calc_dynamic_probability(0.5, 1, 0), 0.625)
        self.assertAlmostEqual(_calc_dynamic_probability(0.5, 2, 0), 0.75)
        self.assertAlmostEqual(_calc_dynamic_probability(0.5, 3, 0), 0.875)
        self.assertAlmostEqual(_calc_dynamic_probability(0.5, 4, 0), 1.0)
        self.assertAlmostEqual(_calc_dynamic_probability(0.5, 0, 1), 0.25)
        self.assertAlmostEqual(_calc_dynamic_probability(0.5, 0, 2), 0.0)

        # n=0.3: max_failures=9, max_successes=3
        self.assertAlmostEqual(_calc_dynamic_probability(0.3, 9, 0), 1.0)
        self.assertAlmostEqual(_calc_dynamic_probability(0.3, 0, 3), 0.0)

        # Extremes
        self.assertAlmostEqual(_calc_dynamic_probability(0.0, 0, 0), 0.0)
        self.assertAlmostEqual(_calc_dynamic_probability(1.0, 0, 0), 1.0)

    async def test_dynamic_probability_failure_streak_to_certainty(self) -> None:
        """After (1/n)² failures, effective=1.0 and the rule always fires."""
        from data.plugins.astrbot_plugin_matrix_rule_react.rules import (
            select_dynamic_reaction,
        )

        rules = [{
            "selection": "fixed",
            "reactions": ["👍"],
            "probability": 0.5,
            "conditions": [{"match_type": "keyword", "pattern": "hello"}],
        }]
        event = FakeEvent()
        state: dict = {}

        with mock.patch(
            "data.plugins.astrbot_plugin_matrix_rule_react.rules.random.random",
            return_value=0.999,
        ):
            for i in range(4):
                result = select_dynamic_reaction(event, rules, state)
                self.assertEqual(result, "", f"Expected failure at iteration {i}")

            # 5th check: effective prob reaches 1.0 → always fires
            result = select_dynamic_reaction(event, rules, state)
            self.assertEqual(result, "👍")

    async def test_dynamic_probability_reduces_on_success(self) -> None:
        """Consecutive successes reduce effective probability."""
        from data.plugins.astrbot_plugin_matrix_rule_react.rules import (
            select_dynamic_reaction,
        )

        rules = [{
            "selection": "fixed",
            "reactions": ["👍"],
            "probability": 0.8,
            "conditions": [{"match_type": "keyword", "pattern": "hello"}],
        }]
        event = FakeEvent()
        state: dict = {}

        with mock.patch(
            "data.plugins.astrbot_plugin_matrix_rule_react.rules.random.random",
            return_value=0.0,
        ):
            # First success (n=0.8, max_successes=int(1/0.8)=1)
            result = select_dynamic_reaction(event, rules, state)
            self.assertEqual(result, "👍")

            # After 1 success, effective = 0.8 - 0.8*1/1 = 0.0 → never fires
            result = select_dynamic_reaction(event, rules, state)
            self.assertEqual(result, "")

    async def test_dynamic_probability_works_through_message_handler(self) -> None:
        """Dynamic probability automatically applies via handle_message."""
        config = PersistedConfig({
            "matrix_rule_react": {
                "enable": True,
                "rules": [{
                    "selection": "fixed",
                    "reactions": ["👍"],
                    "probability": 0.5,
                    "conditions": [
                        {"match_type": "keyword", "pattern": "build passed"},
                    ],
                }],
            }
        })
        plugin = MatrixRuleReactPlugin(SimpleNamespace(), config)
        event = FakeEvent(message_text="build passed", is_native_wake=False)

        # After (1/0.5)² = 4 consecutive failures, the 5th fires with certainty
        with mock.patch(
            "data.plugins.astrbot_plugin_matrix_rule_react.rules.random.random",
            return_value=0.999,
        ):
            for i in range(4):
                await plugin.on_message(event)
                self.assertEqual(event.reactions, [],
                                 f"Expected no reaction at attempt {i+1}")

            # 5th call: effective=1.0 → always fires
            await plugin.on_message(event)
            self.assertEqual(event.reactions, ["👍"])


class PluginFileTests(unittest.TestCase):
    """Check the plugin's user-facing configuration contract."""

    def test_main_delegates_handlers_to_separate_modules(self) -> None:
        """The entrypoint should retain imports while logic lives in split modules."""
        from data.plugins.astrbot_plugin_matrix_rule_react.message_handler import (
            MatrixRuleReactMessageMixin,
        )
        from data.plugins.astrbot_plugin_matrix_rule_react.rule_commands import (
            MatrixRuleReactCommandMixin,
        )
        from data.plugins.astrbot_plugin_matrix_rule_react.rules import (
            parse_conditions,
            select_dynamic_reaction,
        )

        self.assertTrue(issubclass(MatrixRuleReactPlugin, MatrixRuleReactMessageMixin))
        self.assertTrue(issubclass(MatrixRuleReactPlugin, MatrixRuleReactCommandMixin))
        self.assertEqual(
            MatrixRuleReactTriggerFilter.__module__,
            "data.plugins.astrbot_plugin_matrix_rule_react.trigger_filter",
        )
        self.assertEqual(
            parse_conditions.__module__,
            "data.plugins.astrbot_plugin_matrix_rule_react.rules",
        )
        self.assertEqual(
            select_dynamic_reaction.__module__,
            "data.plugins.astrbot_plugin_matrix_rule_react.rules",
        )

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
            if handler_suffix == "add_rule":
                self.assertEqual(
                    list(command_filter.handler_params),
                    ["selection", "reactions", "condition_array"],
                )
                self.assertIs(
                    command_filter.handler_params["condition_array"],
                    GreedyStr,
                )
                self.assertEqual(
                    command_filter.validate_and_convert_params(
                        [
                            "random",
                            "👍,🎉",
                            "(keyword",
                            "build",
                            "passed)",
                            "(group_id",
                            "!ci:example.org)",
                        ],
                        command_filter.handler_params,
                    ),
                    {
                        "selection": "random",
                        "reactions": "👍,🎉",
                        "condition_array": "(keyword build passed) "
                        "(group_id !ci:example.org)",
                    },
                )

    def test_metadata_declares_version_and_matrix_support(self) -> None:
        """Metadata should expose the plugin contract used by AstrBot's loader."""
        from astrbot.core.star.star_manager import PluginManager

        plugin_root = Path(__file__).resolve().parents[1]
        metadata = PluginManager._load_plugin_metadata(str(plugin_root))

        self.assertIsNotNone(metadata)
        self.assertEqual(metadata.version, "0.6.0")
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
        templates = config_items["rules"]["templates"]
        expected_templates = {
            "single_rule": "all",
            "a_and_b": "all",
            "a_or_b": "any",
            "all_rule": "all",
            "any_rule": "any",
        }
        base_options = [
            "keyword",
            "regex",
            "user_id",
            "bot_id",
            "group_id",
            "message_type",
        ]
        for template_key, match_mode in expected_templates.items():
            with self.subTest(template_key=template_key):
                rule_items = templates[template_key]["items"]
                self.assertEqual(rule_items["match_mode"]["default"], match_mode)
                if template_key in {"all_rule", "any_rule"}:
                    ci = rule_items["conditions"]["templates"]["condition"]["items"]
                    self.assertEqual(rule_items["conditions"]["type"], "template_list")
                    self.assertFalse(rule_items["conditions"]["invisible"])
                    self.assertEqual(ci["match_type"]["options"], base_options)
                    self.assertEqual(ci["negated"]["type"], "bool")
                    self.assertFalse(ci["negated"]["default"])
                    self.assertEqual(ci["patterns"]["type"], "list")
                elif template_key == "single_rule":
                    ci = rule_items["condition"]["items"]
                    self.assertEqual(rule_items["condition"]["type"], "object")
                    self.assertEqual(ci["match_type"]["options"], base_options)
                    self.assertEqual(ci["negated"]["type"], "bool")
                    self.assertFalse(ci["negated"]["default"])
                    self.assertEqual(ci["patterns"]["type"], "list")
                else:
                    for condition_key in ("condition_a", "condition_b"):
                        ci = rule_items[condition_key]["items"]
                        self.assertEqual(rule_items[condition_key]["type"], "object")
                        self.assertEqual(ci["match_type"]["options"], base_options)
                        self.assertEqual(ci["negated"]["type"], "bool")
                        self.assertFalse(ci["negated"]["default"])
                        self.assertEqual(ci["patterns"]["type"], "list")


if __name__ == "__main__":
    unittest.main()
