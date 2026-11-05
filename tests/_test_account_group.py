"""Account groups: persisted label, filter source, and group-scoped batch actions."""
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import wb_accounts
import wb_proxy


def make_account(uid, group=None, enabled=True):
    data = {
        "uid": uid,
        "accessToken": "token-%s" % uid,
        "realm": "intl",
        "enabled": enabled,
    }
    if group is not None:
        data["group"] = group
    return wb_accounts.Account(data)


class GroupFieldTests(unittest.TestCase):
    def test_default_is_empty_and_junk_is_ignored(self):
        self.assertEqual(make_account("g-default").group, "")
        for junk in (None, 123, [], {}, True):
            self.assertEqual(make_account("g-junk", group=junk).group, "")

    def test_group_is_stripped_and_bounded(self):
        self.assertEqual(make_account("g-pad", group="  Team A  ").group, "Team A")
        self.assertEqual(len(make_account("g-long", group="x" * 200).group), 64)

    def test_public_and_to_dict_carry_group(self):
        account = make_account("g-view", group="Team A")
        self.assertEqual(account.public()["group"], "Team A")
        self.assertEqual(account.to_dict()["group"], "Team A")

    def test_group_round_trips_through_the_credential_file(self):
        with tempfile.TemporaryDirectory(prefix="group-persist-") as directory:
            make_account("g-disk", group="Team A").save(directory)
            loaded = wb_accounts.AccountPool(directory, log=lambda _m: None).load()
            self.assertEqual({a.uid: a for a in loaded}["g-disk"].group, "Team A")


class GroupActionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="group-action-")
        self.pool = wb_accounts.AccountPool(self.directory, log=lambda _m: None)
        self.pool.accounts = [
            make_account("g-a", group="Team A", enabled=False),
            make_account("g-b", group="Team A", enabled=True),
            make_account("g-c", group="Team B", enabled=True),
            make_account("g-none", enabled=True),
        ]

    def test_set_group_and_clear(self):
        self.assertEqual(self.pool.set_group("g-none", "  Team C ")["group"], "Team C")
        self.assertEqual(self.pool.get("g-none").group, "Team C")
        self.assertEqual(self.pool.set_group("g-none", " ")["group"], "")
        self.assertIsNone(self.pool.set_group("missing", "x"))

    def test_group_batch_enable_only_touches_that_group(self):
        changed = self.pool.set_group_enabled("Team A", True)
        self.assertEqual(sorted(changed), ["g-a", "g-b"])
        self.assertTrue(self.pool.get("g-a").enabled)
        self.assertTrue(self.pool.get("g-a").tasks_enabled)
        self.assertTrue(self.pool.get("g-c").enabled)
        self.assertTrue(self.pool.get("g-none").enabled)

    def test_group_batch_disable_only_touches_that_group(self):
        changed = self.pool.set_group_enabled("Team A", False)
        self.assertEqual(sorted(changed), ["g-a", "g-b"])
        self.assertFalse(self.pool.get("g-a").enabled)
        self.assertFalse(self.pool.get("g-b").enabled)
        self.assertTrue(self.pool.get("g-c").enabled)
        self.assertTrue(self.pool.get("g-none").enabled)

    def test_empty_group_never_matches_everyone(self):
        before = [account.enabled for account in self.pool.accounts]
        self.assertEqual(self.pool.set_group_enabled("", False), [])
        self.assertEqual([account.enabled for account in self.pool.accounts], before)

    def test_route_accepts_group_and_rejects_bad_enabled(self):
        class Request(object):
            _route_accounts_set = wb_proxy.Handler._route_accounts_set
            _route_accounts_set_all = wb_proxy.Handler._route_accounts_set_all

            def __init__(self, payload):
                self.payload = payload
                self.status = None
                self.reply = None

            def _error(self, status, message, kind=""):
                self.status, self.reply = status, message
                return status, message

            def _json_panel(self, status, payload):
                self.status, self.reply = status, payload
                return status, payload

        with mock.patch.object(wb_proxy, "POOL", self.pool):
            request = Request({"uid": "g-none", "group": "Team C"})
            request._route_accounts_set(request.payload)
            self.assertEqual(request.status, 200)
            self.assertEqual(request.reply["account"]["group"], "Team C")

            request = Request({"enabled": True, "group": "Team A"})
            request._route_accounts_set_all(request.payload)
            self.assertEqual(request.status, 200)
            self.assertTrue(self.pool.get("g-a").enabled)
            self.assertTrue(self.pool.get("g-c").enabled)

            request = Request({"enabled": "false", "group": "Team A"})
            request._route_accounts_set_all(request.payload)
            self.assertEqual(request.status, 400)

    def test_route_batch_refresh_credits_and_test_are_group_scoped(self):
        class Request(object):
            _route_accounts_refresh = wb_proxy.Handler._route_accounts_refresh
            _route_accounts_credits_fetch = (
                wb_proxy.Handler._route_accounts_credits_fetch)
            _route_accounts_test_all = wb_proxy.Handler._route_accounts_test_all

            def __init__(self):
                self.status = None
                self.reply = None

            def _json_panel(self, status, payload):
                self.status, self.reply = status, payload
                return status, payload

            def _error(self, status, message, kind=""):
                self.status, self.reply = status, message
                return status, message

        seen = []
        with mock.patch.object(wb_proxy, "POOL", self.pool), \
                mock.patch.object(wb_proxy, "ACCOUNTS_DIR", self.directory), \
                mock.patch.object(wb_proxy, "account_views", lambda: []), \
                mock.patch.object(wb_proxy, "_credits_sweep",
                                  lambda targets, paced: seen.append(
                                      ("credits", [a.uid for a in targets])) or []), \
                mock.patch.object(wb_proxy, "_credits_sweep_begin",
                                  lambda: True), \
                mock.patch.object(wb_proxy, "_credits_sweep_end", lambda: None), \
                mock.patch.object(wb_proxy, "probe_account",
                                  lambda account, model: seen.append(
                                      ("test", account.uid)) or {
                                          "ok": True, "uid": account.uid,
                                          "model": model, "elapsed_ms": 1,
                                          "reply": "ok", "network": False,
                                      }), \
                mock.patch.object(wb_proxy, "probe_local_network",
                                  lambda realm: {"ok": True}), \
                mock.patch.object(wb_proxy, "CURRENT_REALM", "intl"):
            for account in self.pool.accounts:
                account.refresh = lambda: True
                account.save = lambda _directory: None

            request = Request()
            request._route_accounts_refresh({"group": "Team A"})
            self.assertEqual(request.status, 200)
            self.assertEqual([row["uid"] for row in request.reply["results"]],
                             ["g-a", "g-b"])

            request = Request()
            request._route_accounts_credits_fetch({"group": "Team A"})
            self.assertEqual(request.status, 200)
            self.assertEqual(seen[-1], ("credits", ["g-a", "g-b"]))

            request = Request()
            request._route_accounts_test_all({"realm": "intl", "group": "Team A"})
            self.assertEqual(request.status, 200)
            self.assertEqual([row["uid"] for row in request.reply["results"]],
                             ["g-b"])
            self.assertEqual(request.reply["summary"]["tested"], 1)

    def test_import_can_force_a_group(self):
        token = ("eyJhbGciOiJub25lIn0."
                 "eyJzdWIiOiJpbXBvcnRlZCIsImV4cCI6NDEwMjQ0NDgwMH0.")
        row = {"uid": "imported", "accessToken": token, "realm": "intl",
               "group": "old"}
        kwargs = wb_accounts.normalise_import_row(row)
        self.assertEqual(kwargs["group"], "old")
        kwargs = wb_accounts.normalise_import_row(dict(row, group="Team C"))
        self.assertEqual(kwargs["group"], "Team C")


class DashboardGroupWiringTests(unittest.TestCase):
    def test_group_filter_and_batch_controls_are_wired(self):
        with open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8") as fh:
            html = fh.read()
        for marker in (
            'id="accountGroupFilter"',
            "ACCOUNT_FILTERS.group",
            "editAccountGroup",
            "setCurrentGroup",
            "{enabled: enable, group: group}",
            "+ 分组",
            "id=\"importGroup\"",
            "editKeyAccountGroup_",
            "addKeyAccountsFromGroup",
            "payload.group = ACCOUNT_FILTERS.group",
        ):
            self.assertIn(marker, html)


if __name__ == "__main__":
    unittest.main()
