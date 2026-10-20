#!/usr/bin/env python3
"""Deterministic tests for the validated lead-agent delegation policy."""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = REPO_ROOT / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import routing_engine as engine  # noqa: E402


class DelegationPolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.assets = engine.load_policy_assets(REPO_ROOT)

    def test_policy_loads_with_exact_delegation_modes(self):
        self.assertEqual(
            set(self.assets.delegation_policy["allowed_modes"]),
            engine.DELEGATION_MODES,
        )

    def test_family_fanout_never_exceeds_shared_limit(self):
        shared_max = self.assets.delegation_policy["shared"]["max_parallel_workers"]
        for family in ("codex", "claude"):
            self.assertLessEqual(
                self.assets.delegation_policy["families"][family]["max_parallel_workers"],
                shared_max,
            )

    def test_claude_tiers_resolve_through_central_model_profile(self):
        supported = set(self.assets.policy["model_profiles"]["claude"]["capacity_models"])
        tiers = self.assets.delegation_policy["families"]["claude"]["model_tiers"]
        configured = set(tiers.values())
        self.assertTrue(configured <= supported)
        self.assertEqual(tiers["light_worker"], "light")

    def test_invalid_family_fanout_is_rejected(self):
        mutated = copy.deepcopy(self.assets.delegation_policy)
        mutated["families"]["claude"]["max_parallel_workers"] = 4
        mutated["shared"]["max_parallel_workers"] = 3
        issues = []
        engine.validate_delegation_policy(
            mutated,
            self.assets.delegation_schema,
            self.assets.policy,
            issues,
        )
        self.assertTrue(any("cannot exceed shared.max_parallel_workers" in issue.message for issue in issues))

    def test_unknown_claude_capacity_tier_is_rejected(self):
        mutated = copy.deepcopy(self.assets.delegation_policy)
        # Bypass schema enumeration in a controlled test to prove cross-file
        # validation also fails rather than relying only on default-policy.
        mutated["families"]["claude"]["model_tiers"]["mid_worker"] = "not-a-tier"
        issues = []
        engine.validate_delegation_policy(
            mutated,
            self.assets.delegation_schema,
            self.assets.policy,
            issues,
        )
        self.assertTrue(issues)


if __name__ == "__main__":
    unittest.main()
