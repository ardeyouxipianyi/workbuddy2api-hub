"""SSE heartbeat coverage for Codex's "Reconnecting..." failure (issue #578).

The upstream may stay completely silent for longer than the client's idle
window. A comment frame keeps the socket alive, and it must be written by a
background thread: checking only before a blocking read cannot cover that gap.
These tests pin the wire format, the idle rule, serialized writes, and the
blocked-reader case that motivated the thread.

Run with: python tests/_test_stream_keepalive.py
No network access is used.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy as P

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


print("[1] heartbeat writes a valid SSE comment frame")

written = []
beat = P.SSEHeartbeat(written.append, interval=0.05)
time.sleep(0.16)
beat.close()
check("an idle stream emits at least one heartbeat", bool(written), written)
check("every heartbeat is a comment frame",
      all(item == b": keepalive\n\n" for item in written), written)
check("heartbeat never looks like a data event",
      all(b"data:" not in item for item in written), written)

print()
print("[2] application writes reset the idle timer")

written = []
beat = P.SSEHeartbeat(written.append, interval=0.25)
for _ in range(4):
    time.sleep(0.08)
    beat.write(b"data: work\n\n")
beat.close()
check("responsive traffic prevents heartbeats",
      written == [b"data: work\n\n"] * 4, written)

print()
print("[3] a blocked reader still gets a heartbeat")

written = []
gate = threading.Event()
beat = P.SSEHeartbeat(written.append, interval=0.05)
blocked = threading.Thread(target=gate.wait, daemon=True)
blocked.start()
time.sleep(0.16)
was_blocked = blocked.is_alive()
gate.set()
blocked.join(1)
beat.close()
check("the reader stayed blocked while heartbeats went out",
      was_blocked and bool(written), written)

print()
print("[4] heartbeat and request writes cannot interleave")

written = []
beat = P.SSEHeartbeat(written.append, interval=0.01)
payload = b"data: " + (b"x" * 4000) + b"\n\n"
producer = threading.Thread(
    target=lambda: [beat.write(payload) for _ in range(20)], daemon=True)
producer.start()
producer.join(2)
beat.close()
check("all application payloads arrive intact",
      written.count(payload) == 20, len(written))
check("heartbeats are whole frames between payloads",
      all(item == payload or item == b": keepalive\n\n" for item in written),
      written[:3])

print()
print("[5] close stops the daemon and is idempotent")

written = []
beat = P.SSEHeartbeat(written.append, interval=0.05)
beat.close()
count = len(written)
time.sleep(0.12)
beat.close()
check("no heartbeats after close", len(written) == count, written)
check("the heartbeat thread has exited", not beat._thread.is_alive(),
      beat._thread.is_alive())

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
