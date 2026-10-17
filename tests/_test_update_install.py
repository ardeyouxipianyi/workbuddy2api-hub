#!/usr/bin/env python3
"""Offline contracts for the manual update request and package boundary."""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import wb_update_request


HELPER_PATH = os.path.join(ROOT, "ops", "aliyun", "workbuddy2api-hub-updater.py")
_SPEC = importlib.util.spec_from_file_location("wb_systemd_updater", HELPER_PATH)
wb_systemd_updater = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(wb_systemd_updater)


REQUIRED_PACKAGE_FILES = sorted(wb_systemd_updater.BOOTSTRAP_FILES)
REQUIRED_PACKAGE_FILES.extend((
    "dashboard.html", "wb_proxy.py", "wb_updates.py", "wb_update_request.py",
))
REQUIRED_PACKAGE_FILES = sorted(set(REQUIRED_PACKAGE_FILES))


def _package(path, files=None, protected=None, extra=()):
    files = list(REQUIRED_PACKAGE_FILES if files is None else files)
    manifest = {
        "schema": 1,
        "name": "workbuddy2api-hub",
        "version": "1.7.13",
        "tag": "v1.7.13",
        "root": "wb-proxy",
        "files": files,
        "runtime": {"prefix": "python/", "python": "3.12", "files": 0},
        "protected": ["accounts/", "usage/"] if protected is None else protected,
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("release-manifest.json", json.dumps(manifest))
        for name in files:
            archive.writestr("wb-proxy/" + name, "pass\n")
        for name, value in extra:
            archive.writestr(name, value)


class VersionAndPathTests(unittest.TestCase):
    def test_panel_request_paths_match_the_installed_systemd_units(self):
        ops_dir = os.path.join(ROOT, "ops", "aliyun")
        with open(os.path.join(ops_dir, "workbuddy2api-hub-updater.path"),
                  "r", encoding="utf-8") as fh:
            path_unit = fh.read()
        with open(os.path.join(ops_dir, "workbuddy2api-hub-updater-app.conf"),
                  "r", encoding="utf-8") as fh:
            app_dropin = fh.read()

        self.assertEqual(wb_update_request.REQUEST_DIR,
                         "/run/workbuddy2api-hub-updater")
        self.assertIn("PathChanged=%s/request.json" % wb_update_request.REQUEST_DIR,
                      path_unit)
        self.assertIn("Environment=WB_UPDATE_REQUEST_DIR=%s" %
                      wb_update_request.REQUEST_DIR, app_dropin)
        self.assertEqual(os.path.basename(wb_update_request.PATH_UNIT_FILE),
                         "workbuddy2api-hub-updater.path")
        self.assertEqual(os.path.basename(wb_update_request.ENABLED_UNIT_LINK),
                         "workbuddy2api-hub-updater.path")

    def test_only_stable_semver_tags_are_accepted(self):
        self.assertEqual(wb_systemd_updater._version_tuple("v1.7.13"), (1, 7, 13))
        for value in ("1.7.13", "v1.7", "v1.7.13-rc1", "../v1.7.13"):
            self.assertIsNone(wb_systemd_updater._version_tuple(value), value)

    def test_package_paths_reject_traversal_and_windows_separators(self):
        for value in ("../etc/passwd", "/etc/passwd", "ops\\update.py", "a/../../b"):
            self.assertFalse(wb_systemd_updater._safe_relative(value), value)
        self.assertTrue(wb_systemd_updater._safe_relative("ops/aliyun/update.py"))

    def test_checksum_must_name_the_expected_asset(self):
        digest = "a" * 64
        self.assertEqual(wb_systemd_updater._expected_hash(
            (digest + "  workbuddy2api-hub-v1.7.13.zip\n").encode("ascii"),
            "workbuddy2api-hub-v1.7.13.zip"), digest)
        with self.assertRaises(wb_systemd_updater.UpdateError):
            wb_systemd_updater._expected_hash(
                (digest + "  another.zip\n").encode("ascii"),
                "workbuddy2api-hub-v1.7.13.zip")


class PackageManifestTests(unittest.TestCase):
    def test_valid_release_extracts_only_manifest_files(self):
        with tempfile.TemporaryDirectory(prefix="wb-update-package-") as directory:
            archive_path = os.path.join(directory, "release.zip")
            stage = os.path.join(directory, "stage")
            os.mkdir(stage)
            _package(archive_path, extra=(("wb-proxy/python/python.exe", b"ignored"),))
            extracted = wb_systemd_updater._extract_package(
                archive_path, "1.7.13", "v1.7.13", stage)
            self.assertEqual(set(extracted), set(REQUIRED_PACKAGE_FILES))
            self.assertTrue(os.path.isfile(extracted["wb_proxy.py"][0]))

    def test_manifest_must_protect_accounts_and_usage(self):
        with tempfile.TemporaryDirectory(prefix="wb-update-protected-") as directory:
            archive_path = os.path.join(directory, "release.zip")
            stage = os.path.join(directory, "stage")
            os.mkdir(stage)
            _package(archive_path, protected=["accounts/"])
            with self.assertRaises(wb_systemd_updater.UpdateError):
                wb_systemd_updater._extract_package(
                    archive_path, "1.7.13", "v1.7.13", stage)

    def test_manifest_cannot_manage_account_or_usage_files(self):
        with tempfile.TemporaryDirectory(prefix="wb-update-path-") as directory:
            archive_path = os.path.join(directory, "release.zip")
            stage = os.path.join(directory, "stage")
            os.mkdir(stage)
            _package(archive_path, files=REQUIRED_PACKAGE_FILES + ["accounts/settings.json"])
            with self.assertRaises(wb_systemd_updater.UpdateError):
                wb_systemd_updater._extract_package(
                    archive_path, "1.7.13", "v1.7.13", stage)

    def test_archive_cannot_add_unlisted_program_files(self):
        with tempfile.TemporaryDirectory(prefix="wb-update-extra-") as directory:
            archive_path = os.path.join(directory, "release.zip")
            stage = os.path.join(directory, "stage")
            os.mkdir(stage)
            _package(archive_path, extra=(("wb-proxy/unlisted.py", b"pass\n"),))
            with self.assertRaises(wb_systemd_updater.UpdateError):
                wb_systemd_updater._extract_package(
                    archive_path, "1.7.13", "v1.7.13", stage)


class RequestQueueTests(unittest.TestCase):
    def test_panel_request_writes_only_a_stable_newer_tag(self):
        with tempfile.TemporaryDirectory(prefix="wb-update-request-") as directory:
            request_path = os.path.join(directory, "request.json")
            status_path = os.path.join(directory, "status.json")
            with mock.patch.object(wb_update_request, "REQUEST_DIR", directory), \
                    mock.patch.object(wb_update_request, "REQUEST_FILE", request_path), \
                    mock.patch.object(wb_update_request, "STATUS_FILE", status_path), \
                    mock.patch.object(wb_update_request, "install_enabled", return_value=True):
                queued = wb_update_request.request_install("v1.7.13", "1.7.12")
                with open(request_path, "r", encoding="utf-8") as fh:
                    saved = json.load(fh)
                self.assertEqual(queued["state"], "queued")
                self.assertEqual(saved["tag"], "v1.7.13")
                self.assertEqual(saved["version"], "1.7.13")
                with self.assertRaises(wb_update_request.UpdateRequestError):
                    wb_update_request.request_install("v1.7.13", "1.7.12")

    def test_panel_request_rejects_disabled_or_non_newer_target(self):
        with tempfile.TemporaryDirectory(prefix="wb-update-request-refuse-") as directory:
            request_path = os.path.join(directory, "request.json")
            with mock.patch.object(wb_update_request, "REQUEST_DIR", directory), \
                    mock.patch.object(wb_update_request, "REQUEST_FILE", request_path), \
                    mock.patch.object(wb_update_request, "STATUS_FILE", os.path.join(directory, "status.json")), \
                    mock.patch.object(wb_update_request, "install_enabled", return_value=False):
                with self.assertRaises(wb_update_request.UpdateRequestError):
                    wb_update_request.request_install("v1.7.13", "1.7.12")
            with mock.patch.object(wb_update_request, "REQUEST_DIR", directory), \
                    mock.patch.object(wb_update_request, "REQUEST_FILE", request_path), \
                    mock.patch.object(wb_update_request, "STATUS_FILE", os.path.join(directory, "status.json")), \
                    mock.patch.object(wb_update_request, "install_enabled", return_value=True):
                for tag in ("v1.7.12", "v1.7.13-rc1", "not-a-tag"):
                    with self.subTest(tag=tag), self.assertRaises(wb_update_request.UpdateRequestError):
                        wb_update_request.request_install(tag, "1.7.13")
                self.assertFalse(os.path.exists(request_path))


if __name__ == "__main__":
    unittest.main()
