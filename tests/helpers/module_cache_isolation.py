"""Restore host/plugin imports after tests that install process-wide stubs."""

import sys
from contextlib import contextmanager


def _is_test_dependency(name):
    return name in {"main", "config", "astrbot", "astrmai"} or name.startswith(("astrbot.", "astrmai."))


@contextmanager
def isolated_module_cache():
    modules = {name: module for name, module in sys.modules.copy().items() if _is_test_dependency(name)}
    # reload() mutates an existing module, and import attaches children to parents.
    attributes = {name: dict(vars(module)) for name, module in modules.items() if module is not None}
    try:
        yield
    finally:
        for name in tuple(sys.modules):
            if _is_test_dependency(name) and name not in modules:
                sys.modules.pop(name, None)
        sys.modules.update(modules)
        for name, saved in attributes.items():
            namespace = vars(modules[name])
            namespace.clear()
            namespace.update(saved)
