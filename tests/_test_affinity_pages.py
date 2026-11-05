"""Warm-account fallback and page rotation for long conversations.

The prompt cache lives upstream, per account: a hit needs the same account to
see the same prefix again. Two places used to give that up for nothing.

* When a conversation's account became unusable (cooling, parked, disabled),
  the binding was wiped and the next pick went wherever the pool's own
  preferences pointed - usually a cold account, so a long conversation paid a
  full-price prompt again. Now the conversation keeps a short most-recently-used
  list and the next pick walks it in order, so it moves to an account that has
  already seen the conversation instead of a stranger. The account that served
  the last turn stays the front entry, and an account that just failed a request
  is demoted to the back (see SessionAffinity.demote) rather than forgotten.

* Conversations past ``WB_AFFINITY_MAX_MSGS`` were released to the whole pool,
  where each account saw them once per pool-size turns - long enough for the
  prefix to fall out of the cache between visits. Now they rotate over one page
  of the pool (``WB_AFFINITY_PAGE_SIZE``): every account of the page keeps a
  warm copy, and the oversized bodies still spread over more than one account,
  which is what the length cap exists for.

Run with the current interpreter (python tests/run_all.py affinity).
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts


def make_account(uid, realm="cn", **extra):
    data = {"uid": uid, "accessToken": "token-%s" % uid, "realm": realm}
    data.update(extra)
    return wb_accounts.Account(data)


def credits(*days, remain=100):
    """A credits blob whose packages expire in the given days."""
    packages = []
    for index, day in enumerate(days):
        packages.append({
            "name": "pkg%d" % index,
            "package_code": "package",
            "remain": remain,
            "used": 0,
            "size": remain,
            "days_left": day,
            "no_expiry": False,
            "is_expired": day is not None and day < 0,
        })
    total = remain * len(packages)
    return {"remain": total, "used": 0, "size": total, "packages": packages}


def make_pool(accounts, window=None):
    directory = tempfile.mkdtemp(prefix="affinity-pages-")
    pool = wb_accounts.AccountPool(directory, log=lambda _m: None)
    pool.accounts = accounts
    if window is not None:
        pool.apply_expiring_window({"global": window, "intl": window, "cn": window})
    return pool


class SessionAffinityHistoryTests(unittest.TestCase):
    """The binding is a short most-recently-used list, not one account."""

    def test_the_last_account_is_the_front_entry(self):
        affinity = wb_accounts.SessionAffinity()
        affinity.bind("s", "a")
        affinity.bind("s", "b")
        self.assertEqual(affinity.get("s"), "b")
        self.assertEqual(affinity.recent("s"), ["b", "a"])

    def test_binding_again_moves_an_account_to_the_front(self):
        affinity = wb_accounts.SessionAffinity()
        for uid in ("a", "b", "c"):
            affinity.bind("s", uid)
        affinity.bind("s", "a")
        self.assertEqual(affinity.recent("s"), ["a", "c", "b"])

    def test_the_history_is_capped(self):
        affinity = wb_accounts.SessionAffinity(history=3)
        for uid in ("a", "b", "c", "d"):
            affinity.bind("s", uid)
        self.assertEqual(affinity.recent("s"), ["d", "c", "b"])

    def test_demote_drops_one_account_and_keeps_the_rest(self):
        affinity = wb_accounts.SessionAffinity()
        for uid in ("a", "b", "c"):
            affinity.bind("s", uid)
        affinity.demote("s", "c")
        self.assertEqual(affinity.recent("s"), ["b", "a"])
        affinity.demote("s")
        self.assertEqual(affinity.recent("s"), ["a"])

    def test_demoting_the_last_account_forgets_the_conversation(self):
        affinity = wb_accounts.SessionAffinity()
        affinity.bind("s", "a")
        affinity.demote("s", "a")
        self.assertIsNone(affinity.get("s"))
        self.assertEqual(affinity.recent("s"), [])

    def test_unbind_forgets_the_whole_conversation(self):
        affinity = wb_accounts.SessionAffinity()
        for uid in ("a", "b"):
            affinity.bind("s", uid)
        affinity.unbind("s")
        self.assertEqual(affinity.recent("s"), [])

    def test_expired_entries_drop_out(self):
        affinity = wb_accounts.SessionAffinity(ttl=0.05)
        affinity.bind("s", "old")
        time.sleep(0.06)
        affinity.bind("s", "new")
        self.assertEqual(affinity.recent("s"), ["new"])

    def test_the_front_entry_slides_its_expiry(self):
        affinity = wb_accounts.SessionAffinity(ttl=0.3)
        affinity.bind("s", "a")
        time.sleep(0.2)
        self.assertEqual(affinity.get("s"), "a")
        time.sleep(0.2)
        self.assertEqual(affinity.get("s"), "a",
                         "the account serving the conversation must not expire "
                         "while it keeps serving it")


class WarmFallbackTests(unittest.TestCase):
    """An unusable account hands the conversation to a warm one, not a cold one."""

    def _pool_with_warm_history(self):
        """fresh (unusable, most recent) -> warm (older) -> urgent (expiring).

        The expiring-credits preference would hand a new conversation to
        `urgent`; this conversation must go to `warm` instead.
        """
        fresh = make_account("fresh")
        warm = make_account("warm")
        urgent = make_account("urgent", credits=credits(1))
        pool = make_pool([fresh, warm, urgent], window=7)
        pool.affinity.bind("s", "warm")
        pool.affinity.bind("s", "fresh")
        fresh.note_error("boom")            # cooling for 60s
        return pool, fresh, warm, urgent

    def test_the_pool_preference_would_pick_the_urgent_account(self):
        """Guard for the test below: `urgent` really is the pool's choice."""
        pool, _fresh, _warm, urgent = self._pool_with_warm_history()
        self.assertEqual(pool.pick(realm="cn").uid, urgent.uid)

    def test_a_conversation_moves_to_its_warm_account(self):
        pool, _fresh, warm, _urgent = self._pool_with_warm_history()
        self.assertEqual(pool.pick_for_session(realm="cn", session_key="s").uid,
                         warm.uid)

    def test_the_fallback_account_becomes_the_binding(self):
        pool, _fresh, warm, _urgent = self._pool_with_warm_history()
        pool.pick_for_session(realm="cn", session_key="s")
        self.assertEqual(pool.affinity.get("s"), warm.uid)
        # And it stays there: the next turn goes straight to the warm account.
        self.assertEqual(pool.pick_for_session(realm="cn", session_key="s").uid,
                         warm.uid)

    def test_a_conversation_without_history_uses_the_pool(self):
        pool, _fresh, _warm, urgent = self._pool_with_warm_history()
        self.assertEqual(pool.pick_for_session(realm="cn", session_key="new").uid,
                         urgent.uid)

    def test_a_disabled_account_is_skipped_without_losing_the_history(self):
        pool, _fresh, warm, _urgent = self._pool_with_warm_history()
        pool.get("warm").enabled = False
        # Nothing warm is usable, so the pool decides - and the two warm
        # accounts stay in the history for the next time one of them recovers.
        self.assertEqual(pool.pick_for_session(realm="cn", session_key="s").uid,
                         "urgent")
        history = pool.affinity.recent("s")
        self.assertIn("warm", history)
        self.assertIn("fresh", history)


