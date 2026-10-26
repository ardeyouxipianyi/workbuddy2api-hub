"""The shipped dashboard page with its assets inlined (Python twin of
_dashboard_source.js).

dashboard.html loads its stylesheets and scripts from assets/css and
assets/js. Suites that pin markup, CSS rules or script text read the page
through dashboard_html(), which puts every asset back where its tag stands,
so one document carries all of it.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = re.compile(r'<script src="assets/js/([\w-]+\.js)"></script>')
_STYLE = re.compile(r'<link rel="stylesheet" href="assets/css/([\w-]+\.css)">')


def _asset(kind, name):
    with open(os.path.join(ROOT, "assets", kind, name), encoding="utf-8") as fh:
        return fh.read()


def dashboard_html(root=ROOT):
    with open(os.path.join(root, "dashboard.html"), encoding="utf-8") as fh:
        page = fh.read()
    page = _STYLE.sub(lambda m: "<style>\n" + _asset("css", m.group(1)) + "</style>", page)
    return _SCRIPT.sub(lambda m: "<script>\n" + _asset("js", m.group(1)) + "</script>", page)
