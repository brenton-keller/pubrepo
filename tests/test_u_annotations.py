"""pubrepo.py imports on Python 3.11-3.13, which evaluate annotations when a
class or def statement runs (3.14 defers them). An annotation there that
names a module-level class or alias defined later in the file raises
NameError at import, before any test or command runs.
"""
import ast

from conftest import SCRIPT_PATH


def _bound_at(tree):
    """Module-level name → line of its first binding."""
    bound = {}
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names = [node.name]
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        else:
            names = []
        for name in names:
            bound.setdefault(name, node.lineno)
    return bound


def _signature_annotations(func):
    args = func.args
    params = args.posonlyargs + args.args + args.kwonlyargs + [args.vararg, args.kwarg]
    return [p.annotation for p in params if p is not None] + [func.returns]


def _definition_time_annotations(tree):
    """(statement line, annotation) for every annotation evaluated while the
    module runs: module-level defs and variables, class-body fields, and the
    signatures of methods. Quoted annotations are strings and name nothing."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for ann in _signature_annotations(node):
                yield node.lineno, ann
        elif isinstance(node, ast.AnnAssign):
            yield node.lineno, node.annotation
        elif isinstance(node, ast.ClassDef):
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign):
                    yield node.lineno, stmt.annotation
                elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for ann in _signature_annotations(stmt):
                        yield node.lineno, ann


def forward_references(source):
    """Annotations that name a module-level binding made at or after the
    statement that evaluates them, as "line: Name (bound :line)"."""
    tree = ast.parse(source)
    bound = _bound_at(tree)
    hits = []
    for line, ann in _definition_time_annotations(tree):
        if ann is None:
            continue
        for name in ast.walk(ann):
            if isinstance(name, ast.Name) and bound.get(name.id, 0) >= line:
                hits.append(f"{name.lineno}: {name.id} (bound :{bound[name.id]})")
    return hits


def test_no_annotation_names_a_later_definition():
    assert forward_references(SCRIPT_PATH.read_text()) == []


def test_the_scan_sees_fields_signatures_and_methods():
    source = (
        "from dataclasses import dataclass\n"
        "@dataclass\n"
        "class A:\n"
        "    b: B\n"                                   # field
        "    def m(self, x: B) -> B: ...\n"           # method parameter and return
        "def f(b: B, q: 'B') -> None: ...\n"           # function parameter; quoted is fine
        "class B: ...\n")
    assert forward_references(source) == [
        "4: B (bound :7)", "5: B (bound :7)", "5: B (bound :7)", "6: B (bound :7)"]
