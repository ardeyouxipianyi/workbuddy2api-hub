"""Only list models the pool can actually serve, and keep free accounts off PRO models.

`modelTiers[]` from `/v3/config` names the user type a model needs. The gateway
used to drop that array, so a free account could be handed a PRO-only model and
the upstream answered 11158 after the request was spent. The tier map now feeds
both the scheduler (`ready`) and the optional `/v1/models` filter. Both stay
off/unchanged until the map arrives or the operator turns the filter on.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import wb_proxy as proxy
import wb_models as MC
import wb_settings as S
import wb_tiers


def make_account(uid, realm="intl", paid=False, enabled=True):
    return wb_accounts.Account({
        "uid": uid, "realm": realm, "accessToken": "t", "enabled": enabled,
        "credits": {"is_paid_user": True} if paid else {"is_paid_user": False},
    })


class TierParsingTests(unittest.TestCase):
    def setUp(self):
        wb_tiers.reset()

    def tearDown(self):
        wb_tiers.reset()

    def test_free_and_paid_tiers_are_recorded(self):
        payload = {"modelTiers": [
            {"requiredUserType": "personal_free_user", "modelIds": ["free-model"]},
            {"requiredUserType": "standard", "modelIds": ["pro-model"]},
        ]}
        self.assertTrue(wb_tiers.ingest("intl", payload))
        self.assertFalse(wb_tiers.requires_pro("free-model", "intl"))
        self.assertTrue(wb_tiers.requires_pro("pro-model", "intl"))
        self.assertFalse(wb_tiers.requires_pro("unknown-model", "intl"))

    def test_disabled_entries_are_skipped(self):
        payload = {"modelTiers": [
            {"requiredUserType": "standard", "modelIds": ["pro-model"],
             "enabled": False},
        ]}
        wb_tiers.ingest("intl", payload)
        self.assertFalse(wb_tiers.requires_pro("pro-model", "intl"))

    def test_priority_only_badge_is_not_a_gate(self):
        payload = {"modelTiers": [
            {"requiredUserType": "standard", "modelIds": ["priority-model"],
             "badge": {"label": "订阅优先"}},
            {"requiredUserType": "standard", "modelIds": ["exclusive-model"],
             "badge": {"label": "订阅优先"}, "accessMode": "exclusive"},
        ]}
        wb_tiers.ingest("intl", payload)
        self.assertFalse(wb_tiers.requires_pro("priority-model", "intl"))
        self.assertTrue(wb_tiers.requires_pro("exclusive-model", "intl"))

    def test_data_envelope_is_read(self):
        payload = {"data": {"modelTiers": [
            {"tier": "standard", "modelIds": ["pro-model"]}]}}
        wb_tiers.ingest("cn", payload)
        self.assertTrue(wb_tiers.requires_pro("pro-model", "cn"))

    def test_missing_array_keeps_the_previous_map(self):
        wb_tiers.ingest("intl", {"modelTiers": [
            {"requiredUserType": "standard", "modelIds": ["pro-model"]}]})
        self.assertFalse(wb_tiers.ingest("intl", {"agents": []}))
        self.assertTrue(wb_tiers.requires_pro("pro-model", "intl"))

    def test_ingest_records_when_the_map_was_refreshed(self):
        self.assertIsNone(wb_tiers.ingested_at("intl"))
        wb_tiers.ingest("intl", {"modelTiers": [
            {"requiredUserType": "standard", "modelIds": ["pro-model"]}]})
        stamp = wb_tiers.ingested_at("intl")
        self.assertTrue(isinstance(stamp, float) and stamp > 0, stamp)
        # A realm nobody ingested stays unknown, and a payload without the
        # array must not move the stamp of the realm that has one.
        self.assertIsNone(wb_tiers.ingested_at("cn"))
        wb_tiers.ingest("intl", {"agents": []})
        self.assertEqual(wb_tiers.ingested_at("intl"), stamp)


class SchedulerTierTests(unittest.TestCase):
    def setUp(self):
        wb_tiers.reset()
        wb_tiers.ingest("intl", {"modelTiers": [
            {"requiredUserType": "standard", "modelIds": ["pro-model"]},
            {"requiredUserType": "personal_free_user", "modelIds": ["free-model"]},
        ]})

    def tearDown(self):
        wb_tiers.reset()

    def test_paid_flag_comes_from_the_credits_snapshot(self):
        self.assertTrue(make_account("p", paid=True).is_paid_user)
        self.assertFalse(make_account("f", paid=False).is_paid_user)
        legacy = wb_accounts.Account({"uid": "l", "realm": "intl",
                                      "accessToken": "t"})
        self.assertFalse(legacy.is_paid_user)

    def test_free_account_refuses_a_pro_model(self):
        free = make_account("f", paid=False)
        self.assertFalse(free.ready(model="pro-model"))
        self.assertTrue(free.ready(model="free-model"))
        self.assertEqual(free.unavailable_reason(model="pro-model"),
                         "该模型仅限 PRO 账号")

    def test_paid_account_serves_both(self):
        paid = make_account("p", paid=True)
        self.assertTrue(paid.ready(model="pro-model"))
        self.assertTrue(paid.ready(model="free-model"))

    def test_pool_pick_skips_free_accounts_for_a_pro_model(self):
        directory = tempfile.mkdtemp(prefix="tiers-pick-")
        pool = wb_accounts.AccountPool(directory, log=lambda _m: None)
        pool.accounts = [make_account("free"), make_account("paid", paid=True)]
        for _ in range(4):
            self.assertEqual(pool.pick(realm="intl", model="pro-model").uid, "paid")
        self.assertEqual(pool.count_ready("intl", model="pro-model"), 1)
        self.assertEqual(pool.count_ready("intl", model="free-model"), 2)


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        wb_tiers.reset()
        wb_tiers.ingest("intl", {"modelTiers": [
            {"requiredUserType": "standard", "modelIds": ["pro-model"]},
        ]})
        self.dir = tempfile.mkdtemp(prefix="models-available-")
        self.pool = wb_accounts.AccountPool(self.dir, log=lambda _m: None)

    def tearDown(self):
        wb_tiers.reset()

    def test_no_pool_advertises_everything(self):
        with mock.patch.object(proxy, "POOL", None):
            self.assertTrue(MC.models_available_for("pro-model", "intl"))

    def test_no_enabled_account_hides_everything(self):
        self.pool.accounts = [make_account("a", enabled=False)]
        with mock.patch.object(proxy, "POOL", self.pool):
            self.assertFalse(MC.models_available_for("free-model", "intl"))

    def test_pro_model_needs_a_paid_account(self):
        self.pool.accounts = [make_account("free")]
        with mock.patch.object(proxy, "POOL", self.pool):
            self.assertTrue(MC.models_available_for("free-model", "intl"))
            self.assertFalse(MC.models_available_for("pro-model", "intl"))
        self.pool.accounts.append(make_account("paid", paid=True))
        with mock.patch.object(proxy, "POOL", self.pool):
            self.assertTrue(MC.models_available_for("pro-model", "intl"))


class FakeRequest(object):
    _get_v1_models = proxy.Handler._get_v1_models
    _handle_settings_save = proxy.Handler._handle_settings_save

    def __init__(self, payload=None):
        self.key_entry = None
        self.payload = payload
        self.answering = []
        self.headers = {}
        self.path = "/v1/models"

    def _authorized(self):
        return True

    def _request_realm(self):
        return "intl"

    def _json(self, status, payload):
        self.answering.append(("json", status, payload))
        return status, payload

    def _error(self, status, message, kind=""):
        self.answering.append(("error", status, message))
        return status, message

    def _payload_or_error(self, allow_list=False):
        return self.payload

    def _body_consumed(self):
        pass

    def ids(self):
        _kind, _status, payload = self.answering[-1]
        return [m.get("id") for m in (payload.get("data") or [])]

    def models_payload(self):
        _kind, _status, payload = self.answering[-1]
        return payload


CATALOG = [("free-model", {}), ("pro-model", {})]


class ModelsEndpointTests(unittest.TestCase):
    def setUp(self):
        wb_tiers.reset()
        wb_tiers.ingest("intl", {"modelTiers": [
            {"requiredUserType": "standard", "modelIds": ["pro-model"]},
        ]})
        self.dir = tempfile.mkdtemp(prefix="models-endpoint-")
        self.pool = wb_accounts.AccountPool(self.dir, log=lambda _m: None)
        self.old_dir = proxy.ACCOUNTS_DIR
        proxy.ACCOUNTS_DIR = self.dir

    def tearDown(self):
        proxy.ACCOUNTS_DIR = self.old_dir
        wb_tiers.reset()

    def ids_for(self, enabled, accounts):
        S.set_models_only_available(self.dir, enabled)
        self.pool.accounts = accounts
        req = FakeRequest()
        with mock.patch.object(proxy, "POOL", self.pool), \
             mock.patch.object(proxy, "fetch_models", return_value=list(CATALOG)), \
             mock.patch.object(proxy, "catalog_meta", return_value=None), \
             mock.patch.object(proxy, "pinned_account", return_value=None), \
             mock.patch.object(proxy.wb_modelsdev, "refresh_async", return_value=None):
            proxy.Handler._get_v1_models(req)
        return req.ids()

    def test_off_keeps_the_full_catalogue(self):
        ids = self.ids_for(False, [make_account("free")])
        self.assertEqual(ids, ["free-model", "pro-model"])

    def test_on_hides_pro_models_without_a_paid_account(self):
        ids = self.ids_for(True, [make_account("free")])
        self.assertEqual(ids, ["free-model"])

    def test_on_lists_pro_models_when_a_paid_account_exists(self):
        ids = self.ids_for(True, [make_account("free"),
                                  make_account("paid", paid=True)])
        self.assertEqual(ids, ["free-model", "pro-model"])

    def test_setting_defaults_to_off_and_round_trips(self):
        self.assertFalse(S.models_only_available(self.dir))
        self.assertTrue(S.set_models_only_available(self.dir, True))
        self.assertTrue(S.models_only_available(self.dir))

    def test_pro_models_carry_a_one_way_catalogue_label(self):
        """#505: the label comes from the same tier map the scheduler uses.

        One-way on purpose: the gated model carries `entitlement`, everything
        else carries nothing at all - the payload never claims a model is
        available.
        """
        S.set_models_only_available(self.dir, False)
        self.pool.accounts = [make_account("free")]
        req = FakeRequest()
        with mock.patch.object(proxy, "POOL", self.pool), \
             mock.patch.object(proxy, "fetch_models", return_value=list(CATALOG)), \
             mock.patch.object(proxy, "catalog_meta", return_value=None), \
             mock.patch.object(proxy, "pinned_account", return_value=None), \
             mock.patch.object(proxy.wb_modelsdev, "refresh_async", return_value=None):
            proxy.Handler._get_v1_models(req)
        by_id = {m["id"]: m for m in req.models_payload()["data"]}
        self.assertNotIn("entitlement", by_id["free-model"])
        ent = by_id["pro-model"]["entitlement"]
        self.assertEqual(ent["source"], "catalog")
        self.assertEqual(ent["required"], "standard")
        self.assertEqual(ent["at"], wb_tiers.ingested_at("intl"))
        # The old runtime channel wrote an upstream code; the catalogue
        # source must not pretend it saw one.
        self.assertNotIn("code", ent)

    def test_no_tier_map_means_no_label(self):
        wb_tiers.reset()
        S.set_models_only_available(self.dir, False)
        self.pool.accounts = [make_account("free")]
        req = FakeRequest()
        with mock.patch.object(proxy, "POOL", self.pool), \
             mock.patch.object(proxy, "fetch_models", return_value=list(CATALOG)), \
             mock.patch.object(proxy, "catalog_meta", return_value=None), \
             mock.patch.object(proxy, "pinned_account", return_value=None), \
             mock.patch.object(proxy.wb_modelsdev, "refresh_async", return_value=None):
            proxy.Handler._get_v1_models(req)
        for m in req.models_payload()["data"]:
            self.assertNotIn("entitlement", m)


if __name__ == "__main__":
    unittest.main(verbosity=2)
