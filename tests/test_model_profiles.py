#!/usr/bin/env python3
"""Deterministic tests for concrete model/runtime overrides."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import model_profiles  # noqa: E402
import routing_engine as engine  # noqa: E402


class ModelRuntimeProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Path(self.tmp.name) / "models.yaml"

    def tearDown(self):
        self.tmp.cleanup()

    def test_codex_override_changes_models_without_changing_routing_contract(self):
        base = engine.load_policy_assets(ROOT)
        model_profiles.cmd_set(
            self.config, "codex", "high", "openai-codex/gpt-7-pro", None, None, 8
        )
        configured = engine.load_policy_assets(ROOT, self.config)

        self.assertEqual(
            configured.policy["model_profiles"]["codex"]["capacity_models"]["high"]["model"],
            "openai-codex/gpt-7-pro",
        )
        for invariant in (
            "routes",
            "precedence",
            "fallback_route",
            "capacity_assignment",
            "execution",
            "execution_backends",
            "review_findings",
            "completion",
        ):
            self.assertEqual(configured.policy[invariant], base.policy[invariant], invariant)

    def test_new_model_needs_assessed_ambiguity_capacity(self):
        with self.assertRaisesRegex(ValueError, "require --ambiguity-capacity"):
            model_profiles.cmd_set(
                self.config, "codex", "high", "openai-codex/unknown", None, None
            )
        with self.assertRaisesRegex(ValueError, "requires at least 8"):
            model_profiles.cmd_set(
                self.config, "codex", "high", "openai-codex/unknown", None, None, 6
            )
        self.assertFalse(self.config.exists())

    def test_legacy_string_override_cannot_inherit_high_capacity(self):
        self.config.write_text(
            "schema_version: 1\nmodels:\n  codex:\n    high: openai-codex/unknown\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(engine.RoutingAssetError, "ambiguity_capacity must be at least 8"):
            engine.load_policy_assets(ROOT, self.config)

    def test_legacy_string_matching_builtin_keeps_its_assessment(self):
        self.config.write_text(
            "schema_version: 1\nmodels:\n  codex:\n    high: openai-codex/gpt-6.1-sol\n",
            encoding="utf-8",
        )
        assets = engine.load_policy_assets(ROOT, self.config)
        self.assertEqual(
            assets.policy["model_profiles"]["codex"]["capacity_models"]["high"]["ambiguity_capacity"],
            8,
        )

    def test_show_lists_all_five_tiers_and_set_accepts_light_and_max(self):
        self.assertEqual(model_profiles.TIERS, engine.CAPACITY_TIER_ORDER)
        summary = model_profiles._summary(self.config, None)
        for family in ("codex", "claude"):
            self.assertEqual(list(summary["models"][family]), list(engine.CAPACITY_TIER_ORDER))
        self.assertEqual(summary["models"]["codex"]["max"], "openai-codex/gpt-6-astra")
        self.assertEqual(summary["models"]["claude"]["light"], "haiku")
        with patch("sys.stdout"):
            self.assertEqual(model_profiles.main(
                ["--config", str(self.config), "set", "codex", "max", "openai-codex/gpt-7-max", "--ambiguity-capacity", "10"]), 0)
            self.assertEqual(model_profiles.main(
                ["--config", str(self.config), "set", "codex", "light", "openai-codex/gpt-7-nano", "--ambiguity-capacity", "2"]), 0)
        configured = engine.load_policy_assets(ROOT, self.config)
        models = configured.policy["model_profiles"]["codex"]["capacity_models"]
        self.assertEqual(models["max"]["model"], "openai-codex/gpt-7-max")
        self.assertEqual(models["light"]["model"], "openai-codex/gpt-7-nano")

    def test_new_claude_model_requires_explicit_backend_mapping(self):
        with self.assertRaises(ValueError):
            model_profiles.cmd_set(
                self.config, "claude", "high", "anthropic/claude-opus-6", None, None
            )
        self.assertFalse(self.config.exists())

        model_profiles.cmd_set(
            self.config,
            "claude",
            "high",
            "anthropic/claude-opus-6",
            "claude-opus-6",
            "3.0.0", 9,
        )
        assets = engine.load_policy_assets(ROOT, self.config)
        rules = engine.load_backend_compatibility(
            ROOT,
            model_config_path=self.config,
            effective_policy=assets.policy,
        )
        self.assertEqual(
            assets.policy["model_profiles"]["claude"]["capacity_models"]["high"]["model"],
            "anthropic/claude-opus-6",
        )
        self.assertEqual(
            rules["claude_code_model_ids"]["anthropic/claude-opus-6"],
            "claude-opus-6",
        )
        self.assertEqual(
            rules["claude_code_minimum_versions"]["anthropic/claude-opus-6"],
            "3.0.0",
        )

    def test_builtin_claude_compatibility_cannot_be_reinterpreted_or_weakened(self):
        with self.assertRaises(engine.RoutingAssetError):
            model_profiles.cmd_set(
                self.config,
                "claude",
                "high",
                "anthropic/claude-opus-5-5",
                "haiku",
                None,
            )
        self.assertFalse(self.config.exists())

        with self.assertRaises(engine.RoutingAssetError):
            model_profiles.cmd_set(
                self.config,
                "claude",
                "high",
                "anthropic/claude-opus-5-5",
                None,
                "1.0.0",
            )
        self.assertFalse(self.config.exists())

        model_profiles.cmd_set(
            self.config,
            "claude",
            "high",
            "anthropic/claude-opus-5-5",
            None,
            "9.0.0",
        )
        rules = engine.load_backend_compatibility(ROOT, model_config_path=self.config)
        self.assertEqual(
            rules["claude_code_model_ids"]["anthropic/claude-opus-5-5"],
            "claude-opus-5-5",
        )
        self.assertEqual(
            rules["claude_code_minimum_versions"]["anthropic/claude-opus-5-5"],
            "9.0.0",
        )

    def test_builtin_claude_mapping_and_minimum_version_cannot_be_weakened(self):
        self.config.write_text(
            "schema_version: 1\n"
            "claude_code:\n"
            "  model_ids:\n"
            "    anthropic/claude-opus-5-5: wrong-backend-id\n",
            encoding="utf-8",
        )
        with self.assertRaises(engine.RoutingAssetError):
            engine.load_policy_assets(ROOT, self.config)

        self.config.write_text(
            "schema_version: 1\n"
            "claude_code:\n"
            "  minimum_versions:\n"
            "    anthropic/claude-opus-5-5: 2.1.100\n",
            encoding="utf-8",
        )
        with self.assertRaises(engine.RoutingAssetError):
            engine.load_policy_assets(ROOT, self.config)

        self.config.write_text(
            "schema_version: 1\n"
            "claude_code:\n"
            "  minimum_versions:\n"
            "    anthropic/claude-opus-5-5: 3.0.0\n",
            encoding="utf-8",
        )
        assets = engine.load_policy_assets(ROOT, self.config)
        rules = engine.load_backend_compatibility(
            ROOT, model_config_path=self.config, effective_policy=assets.policy
        )
        self.assertEqual(rules["claude_code_minimum_versions"]["anthropic/claude-opus-5-5"], "3.0.0")

    def test_runtime_config_cannot_override_routing_policy(self):
        self.config.write_text(
            "schema_version: 1\n"
            "models:\n"
            "  codex:\n"
            "    high: openai-codex/gpt-7-pro\n"
            "routes: {}\n",
            encoding="utf-8",
        )
        with self.assertRaises(engine.RoutingAssetError):
            engine.load_policy_assets(ROOT, self.config)

    def test_invalid_serialized_candidate_never_replaces_valid_config(self):
        model_profiles.cmd_set(
            self.config, "codex", "high", "openai-codex/gpt-7-pro", None, None, 8
        )
        before = self.config.read_text(encoding="utf-8")
        with self.assertRaises(engine.RoutingAssetError):
            model_profiles.cmd_set(
                self.config, "codex", "high", "invalid model with spaces", None, None, 8
            )
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)
        self.assertFalse(self.config.with_name(self.config.name + ".tmp").exists())

    def test_reset_restores_repository_defaults(self):
        base = engine.load_policy_assets(ROOT)
        model_profiles.cmd_set(
            self.config, "codex", "high", "openai-codex/gpt-7-pro", None, None, 8
        )
        model_profiles.cmd_reset(self.config, "codex", "high", False)
        self.assertFalse(self.config.exists())
        restored = engine.load_policy_assets(ROOT)
        self.assertEqual(restored.policy["model_profiles"], base.policy["model_profiles"])

    def test_relative_environment_config_path_fails_closed(self):
        with patch.dict(os.environ, {engine.MODEL_RUNTIME_ENV: "relative/models.yaml"}):
            with self.assertRaises(engine.RoutingAssetError):
                engine.load_policy_assets(ROOT)


if __name__ == "__main__":
    unittest.main()
