"""Keep every test away from the user's real Momento.

Imported first by each test module (and by tests/__init__.py). Points the XDG
runtime, cache, config and state dirs at a throwaway directory *before*
momento.config is imported, so no test can reach the live daemon's socket,
its replay buffer, the user's settings or the saved portal permission.
"""

import atexit
import os
import shutil
import tempfile

if not os.environ.get("MOMENTO_TEST_SANDBOX"):
    _root = tempfile.mkdtemp(prefix="momento-tests-", dir="/tmp")
    for _var, _sub in (("XDG_RUNTIME_DIR", "run"), ("XDG_CACHE_HOME", "cache"),
                       ("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state")):
        os.makedirs(os.path.join(_root, _sub), mode=0o700)
        os.environ[_var] = os.path.join(_root, _sub)
    os.environ["MOMENTO_TEST_SANDBOX"] = _root
    atexit.register(shutil.rmtree, _root, True)

import sys  # noqa: E402

if "momento.config" in sys.modules:
    raise RuntimeError("momento.config was imported before the test sandbox; import tests._sandbox first")