class PageRotationTests(unittest.TestCase):
    """Past the cap a conversation rotates over one page of the pool."""

    def _pool(self, uids=("a", "b", "c", "d", "e", "f")):
        return make_pool([make_account(uid) for uid in uids])

    def test_a_page_holds_one_slot_per_account(self):
        pool = self._pool()
        self.assertEqual([a.uid for a in pool._page_slice(pool.accounts, (0, 2))],
                         ["a", "c", "e"])
        self.assertEqual([a.uid for a in pool._page_slice(pool.accounts, (1, 2))],
                         ["b", "d", "f"])

    def test_the_page_does_not_depend_on_the_load_order(self):
        """Ranking by uid keeps a conversation's page stable across reloads."""
        pool = self._pool()
        shuffled = list(reversed(pool.accounts))
        self.assertEqual(sorted(a.uid for a in pool._page_slice(shuffled, (0, 3))),
                         sorted(a.uid for a in pool._page_slice(pool.accounts, (0, 3))))

    def test_a_page_is_scoped_to_its_realm(self):
        """A mixed-realm pool must not shrink a realm's page below the size.

        Ranking across the whole pool would put foreign accounts on the page;
        the realm filter then leaves it with fewer - or with one, which would
        pin the conversation, the state the length cap exists to leave.
        """
        intl = [make_account("intl-%d" % i, realm="intl") for i in range(6)]
        pool = make_pool([make_account("cn-0", realm="cn"),
                          make_account("cn-1", realm="cn")] + intl)
        self.assertEqual(sorted(a.uid for a in pool._page_slice(intl, (0, 3))),
                         ["intl-0", "intl-3"])
        self.assertEqual(sorted(a.uid for a in pool._page_slice(intl, (1, 3))),
                         ["intl-1", "intl-4"])

    def test_rotation_visits_every_member_of_the_page(self):
        pool = self._pool()
        picks = [pool.pick(realm="cn", page=(0, 2)).uid for _ in range(6)]
        self.assertEqual(set(picks), {"a", "c", "e"})
        self.assertNotEqual(picks[0], picks[1],
                            "a page must keep taking turns, not pin")

    def test_a_page_rotation_does_not_move_the_pool_cursor(self):
        pool = self._pool()
        before = pool._cursor
        for _ in range(4):
            pool.pick(realm="cn", page=(0, 2))
        self.assertEqual(pool._cursor, before)
        # The plain rotation still starts where it always did.
        self.assertEqual([pool.pick(realm="cn").uid for _ in range(3)],
                         ["a", "b", "c"])

    def test_a_page_with_nobody_ready_falls_back_to_the_pool(self):
        pool = self._pool()
        for uid in ("a", "c", "e"):
            pool.get(uid).enabled = False
        picked = pool.pick_for_session(realm="cn", session_key="s", page=(0, 2))
        self.assertIn(picked.uid, {"b", "d", "f"})

    def test_a_page_conversation_is_not_bound(self):
        """A binding would pin it back to one account - the state it left."""
        pool = self._pool()
        pool.pick_for_session(realm="cn", session_key="s", page=(0, 2))
        self.assertIsNone(pool.affinity.get("s"))
        self.assertEqual(pool.affinity.recent("s"), [])

    def test_a_page_covering_the_whole_small_pool_is_bound(self):
        """With two accounts page rotation would swap every turn (issue #718).

        Every turn would land on a different account, so neither can reuse the
        prefix cache. A page that covers the entire realm is therefore treated
        as a normal conversation and keeps one account.
        """
        pool = self._pool(("a", "b"))
        first = pool.pick_for_session(realm="cn", session_key="long", page=(0, 3))
        self.assertEqual(pool.affinity.get("long"), first.uid)
        picks = [pool.pick_for_session(
            realm="cn", session_key="long", page=(0, 3)).uid for _ in range(5)]
        self.assertEqual(set(picks), {first.uid})

    def test_no_page_means_the_whole_pool(self):
        pool = self._pool()
        picks = {pool.pick(realm="cn").uid for _ in range(6)}
        self.assertEqual(picks, {"a", "b", "c", "d", "e", "f"})

    def test_a_page_larger_than_the_pool_is_the_whole_pool(self):
        pool = self._pool(("a", "b"))
        self.assertEqual({pool.pick(realm="cn", page=(0, 5)).uid for _ in range(4)},
                         {"a", "b"})

    def test_garbage_pages_fall_back_to_the_whole_pool(self):
        pool = self._pool(("a", "b"))
        for page in (("x", "y"), (0,), None):
            self.assertEqual(
                {pool.pick(realm="cn", page=page).uid for _ in range(4)},
                {"a", "b"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
