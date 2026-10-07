"""cockpit tools JSON import compatibility (M4 D7).

Run with: python _test_cockpit_import.py
No upstream credentials or outbound network are used.
"""
import base64
import json
import os
import sys
import tempfile
import time
import unittest

_startup_dir = tempfile.TemporaryDirectory(prefix="cockpit-import-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts


def fake_jwt(uid="uid-cockpit", issuer="https://www.workbuddy.ai"):
    def part(obj):
        raw = json.dumps(obj).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    header = part({"alg": "none", "typ": "JWT"})
    payload = part({"sub": uid, "iss": issuer,
                    "exp": int(time.time()) + 3600})
    return header + "." + payload + ".signature"


def cockpit_row(**extra):
    row = {
        "id": "c1",
        "email": "user@example.com",
        "uid": "uid-cockpit",
        "nickname": "",
        "access_token": fake_jwt(),
        "refresh_token": "refresh-token",
        "token_type": "Bearer",
        "expires_at": int((time.time() + 3600) * 1000),
        "domain": "www.workbuddy.ai",
    }
    row.update(extra)
    return row


class CockpitRowTests(unittest.TestCase):
    def test_snake_case_row_maps_to_account_kwargs(self):
        kwargs = wb_accounts.normalise_import_row(cockpit_row())
        self.assertEqual(kwargs["uid"], "uid-cockpit")
        self.assertEqual(kwargs["accessToken"], fake_jwt())
        self.assertEqual(kwargs["refreshToken"], "refresh-token")
        self.assertEqual(kwargs["realm"], "intl")
        self.assertEqual(kwargs["source"], "cockpit")
        # expires_at is milliseconds; the account stores seconds.
        self.assertLess(abs(kwargs["expiresAt"] - (time.time() + 3600)), 120)

    def test_nickname_falls_back_to_email(self):
        kwargs = wb_accounts.normalise_import_row(cockpit_row(nickname=""))
        self.assertEqual(kwargs["nickname"], "user@example.com")
        named = wb_accounts.normalise_import_row(cockpit_row(nickname="Named"))
        self.assertEqual(named["nickname"], "Named")

    def test_cn_domain_stays_cn(self):
        row = cockpit_row(domain="copilot.tencent.com",
                          access_token=fake_jwt(issuer="https://copilot.tencent.com"))
        kwargs = wb_accounts.normalise_import_row(row)
        self.assertEqual(kwargs["realm"], "cn")

    def test_missing_token_is_rejected(self):
        row = cockpit_row()
        row["access_token"] = ""
        with self.assertRaises(ValueError):
            wb_accounts.normalise_import_row(row)


class CockpitPoolImportTests(unittest.TestCase):
    def test_import_rows_adds_cockpit_accounts(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = wb_accounts.AccountPool(directory)
            report = pool.import_rows([
                cockpit_row(uid="uid-a", access_token=fake_jwt("uid-a")),
                cockpit_row(uid="uid-b", access_token=fake_jwt("uid-b")),
            ])
            self.assertEqual(sorted(report["added"]), ["uid-a", "uid-b"])
            self.assertEqual(report["invalid"], [])
            self.assertEqual(len(pool.accounts), 2)
            for account in pool.accounts:
                self.assertEqual(account.source, "cockpit")
                self.assertTrue(account.refresh_token)
                self.assertTrue(os.path.exists(os.path.join(directory,
                                                            account.uid + ".json")))

    def test_duplicate_is_skipped_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = wb_accounts.AccountPool(directory)
            pool.import_rows([cockpit_row(uid="uid-a",
                                          access_token=fake_jwt("uid-a"))])
            report = pool.import_rows([cockpit_row(uid="uid-a",
                                                   access_token=fake_jwt("uid-a"))])
            self.assertEqual(report["added"], [])
            self.assertEqual(report["skipped"][0]["reason"], "already exists")


if __name__ == "__main__":
    unittest.main()
