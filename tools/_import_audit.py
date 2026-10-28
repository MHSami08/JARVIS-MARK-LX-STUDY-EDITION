"""One-shot audit: find imports of first-party modules whose files no longer exist."""
import ast, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG_DIRS = ["core", "actions", "plugins", "memory", "dashboard", "tools", "config"]

def module_path(mod: str):
    """Return the Path a module's file should live at, or None if not first-party."""
    parts = mod.split(".")
    if not parts or parts[0] not in PKG_DIRS:
        return None
    return ROOT.joinpath(*parts).with_suffix(".py")

def main():
    broken = []
    skip = {".venv", "venv", "__pycache__", "site-packages", "build", "dist", ".git", "node_modules"}
    for py in ROOT.rglob("*.py"):
        if any(part in skip or part == "__pycache__" for part in py.parts[:-1]):
            continue
        if p := str(py).replace("\\", "/"):
            pass
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"), str(py))
        except SyntaxError as e:
            broken.append((str(py), e.lineno, f"SYNTAX ERROR: {e.msg}"))
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and node.level == 0:
                    # only flag when the parent module itself is gone
                    if (p := module_path(node.module)) and not p.exists() and not p.with_suffix("").is_dir():
                        broken.append((str(py), node.lineno,
                                       f"from {node.module} import ... -> missing {p}"))
                elif node.module and node.level > 0:
                    base = py.parent
                    for _ in range(node.level - 1):
                        base = base.parent
                    target = base.joinpath(*node.module.split(".")).with_suffix(".py") if node.module else None
                    if target and not target.exists():
                        broken.append((str(py), node.lineno,
                                       f"from .{node.module} import ... -> missing {target}"))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if (p := module_path(alias.name)) and not p.exists():
                        broken.append((str(py), node.lineno, f"import {alias.name} -> missing {p}"))
    # attribute lookups on single-level first-party packages (from actions.web_search import _news)
    # need the symbol checked too when the file exists
    print(f"projects scanned: {ROOT}")
    if broken:
        for f, ln, msg in broken:
            print(f"{Path(f).relative_to(ROOT)}:{ln}: {msg}")
        sys.exit(1)
    print("NO broken first-party imports found.")

if __name__ == "__main__":
    main()
