"""scripts/plot_results.py を引数なしで流すと、性能評価の文書が参照する図がすべて描き直されることを検査する。

作図には matplotlib が要るので、ここでは実行せずに構文木だけを見る（PR #12 のレビュー指摘:
新しく足した作図の関数が __main__ から呼ばれておらず、文書の手順では図を作り直せなかった）。
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "plot_results.py"
DOC = ROOT / "docs" / "performance-evaluation.md"


def _tree() -> ast.Module:
    return ast.parse(SCRIPT.read_text(encoding="utf-8"))


def _main_block(tree: ast.Module) -> ast.If:
    for node in tree.body:
        if isinstance(node, ast.If) and "__main__" in ast.unparse(node.test):
            return node
    raise AssertionError("__main__ の節が無い")


def _called_names(node: ast.AST) -> set[str]:
    return {n.func.id for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


def _figures_by_function(tree: ast.Module) -> dict[str, set[str]]:
    """関数名 → その関数が save() に渡す図の名前。"""
    out = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            names = set()
            for call in ast.walk(node):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "save"
                        and len(call.args) >= 2 and isinstance(call.args[1], ast.Constant)):
                    names.add(call.args[1].value)
            out[node.name] = names
    return out


def test_every_plot_function_is_called_from_main():
    tree = _tree()
    defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("plot_")}
    missing = defined - _called_names(_main_block(tree))
    assert not missing, f"__main__ から呼ばれていない作図の関数: {sorted(missing)}"


def test_every_figure_in_doc_is_drawn_from_main():
    tree = _tree()
    called = _called_names(_main_block(tree))
    drawn = set().union(*(figs for name, figs in _figures_by_function(tree).items() if name in called))
    referenced = set(re.findall(r"results/figures/([\w.-]+)\.(?:svg|png)", DOC.read_text(encoding="utf-8")))
    assert referenced, "文書から図の参照を読み取れない"
    missing = referenced - drawn
    assert not missing, f"文書が参照しているのに __main__ から描かれない図: {sorted(missing)}"
