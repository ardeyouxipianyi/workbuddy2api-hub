"""The public health flag follows the key check used by /v1 requests."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy as proxy
import wb_settings as settings


class RequestWithoutKey(object):
    _key_ok = proxy.Handler._key_ok

    def _panel_ok(self):
        return False

    def _supplied_key(self):
        return ""

    def _json(self, status, payload):
        return status, payload


class HealthAuthTests(unittest.TestCase):
    def test_health_follows_current_settings(self):
        with tempfile.TemporaryDirectory(prefix="health-auth-") as directory:
            with mock.patch.multiple(proxy, ACCOUNTS_DIR=directory, API_KEY=None, POOL=None):
                request = RequestWithoutKey()

                def check(required):
                    status, health = proxy.Handler._get_health(request)
                    self.assertEqual(status, 200)
                    self.assertIs(health["api_key_required"], required)
                    self.assertIs(request._key_ok(), not required)

                check(False)
                key = {"name": "panel key", "key": "panel-secret", "enabled": True}
                settings.set_api_keys(directory, [key])
                check(True)

                settings.set_api_keys(directory, [dict(key, enabled=False)])
                check(False)

                settings.set_api_keys(directory, [key])
                settings.set_auth_disabled(directory, True)
                check(False)

                settings.set_auth_disabled(directory, False)
                settings.set_api_keys(directory, [])
                proxy.API_KEY = "launcher-secret"
                check(True)


if __name__ == "__main__":
    unittest.main()
