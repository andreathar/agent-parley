"""Regenerate Serena orientation memories for agent-parley.

Usage: python .serena/refresh_map.py [--check]

Writes memories/modules.md (compact module purposes and dependency edges,
stamped with the source commit) and index/symbol_index.md (full symbol list,
not a memory). With --check, exits 1 when the stamp differs from HEAD or the
tracked Python files changed since it.
"""

import ast
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERENA = ROOT / ".serena"
SOURCES = ("agent_parley", "scripts")


def git(*args):
    """Returns stdout of a git command run in the repository root."""
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True
    ).stdout.strip()


def sources():
    """Lists tracked or present Python files under the source packages."""
    return sorted(p for d in SOURCES for p in (ROOT / d).glob("*.py"))


def stamp():
    """Returns the commit stamp plus a marker for uncommitted source edits."""
    dirty = git("status", "--porcelain", "--", *SOURCES)
    return git("rev-parse", "--short", "HEAD") + ("+dirty" if dirty else "")


def check():
    """Returns True when modules.md matches the current source state."""
    target = SERENA / "memories" / "modules.md"
    if not target.exists():
        return False
    found = re.search(r"source: (\S+)", target.read_text())
    return bool(found) and found.group(1) == stamp()


def summarize(path, internal):
    """Returns (purpose, dependency names, public count) for one module."""
    tree = ast.parse(path.read_text())
    doc = (ast.get_docstring(tree) or "").splitlines()
    deps = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            parts = node.module.split(".")
            if parts[0] == "agent_parley":
                if len(parts) > 1:
                    deps.add(parts[1])
                else:
                    deps.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            deps.update(
                a.name.split(".")[1]
                for a in node.names
                if a.name.startswith("agent_parley.")
            )
    public = sum(
        isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and not n.name.startswith("_")
        for n in tree.body
    )
    return (doc[0] if doc else ""), sorted(deps & internal - {path.stem}), public


def write_modules(files):
    """Writes the compact module memory."""
    internal = {p.stem for p in files if p.parent.name == "agent_parley"}
    lines = [
        f"<!-- source: {stamp()} -->",
        "# Module map (generated)",
        "",
        "Regenerate: `python .serena/refresh_map.py`. Stale if the source",
        "stamp above differs from `git rev-parse --short HEAD`.",
        "Format: module (public symbols) purpose -> imports from agent_parley.",
        "For live symbols use find_symbol/get_symbols_overview, not this file.",
        "",
    ]
    for pkg in SOURCES:
        lines += [f"## {pkg}/", ""]
        for path in (p for p in files if p.parent.name == pkg):
            purpose, deps, public = summarize(path, internal)
            edge = f" -> {', '.join(deps)}" if deps else ""
            lines.append(f"- `{path.stem}` ({public}) {purpose}{edge}")
        lines.append("")
    (SERENA / "memories" / "modules.md").write_text("\n".join(lines))


def write_index(files):
    """Writes the full symbol index outside the memory directory."""
    out = [f"<!-- source: {stamp()} -->", "# Symbol index (generated)", ""]
    for path in files:
        tree = ast.parse(path.read_text())
        out.append(f"## {path.relative_to(ROOT)}")
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                methods = [
                    m.name
                    for m in node.body
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                ]
                out.append(f"- class {node.name}: {', '.join(methods)}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append(f"- def {node.name}")
        out.append("")
    (SERENA / "index").mkdir(exist_ok=True)
    (SERENA / "index" / "symbol_index.md").write_text("\n".join(out))


def main():
    """Runs the check or the regeneration."""
    if "--check" in sys.argv:
        ok = check()
        print("fresh" if ok else "stale")
        sys.exit(0 if ok else 1)
    files = sources()
    write_modules(files)
    write_index(files)
    print("written", stamp())


main()
