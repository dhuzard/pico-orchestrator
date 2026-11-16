"""Lost-anchor cleanup observes completion without signaling surviving processes."""
from contextlib import ExitStack
from itertools import count
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))
import execution_supervisor as supervisor


class LostAnchorDrainTest(unittest.TestCase):
    def observe(self, observations, *, anchor_gone=False, anchor_state=None, denied=False):
        ticks = count(0, .2)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(supervisor, 'session_members', side_effect=observations))
            stack.enter_context(mock.patch.object(supervisor, 'identity', return_value=None))
            stack.enter_context(mock.patch.object(supervisor.sys, 'platform', 'linux'))
            stack.enter_context(mock.patch.object(supervisor.run_lease, '_proc_stat', return_value=anchor_state))
            stack.enter_context(mock.patch.object(supervisor.time, 'monotonic', side_effect=lambda: next(ticks)))
            stack.enter_context(mock.patch.object(supervisor.time, 'sleep'))
            probe = stack.enter_context(mock.patch.object(supervisor.os, 'kill'))
            if anchor_gone:
                probe.side_effect = ProcessLookupError
            elif denied:
                probe.side_effect = PermissionError
            group_signal = stack.enter_context(mock.patch.object(supervisor.os, 'killpg'))
            process_signal = stack.enter_context(mock.patch.object(supervisor, 'pidfd_send'))
            result = supervisor.stop(123, 'recorded-birth')
            group_signal.assert_not_called()
            process_signal.assert_not_called()
            self.assertTrue(all(call.args == (123, 0) for call in probe.call_args_list))
            return result

    def test_killed_siblings_can_drain_after_anchor_disappears(self):
        self.assertTrue(self.observe([[(124, 123, 'R')], [(124, 123, 'R')], []], anchor_gone=True))

    def test_surviving_siblings_keep_the_barrier(self):
        self.assertFalse(self.observe(lambda _: [(124, 123, 'S')], anchor_gone=True))

    def test_empty_session_with_unidentified_live_anchor_keeps_the_barrier(self):
        observations = iter([[(124, 123, 'R')]])
        self.assertFalse(self.observe(lambda _: next(observations, []), anchor_state=['S']))

    def test_empty_session_with_unreadable_anchor_keeps_the_barrier(self):
        observations = iter([[(124, 123, 'R')]])
        self.assertFalse(self.observe(lambda _: next(observations, []), denied=True))

    def test_empty_session_and_zombie_anchor_confirm_completion(self):
        self.assertTrue(self.observe([[(124, 123, 'R')], []], anchor_state=['Z']))

    def test_initial_session_observation_failure_keeps_the_barrier(self):
        self.assertFalse(self.observe([None], anchor_gone=True))

    def test_session_observation_failure_during_drain_keeps_the_barrier(self):
        self.assertFalse(self.observe([[(124, 123, 'R')], None], anchor_gone=True))
