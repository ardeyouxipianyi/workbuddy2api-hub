"""Release engineering pins (M6 F1-F3).

The workflows, Dockerfile and compose file are text artifacts nothing else in
the suite executes; these assertions keep the release discipline from silently
disappearing (checksums asset, tag == source version, healthcheck, PUID/PGID).

Run with: python _test_release_engineering.py
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class VersionStringTests(unittest.TestCase):
    def test_source_version_strings_agree(self):
        src = read("wb_proxy.py")
        overview = re.search(r'"version"\s*:\s*"([^"]+)"', src)
        server = re.search(r'server_version\s*=\s*"wb-proxy/([^"]+)"', src)
        self.assertIsNotNone(overview, "overview version string not found")
        self.assertIsNotNone(server, "server header version string not found")
        self.assertEqual(overview.group(1), server.group(1))


class WorkflowTests(unittest.TestCase):
    def test_tests_workflow_asserts_tag_against_source(self):
        text = read(".github", "workflows", "tests.yml")
        self.assertIn('tags: ["v*"]', text)
        self.assertIn("version-assert", text)
        self.assertIn("refs/tags/v", text)
        self.assertIn("GITHUB_REF_NAME", text)
        self.assertIn("server_version", text)
        # The workflow's extraction must see the same two strings the source
        # test pins, otherwise a passing CI would mean nothing.
        self.assertIn('"version"', text)

    def test_release_checksums_workflow_hashes_every_asset(self):
        text = read(".github", "workflows", "release-checksums.yml")
        self.assertIn("types: [published]", text)
        self.assertIn("gh release download", text)
        self.assertIn("sha256sum *", text)
        self.assertIn("gh release upload", text)
        self.assertIn("checksums.txt", text)


class DockerTests(unittest.TestCase):
    def test_healthcheck_probes_the_health_endpoint(self):
        text = read("Dockerfile")
        self.assertIn("HEALTHCHECK", text)
        self.assertIn("/health", text)

    def test_compose_supports_puid_pgid(self):
        text = read("docker-compose.yml")
        self.assertIn("${PUID:-", text)
        self.assertIn("${PGID:-", text)


class ReadmeTests(unittest.TestCase):
    def test_readme_suite_count_matches_the_tree(self):
        names = [n for n in os.listdir(os.path.join(ROOT, "tests"))
                 if n.startswith("_test_") and n.endswith((".py", ".js"))]
        py = len([n for n in names if n.endswith(".py")])
        js = len([n for n in names if n.endswith(".js")])
        expected = "%d 个套件：%d 个 Python + %d 个 JS" % (len(names), py, js)
        self.assertIn(expected, read("README.md"))


if __name__ == "__main__":
    unittest.main()
