"""The isolated data root the suites were each building by hand.

Nine suites opened with the same six lines: a TemporaryDirectory, an atexit
cleanup, `accounts/` and `usage/` beneath it, and the two environment overrides
that point the gateway at them. That is the whole duplication this module
removes.

It is a function the suite calls at the top of its own file rather than an
import side effect, because the overrides have to land before the first module
that snapshots them is imported - and only the suite knows where that is.

It returns the TemporaryDirectory rather than the paths, so a caller keeps
writing `_TMP.name` for the suite-specific stores it puts next to `accounts/`
and `usage/`. Where a suite keeps its mutable state stays visible in the suite.
"""
import atexit
import os
import tempfile


def isolated_data_dirs(prefix):
    """An isolated ACCOUNTS_DIR + WB_PROXY_USAGE_DIR, removed at process exit.

    The two overrides are assignments, never `setdefault`: a value already in
    the environment belongs to whatever started this process, and inheriting it
    would put several suites back on one directory.
    """
    tmp = tempfile.TemporaryDirectory(prefix=prefix)
    atexit.register(tmp.cleanup)
    accounts = os.path.join(tmp.name, "accounts")
    usage = os.path.join(tmp.name, "usage")
    os.makedirs(accounts, exist_ok=True)
    os.makedirs(usage, exist_ok=True)
    os.environ["ACCOUNTS_DIR"] = accounts
    os.environ["WB_PROXY_USAGE_DIR"] = usage
    return tmp
