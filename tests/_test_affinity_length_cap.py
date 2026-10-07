"""Affinity length cap and pool-sized chat concurrency.

Two independent limits that both used to be fixed constants:

* ``derive_affinity_key`` pinned every conversation to one upstream account so
  the account-level prompt cache keeps hitting. That is right for normal
  conversations, but a conversation's body grows monotonically, and past a few
  hundred messages the oversized request makes the upstream drop the
  connection. ``WB_AFFINITY_MAX_MSGS`` releases the pin once a conversation is
  that long, trading its prefix cache for not being retried.

* ``MAX_CONCURRENT_CHAT`` was a fixed 32 regardless of pool size.
  ``WB_MAX_CONCURRENT_CHAT=auto`` sizes it from the ready-account count.

Run with: python _test_affinity_length_cap.py
No upstream credentials or outbound network are used.
"""
import os
import sys
import tempfile
import threading
import unittest

_startup_dir = tempfile.TemporaryDirectory(prefix="affinity-cap-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy


def conversation(length):
    """A conversation of exactly `length` messages, stable prefix first."""
    msgs = [{"role": "system", "content": "you are a helpful assistant"}]
    for i in range(max(0, length - 1)):
        msgs.append({"role": "user" if i % 2 == 0 else "assistant",
                     "content": "turn %d" % i})
    return msgs


class AffinityLengthCapTests(unittest.TestCase):
    """The cap must only touch conversations past the threshold."""

    def setUp(self):
        self._saved = wb_proxy.AFFINITY_MAX_MSGS

    def tearDown(self):
        wb_proxy.AFFINITY_MAX_MSGS = self._saved

    def test_short_conversations_still_bind(self):
        wb_proxy.AFFINITY_MAX_MSGS = 400
        for length in (1, 2, 10, 399, 400):
            key = wb_proxy.derive_affinity_key(conversation(length))
            self.assertTrue(key, "msgs=%d should keep its affinity key" % length)
            self.assertTrue(key.startswith("pfx-"))

    def test_long_conversations_are_released(self):
        wb_proxy.AFFINITY_MAX_MSGS = 400
        for length in (401, 500, 1245):
            self.assertIsNone(
                wb_proxy.derive_affinity_key(conversation(length)),
                "msgs=%d should be released so the pool can rotate" % length)

    def test_threshold_is_inclusive(self):
        """msgs == cap binds; the cap is a limit, not an off-by-one."""
        wb_proxy.AFFINITY_MAX_MSGS = 3
        self.assertIsNotNone(wb_proxy.derive_affinity_key(conversation(3)))
        self.assertIsNone(wb_proxy.derive_affinity_key(conversation(4)))

    def test_zero_disables_the_cap(self):
        """0 keeps the pre-1.6.x behaviour: always bind."""
        wb_proxy.AFFINITY_MAX_MSGS = 0
        for length in (1, 400, 5000):
            self.assertIsNotNone(wb_proxy.derive_affinity_key(conversation(length)))

    def test_bound_conversations_keep_a_stable_key(self):
        """The whole point: later turns of one conversation share one account."""
        wb_proxy.AFFINITY_MAX_MSGS = 400
        first = wb_proxy.derive_affinity_key(conversation(10))
        later = wb_proxy.derive_affinity_key(conversation(300))
        self.assertEqual(first, later)

    def test_distinct_conversations_do_not_collide(self):
        wb_proxy.AFFINITY_MAX_MSGS = 400
        a = conversation(10)
        b = conversation(10)
        b[1] = {"role": "user", "content": "a different opening turn"}
        self.assertNotEqual(wb_proxy.derive_affinity_key(a),
                            wb_proxy.derive_affinity_key(b))

    def test_degenerate_inputs_are_safe(self):
        wb_proxy.AFFINITY_MAX_MSGS = 400
        self.assertIsNone(wb_proxy.derive_affinity_key(None))
        self.assertIsNone(wb_proxy.derive_affinity_key([]))


class ChatSlotResizeTests(unittest.TestCase):
    """resize_chat_slots grows the ceiling, never shrinks it."""

    def setUp(self):
        self._auto = wb_proxy.CHAT_SLOTS_AUTO
        self._max = wb_proxy.MAX_CONCURRENT_CHAT
        self._slots = wb_proxy._chat_slots

    def tearDown(self):
        wb_proxy.CHAT_SLOTS_AUTO = self._auto
        wb_proxy.MAX_CONCURRENT_CHAT = self._max
        wb_proxy._chat_slots = self._slots

    def test_auto_sizes_from_the_pool(self):
        wb_proxy.CHAT_SLOTS_AUTO = True
        wb_proxy.MAX_CONCURRENT_CHAT = wb_proxy.CHAT_SLOTS_FLOOR
        self.assertEqual(wb_proxy.resize_chat_slots(110), 110)
        self.assertEqual(wb_proxy.MAX_CONCURRENT_CHAT, 110)
        # The new semaphore really does carry that many permits.
        acquired = 0
        while wb_proxy._chat_slots.acquire(blocking=False):
            acquired += 1
        self.assertEqual(acquired, 110)
        for _ in range(acquired):
            wb_proxy._chat_slots.release()

    def test_small_pool_falls_back_to_the_floor(self):
        wb_proxy.CHAT_SLOTS_AUTO = True
        wb_proxy.MAX_CONCURRENT_CHAT = wb_proxy.CHAT_SLOTS_FLOOR
        self.assertEqual(wb_proxy.resize_chat_slots(3), wb_proxy.CHAT_SLOTS_FLOOR)

    def test_never_shrinks(self):
        """A shrink would let releases exceed the ceiling and raise ValueError."""
        wb_proxy.CHAT_SLOTS_AUTO = True
        wb_proxy.MAX_CONCURRENT_CHAT = wb_proxy.CHAT_SLOTS_FLOOR
        wb_proxy.resize_chat_slots(200)
        slots = wb_proxy._chat_slots
        self.assertEqual(wb_proxy.resize_chat_slots(5), 200)
        self.assertIs(wb_proxy._chat_slots, slots)

    def test_fixed_setting_is_a_no_op(self):
        """A numeric WB_MAX_CONCURRENT_CHAT keeps the 1.6.x behaviour."""
        wb_proxy.CHAT_SLOTS_AUTO = False
        wb_proxy.MAX_CONCURRENT_CHAT = 32
        slots = wb_proxy._chat_slots
        self.assertEqual(wb_proxy.resize_chat_slots(500), 32)
        self.assertEqual(wb_proxy.MAX_CONCURRENT_CHAT, 32)
        self.assertIs(wb_proxy._chat_slots, slots)

    def test_garbage_input_is_ignored(self):
        wb_proxy.CHAT_SLOTS_AUTO = True
        wb_proxy.MAX_CONCURRENT_CHAT = wb_proxy.CHAT_SLOTS_FLOOR
        self.assertEqual(wb_proxy.resize_chat_slots(None), wb_proxy.CHAT_SLOTS_FLOOR)
        self.assertEqual(wb_proxy.resize_chat_slots("abc"), wb_proxy.CHAT_SLOTS_FLOOR)


class SlotAccountingTests(unittest.TestCase):
    """A resize must not corrupt the in-flight count."""

    def test_resize_preserves_held_slots(self):
        saved_auto = wb_proxy.CHAT_SLOTS_AUTO
        saved_max = wb_proxy.MAX_CONCURRENT_CHAT
        saved_slots = wb_proxy._chat_slots
        try:
            wb_proxy.CHAT_SLOTS_AUTO = True
            wb_proxy.MAX_CONCURRENT_CHAT = wb_proxy.CHAT_SLOTS_FLOOR
            wb_proxy._chat_slots = threading.BoundedSemaphore(wb_proxy.CHAT_SLOTS_FLOOR)
            self.assertTrue(wb_proxy._chat_slots.acquire(blocking=False))
            wb_proxy.resize_chat_slots(64)
            # The permit taken before the resize belongs to the old semaphore;
            # releasing it there must not raise on the new one.
            for _ in range(64):
                self.assertTrue(wb_proxy._chat_slots.acquire(blocking=False))
            for _ in range(64):
                wb_proxy._chat_slots.release()
        finally:
            wb_proxy.CHAT_SLOTS_AUTO = saved_auto
            wb_proxy.MAX_CONCURRENT_CHAT = saved_max
            wb_proxy._chat_slots = saved_slots


if __name__ == "__main__":
    unittest.main(verbosity=2)
