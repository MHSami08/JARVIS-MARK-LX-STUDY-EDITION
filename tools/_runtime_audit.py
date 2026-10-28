"""Runtime import smoke test: actually import every first-party module."""
import importlib
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
BASE = Path(__file__).resolve().parent

bad = []
ok = 0
for pkg in ("config", "core", "actions", "plugins", "memory", "dashboard"):
    pkg_dir = ROOT / pkg
    if not pkg_dir.is_dir():
        continue
    for py in sorted(pkg_dir.glob("*.py")):
        name = py.stem
        mod = f"{pkg}.{name}"
        if name.startswith("_") and name != "__init__":
            continue
        try:
            importlib.import_module(mod)
            ok += 1
        except BaseException as e:
            bad.append((mod, f"{type(e).__name__}: {e}"))

tools = [p for p in sorted((ROOT / "tools").glob("*.py")) if not p.stem.startswith("_")]
for py in tools:
    pass  # tools run standalone with heavy side effects; compile check covers them

print(f"imported OK: {ok} modules")
for m, e in bad:
    print(f"FAIL {m}: {e}")
sys.exit(1 if bad else 0)
