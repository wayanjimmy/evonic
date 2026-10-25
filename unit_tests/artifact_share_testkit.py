"""
Shared helpers for the artifact-sharing tests.

The plugin directory name contains a hyphen (``plugins/artifact-share``), so it
cannot be imported as a dotted package.  These helpers load the plugin's
``db.py``, ``routes.py`` and the ``share_artifact`` tool module the same way the
platform does — by file path, under stable module names — and point the plugin
store at a temporary database.
"""

import importlib.util
import os
import sys
import types

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(REPO_ROOT, "plugins", "artifact-share")
TOOL_PATH = os.path.join(PLUGIN_DIR, "backend", "tools", "share_artifact.py")

#: Stable module name the ``share_artifact`` tool expects for the plugin store.
SHARE_DB_MODULE = "artifact_share_plugin_db"

#: Synthetic package used to load ``routes.py`` so its ``from .db import ...``
#: relative import resolves.
ROUTES_PACKAGE = "artifact_share_under_test"


def load_share_db(db_path):
    """Load ``plugins/artifact-share/db.py`` and point it at *db_path*."""
    module = sys.modules.get(SHARE_DB_MODULE)
    if module is None:
        spec = importlib.util.spec_from_file_location(
            SHARE_DB_MODULE, os.path.join(PLUGIN_DIR, "db.py")
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[SHARE_DB_MODULE] = module
        spec.loader.exec_module(module)
    module.db.db_path = str(db_path)
    module.db._init_tables()
    return module


def load_tool_module(db_path):
    """Load the ``share_artifact`` tool with its plugin store redirected."""
    load_share_db(db_path)
    name = "artifact_share_tool_under_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_routes_module(db_path):
    """Load ``routes.py`` as ``<pkg>.routes`` and swap in a temp store."""
    if ROUTES_PACKAGE not in sys.modules:
        pkg = types.ModuleType(ROUTES_PACKAGE)
        pkg.__path__ = [PLUGIN_DIR]
        pkg.__package__ = ROUTES_PACKAGE
        sys.modules[ROUTES_PACKAGE] = pkg

    module_name = f"{ROUTES_PACKAGE}.routes"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(
        module_name, os.path.join(PLUGIN_DIR, "routes.py")
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = ROUTES_PACKAGE
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    # Redirect the module-level store to the temporary database.  ``routes.py``
    # binds the singleton (``from .db import db``) rather than the class, so we
    # build a fresh instance from the loaded ``db`` module instead.
    db_module = load_share_db(db_path)
    store = db_module.ArtifactShareDB(str(db_path))
    store._init_tables()
    module.share_db = store
    return module
