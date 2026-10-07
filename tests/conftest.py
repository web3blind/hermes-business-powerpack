import importlib.util
import atexit
import os
from pathlib import Path
import sys
import tempfile

# Collection itself is isolated, before any Hermes imports.
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
_test_home = tempfile.TemporaryDirectory(prefix="business-powerpack-tests-")
atexit.register(_test_home.cleanup)
os.environ["HERMES_HOME"] = _test_home.name
spec = importlib.util.spec_from_file_location("business_powerpack", ROOT / "__init__.py",
                                            submodule_search_locations=[str(ROOT)])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
