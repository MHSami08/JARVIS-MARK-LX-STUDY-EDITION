"""Verify symbols named in `from <first-party module> import X` actually exist."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG_DIRS = {"core", "actions", "plugins", "memory", "dashboard", "tools", "config"}
skip = {".venv", "venv", "__pycache__", "build", "dist", ".git", "node_modules"}


def top_names(path: Path) -> set:
    names = set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), str(path))
    except SyntaxError:
        return names
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
    return names


bad = 0
for py in ROOT.rglob("*.py"):
    if any(part in skip for part in py.parts[:-1]):
        continue
    try:
        tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"), str(py))
    except SyntaxError:
        continue
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ImportFrom) and node.module and node.level == 0):
            continue
        parts = node.module.split(".")
        if parts[0] not in PKG_DIRS:
            continue
        target = ROOT.joinpath(*parts)
        mod_file = target.with_suffix(".py")
        init_file = target / "__init__.py"
        if parts[-1].startswith("_") and not mod_file.exists():
            continue
        if mod_file.exists():
            names = top_names(mod_file)
        elif init_file.exists():
            names = set()
            for p in target.glob("*.py"):
                names |= top_names(p)
        else:
            continue
        for alias in node.names:
            if alias.name == "*":
                continue
            if "@" in alias.name:  # explicit module import: "utils.foo"
                continue
            if alias.name not in names:
                print(f"{py.relative_to(ROOT)}:{node.lineno}: "
                      f"'{alias.name}' not found in {node.module}")
                bad += 1
print("DONE", "— issues above" if bad else "— all first-party symbols resolve")
