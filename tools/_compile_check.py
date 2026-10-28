"""py_compile sweep across every remaining module (verification protocol step 5)."""
import glob
import py_compile
import sys

skip_dirs = ("runtime", "workspace", "data", "__pycache__")
bad = []
ok = 0
for f in glob.glob("**/*.py", recursive=True):
    if f.startswith(skip_dirs) or "__pycache__" in f:
        continue
    try:
        py_compile.compile(f, doraise=True)
        ok += 1
    except Exception as e:
        bad.append((f, str(e).splitlines()[-1]))

print(f"compiled OK: {ok}")
for f, e in bad:
    print(f"FAIL {f}: {e}")
sys.exit(1 if bad else 0)
