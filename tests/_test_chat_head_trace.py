"""Request-arrival trace: a slow upload must leave a line in the log.

The chat log line is written only after the request body is in hand, so a
request that spent minutes uploading left no trace at all: a conversation
looked stuck while the log showed nothing for that window, and there was no
way to tell a slow upload from a request that never arrived.

`do_POST` now logs `chat head` the moment a model route's request head
reaches the process, before any body work. Paired with `body_wait`, the two
lines answer the question directly:

    chat head: /v1/chat/completions content_length=1080150 ...
    chat: ... body_wait=1051ms

The trace is deliberately limited to the model routes a conversation
actually uses, so panel polling does not fill the log.

    python tests/_test_chat_head_trace.py
"""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

with open(os.path.join(ROOT, "wb_proxy.py"), encoding="utf-8") as fh:
    SOURCE = fh.read()


class ChatHeadTraceTests(unittest.TestCase):
    def test_model_routes_are_traced(self):
        """The routes a conversation uses are in the traced set."""
        block = re.search(r"_CHAT_PATHS = \((.*?)\)", SOURCE, re.S)
        self.assertIsNotNone(block, "_CHAT_PATHS is gone; the arrival trace "
                                     "has no route list")
        body = block.group(1)
        for route in ("/v1/chat/completions", "/v1/responses",
                      "/v1/messages", "/v1/completions"):
            self.assertIn('"%s"' % route, body,
                          "%s must be traced on arrival" % route)

    def test_panel_routes_are_not_traced(self):
        """Panel polling must stay out of the arrival trace."""
        block = re.search(r"_CHAT_PATHS = \((.*?)\)", SOURCE, re.S)
        self.assertIsNotNone(block)
        body = block.group(1)
        for route in ("/usage", "/accounts", "/settings", "/proxy/discover"):
            self.assertNotIn('"%s"' % route, body,
                             "%s is panel polling, not a conversation route"
                             % route)

    def test_the_trace_is_written_before_body_work(self):
        """`chat head` must be logged before the body is read.

        Logging it after the body is what made a slow upload invisible; the
        whole point is that the line lands on arrival.
        """
        self.assertIn('log("chat head:', SOURCE,
                      "the arrival trace line is gone")
        head_at = SOURCE.index('log("chat head:')
        path_at = SOURCE.index('path = self.path.split("?")[0]', head_at - 4000)
        self.assertLess(path_at, head_at,
                        "chat head must be logged after the path is known")
        # The guard must come before the body handling in do_POST.
        body_at = SOURCE.index("payload = self._payload_or_error", head_at)
        self.assertLess(head_at, body_at,
                        "chat head is logged after the body is read, so a "
                        "slow upload is still invisible")

    def test_it_is_tagged_chat(self):
        """The line carries tag="chat" so the log filter files it correctly."""
        head = SOURCE[SOURCE.index('log("chat head:'):]
        head = head[:head.index(")\n")]
        self.assertIn('tag="chat"', head,
                      "chat head without tag=chat lands in the wrong log filter")

    def test_the_filter_recognises_the_prefix(self):
        """The chat log filter must match `chat head:`."""
        block = re.search(r"_LOG_TAG_HINTS = \((.*?)\n\)", SOURCE, re.S)
        self.assertIsNotNone(block, "_LOG_TAG_HINTS is gone")
        self.assertIn('"chat head:"', block.group(1),
                      "the chat filter no longer matches the arrival trace")


if __name__ == "__main__":
    unittest.main(verbosity=2)
