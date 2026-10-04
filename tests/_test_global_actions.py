"""International account activation / trial claim (B7).

Run with: python _test_global_actions.py
No upstream credentials or outbound network are used.
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

_startup_dir = tempfile.TemporaryDirectory(prefix="global-actions-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import wb_global
import wb_proxy

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


class FakeResponse(object):
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def make_account(realm="intl", uid="uid-intl"):
    return wb_accounts.Account({"uid": uid, "accessToken": "tok", "realm": realm})


class CountryTests(unittest.TestCase):
    def test_cn_accounts_are_refused(self):
        with self.assertRaises(RuntimeError):
            wb_global.fetch_countries(make_account("cn"))
        with self.assertRaises(RuntimeError):
            wb_global.claim_trial(make_account("cn"))

    def test_fetch_countries_unwraps_the_double_envelope_and_filters(self):
        captured = {}

        def fake_urlopen(req, timeout=30, proxy=""):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return FakeResponse({
                "code": 0, "msg": "",
                "data": json.dumps({"data": {"list": [
                    {"EnName": "United States", "Name": "US", "IOS2": "US",
                     "IOS3": "USA", "Code": "840"},
                    {"EnName": "Hong Kong", "Name": "HK", "IOS2": "HK",
                     "IOS3": "HKG", "Code": "344"},
                    {"EnName": "Singapore", "Name": "SG", "IOS2": "SG",
                     "IOS3": "SGP", "Code": "702"},
                ]}}),
            })

        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake_urlopen
        try:
            countries = wb_global.fetch_countries(make_account())
        finally:
            wb_accounts.urlopen = old
        self.assertEqual([c["ios2"] for c in countries], ["HK", "SG"])
        self.assertEqual(countries[0]["en_name"], "Hong Kong")
        self.assertEqual(captured["url"],
                         "https://www.workbuddy.ai/billing/area/get-country-code")
        self.assertEqual(captured["body"], {"filterForbidden": 1})

    def test_fetch_countries_surfaces_business_errors(self):
        def fake_urlopen(req, timeout=30, proxy=""):
            return FakeResponse({"code": 500, "msg": "nope", "data": None})

        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake_urlopen
        try:
            with self.assertRaises(RuntimeError):
                wb_global.fetch_countries(make_account())
        finally:
            wb_accounts.urlopen = old


class RegisterTests(unittest.TestCase):
    def test_register_status_variants(self):
        def status_for(payload):
            def fake_urlopen(req, timeout=30, proxy=""):
                return FakeResponse(payload)
            return fake_urlopen

        cases = [
            ({"code": 200, "msg": "ok", "data": None}, (True, False)),
            ({"code": 500, "msg": "region required", "data": None}, (False, True)),
            ({"code": 1, "msg": "region required", "data": None}, (False, True)),
            ({"code": 1, "msg": "something else", "data": None}, (False, False)),
        ]
        old = wb_accounts.urlopen
        try:
            for payload, expected in cases:
                wb_accounts.urlopen = status_for(payload)
                activated, needs_region, _msg = wb_global.register_status(make_account())
                self.assertEqual((activated, needs_region), expected, payload)
        finally:
            wb_accounts.urlopen = old

    def test_submit_region_body(self):
        captured = {}

        def fake_urlopen(req, timeout=30, proxy=""):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return FakeResponse({"code": 0, "msg": "", "data": None})

        country = {"code": "344", "en_name": "Hong Kong", "ios2": "HK"}
        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake_urlopen
        try:
            wb_global.submit_region(make_account(), country)
        finally:
            wb_accounts.urlopen = old
        self.assertEqual(captured["url"],
                         "https://www.workbuddy.ai/console/login/account")
        self.assertEqual(captured["body"], {"attributes": {
            "countryCode": ["344"], "countryFullName": ["Hong Kong"],
            "countryName": ["HK"]}})

    def test_complete_registration_flow(self):
        calls = {"status": 0}

        def fake_status(account, timeout=20):
            calls["status"] += 1
            if calls["status"] == 1:
                return False, True, "region required"
            return True, False, "register success"

        submitted = []

        def fake_submit(account, country, timeout=20):
            submitted.append(country["ios2"])
            return True

        with mock.patch.object(wb_global, "register_status", side_effect=fake_status), \
                mock.patch.object(wb_global, "fetch_countries",
                                  return_value=[{"ios2": "HK", "code": "344",
                                                 "en_name": "Hong Kong"}]), \
                mock.patch.object(wb_global, "submit_region", side_effect=fake_submit):
            activated, detail = wb_global.complete_registration(make_account())
        self.assertTrue(activated)
        self.assertIn("HK", detail)
        self.assertEqual(submitted, ["HK"])

    def test_already_activated_skips_region(self):
        with mock.patch.object(wb_global, "register_status",
                               return_value=(True, False, "register success")), \
                mock.patch.object(wb_global, "fetch_countries") as fetch:
            activated, detail = wb_global.complete_registration(make_account())
        self.assertTrue(activated)
        self.assertEqual(detail, "already activated")
        fetch.assert_not_called()


class TrialTests(unittest.TestCase):
    def test_claim_success(self):
        def fake_urlopen(req, timeout=30, proxy=""):
            return FakeResponse({"code": 0, "msg": "", "data": None})

        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake_urlopen
        try:
            self.assertTrue(wb_global.claim_trial(make_account()))
        finally:
            wb_accounts.urlopen = old

    def test_already_claimed_is_idempotent(self):
        def fake_urlopen(req, timeout=30, proxy=""):
            return FakeResponse({"code": 14051, "msg": "already", "data": None})

        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake_urlopen
        try:
            self.assertFalse(wb_global.claim_trial(make_account()))
        finally:
            wb_accounts.urlopen = old

    def test_http_error_body_with_14051_is_idempotent(self):
        def fake_urlopen(req, timeout=30, proxy=""):
            raise urllib.error.HTTPError(
                "https://upstream.invalid", 400, "bad", {},
                io.BytesIO(json.dumps({"code": 14051, "msg": "claimed"}).encode()))

        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake_urlopen
        try:
            self.assertFalse(wb_global.claim_trial(make_account()))
        finally:
            wb_accounts.urlopen = old

    def test_other_business_error_raises(self):
        def fake_urlopen(req, timeout=30, proxy=""):
            return FakeResponse({"code": 500, "msg": "boom", "data": None})

        old = wb_accounts.urlopen
        wb_accounts.urlopen = fake_urlopen
        try:
            with self.assertRaises(RuntimeError):
                wb_global.claim_trial(make_account())
        finally:
            wb_accounts.urlopen = old


class GlobalRouteTests(unittest.TestCase):
    def make_handler(self):
        class Handler(wb_proxy.Handler):
            def __init__(self):
                self.captured = None

            def _json(self, code, obj):
                self.captured = (code, obj)

            def _error(self, code, message, err_type="server_error"):
                self.captured = (code, {"error": message})

        return Handler()

    def make_pool(self, account):
        class Pool(object):
            accounts = [account]

            def get(self, uid):
                return account if uid == account.uid else None

            def list_public(self, realm=None):
                return [account.public()]

        return Pool()

    def test_global_register_route(self):
        with tempfile.TemporaryDirectory() as directory:
            account = wb_accounts.Account(
                {"uid": "uid-1", "accessToken": "tok", "realm": "intl"},
                os.path.join(directory, "uid-1.json"))
            old_pool = wb_proxy.POOL
            wb_proxy.POOL = self.make_pool(account)
            try:
                with mock.patch.object(wb_global, "complete_registration",
                                       return_value=(True, "activated with region HK")):
                    handler = self.make_handler()
                    handler._route_accounts_global_register({"uid": "uid-1"})
            finally:
                wb_proxy.POOL = old_pool
            code, obj = handler.captured
            self.assertEqual(code, 200)
            self.assertTrue(obj["ok"])
            self.assertTrue(obj["activated"])

    def test_trial_route_reports_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            account = wb_accounts.Account(
                {"uid": "uid-1", "accessToken": "tok", "realm": "intl"},
                os.path.join(directory, "uid-1.json"))
            old_pool = wb_proxy.POOL
            wb_proxy.POOL = self.make_pool(account)
            try:
                with mock.patch.object(wb_global, "claim_trial", return_value=False):
                    handler = self.make_handler()
                    handler._route_accounts_trial({"uid": "uid-1"})
            finally:
                wb_proxy.POOL = old_pool
            code, obj = handler.captured
            self.assertEqual(code, 200)
            self.assertFalse(obj["claimed"])
            self.assertEqual(obj["detail"], "already claimed")

    def test_unknown_uid_is_a_404(self):
        with tempfile.TemporaryDirectory() as directory:
            account = wb_accounts.Account(
                {"uid": "uid-1", "accessToken": "tok", "realm": "intl"},
                os.path.join(directory, "uid-1.json"))
            old_pool = wb_proxy.POOL
            wb_proxy.POOL = self.make_pool(account)
            try:
                handler = self.make_handler()
                handler._route_accounts_trial({"uid": "nope"})
            finally:
                wb_proxy.POOL = old_pool
            code, _obj = handler.captured
            self.assertEqual(code, 404)


class TrialScriptTests(unittest.TestCase):
    def test_list_mode(self):
        spec = importlib.util.spec_from_file_location(
            "trial_script", os.path.join(ROOT, "scripts", "trial.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            account = wb_accounts.Account(
                {"uid": "uid-intl", "accessToken": "tok", "realm": "intl",
                 "nickname": "Nick"})
            account.save(directory)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = module.main(["--accounts-dir", directory, "--list"])
        self.assertEqual(code, 0)
        self.assertIn("uid-intl", buffer.getvalue())
        self.assertIn("Nick", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
