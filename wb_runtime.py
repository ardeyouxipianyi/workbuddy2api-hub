"""The running gateway module, for code that must call back into it.

The gateway is normally started as a script (`python[w] wb_proxy.py`), so the
live instance is `__main__`. A plain `import wb_proxy` from another module then
executes the file a second time in the same process, and that twin has its own
empty account pool, log buffer and realm-hold state: log lines vanish from the
panel, the live catalogue reads as empty, the restriction gate is always 0. All
of these were observed in production.

Modules that call into the gateway (scheduler, pricing, activity log, account
probes, protocol translators) resolve it through `gateway()` and never import
wb_proxy at module top level, which also keeps the import graph acyclic.
"""

import sys


def gateway():
    """The live wb_proxy module; imports it only when nothing is loaded yet."""
    main = sys.modules.get("__main__")
    if main is not None and getattr(main, "__wb_gateway__", False):
        return main
    module = sys.modules.get("wb_proxy")
    if module is not None:
        return module
    import wb_proxy as module
    return module
