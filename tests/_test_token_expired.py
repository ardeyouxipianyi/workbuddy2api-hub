"""Only credential-shaped 401s park an account (#440 / #435).

A bare 401 from an intermediate reverse proxy is an HTML error page, not
evidence that the token is dead. Parking on it would remove a healthy
credential from rotation until a manual re-login.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts


def make_account():
    return wb_accounts.Account({"uid": "uid-401", "accessToken": "token"})


class AuthFailureTextTests(unittest.TestCase):
    def test_proxy_html_is_not_a_credential_failure(self):
        self.assertFalse(wb_accounts._auth_failure_text(
            "HTTP 401: <html><head><title>401 Authorization Required</title></head></html>"))

    def test_plain_401_and_authorization_are(self):
        for text in ("HTTP 401", "401 Unauthorized", "Authorization failed",
                     "invalid_token", "token expired"):
            with self.subTest(text=text):
                self.assertTrue(wb_accounts._auth_failure_text(text))

    def test_generic_token_wording_is_not_enough(self):
        self.assertFalse(wb_accounts._auth_failure_text("token count exceeded"))
        self.assertFalse(wb_accounts._auth_failure_text("refresh timeout"))


class NoteErrorTests(unittest.TestCase):
    def test_http_401_marks_the_account(self):
        account = make_account()
        account.note_error("HTTP 401")
        self.assertTrue(account.token_expired)
        self.assertFalse(account.ready())

    def test_openresty_401_keeps_the_account_usable(self):
        account = make_account()
        account.note_error("HTTP 401: <html><title>401 Authorization Required</title></html>")
        self.assertFalse(account.token_expired)
        account.cooldown_until = 0
        self.assertTrue(account.ready())

    def test_generic_token_error_does_not_park(self):
        account = make_account()
        account.note_error("token count exceeded")
        self.assertFalse(account.token_expired)
        account.cooldown_until = 0
        self.assertTrue(account.ready())


class NoteSuccessTests(unittest.TestCase):
    """A served request is proof the token is alive, so it lifts the park.

    Regression: the panel's "test" returned a real model reply while the badge
    still read 「凭证失效」 and the account stayed out of rotation, because
    `note_success()` cleared every other penalty but not `token_expired`.
    """

    def test_success_clears_the_credential_park(self):
        account = make_account()
        account.note_error("HTTP 401")
        self.assertTrue(account.token_expired)
        self.assertFalse(account.ready())

        account.note_success()

        self.assertFalse(account.token_expired)
        self.assertTrue(account.ready())
        view = account.public()
        self.assertFalse(view["tokenExpired"])
        self.assertFalse(view["needRelogin"])
        self.assertNotEqual(view["expiresIn"], "expired")

    def test_success_clears_the_park_for_one_model_only_call(self):
        # The panel test and the real dispatch path both pass a model name.
        account = make_account()
        account.note_error("HTTP 401")
        account.note_success(model="deepseek-v4.1-flash")
        self.assertFalse(account.token_expired)
        self.assertTrue(account.ready(model="deepseek-v4.1-flash"))

    def test_success_cannot_resurrect_a_tokenless_account(self):
        # No credential means no credential: clearing the flag must not make an
        # account without an access token dispatchable, and the badge stays red.
        account = wb_accounts.Account({"uid": "uid-no-token"})
        account.token_expired = True
        account.note_success()
        self.assertFalse(account.ready())
        self.assertTrue(account.public()["tokenExpired"])


if __name__ == "__main__":
    unittest.main()
