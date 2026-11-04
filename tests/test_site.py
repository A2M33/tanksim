import ast
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "tanksim"


def load_build_site():
    spec = importlib.util.spec_from_file_location("build_site", ROOT / "tools" / "build_site.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_site_build_marks_the_demo_and_ships_the_modules(tmp_path):
    build_site = load_build_site()
    build_site.main(tmp_path)
    assert "<html lang=\"en\" data-demo>" in (tmp_path / "index.html").read_text(encoding="utf-8")
    for name in build_site.MODULES:
        assert (tmp_path / "py" / "tanksim" / f"{name}.py").is_file()


def test_module_lists_in_the_page_and_the_build_agree():
    js = (PACKAGE / "static" / "hmi.js").read_text(encoding="utf-8")
    listed = re.search(r"const DEMO_MODULES = \[(.*?)\]", js).group(1)
    assert re.findall(r'"(\w+)"', listed) == load_build_site().MODULES


def test_demo_modules_stay_free_of_sockets():
    for name in load_build_site().MODULES:
        tree = ast.parse((PACKAGE / f"{name}.py").read_text(encoding="utf-8"))
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree) if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert not imported & {"socket", "socketserver", "http"}, name
