"""Build the GitHub Pages demo: the HMI plus the Python sources it runs
in the browser with Pyodide.

    python tools/build_site.py site
"""

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "tanksim"
MODULES = ["__init__", "process", "plc", "sim", "commands"]  # keep in sync with hmi.js


def main(out: Path) -> None:
    # Empty the folder rather than delete it, so a preview server running
    # inside it (python -m http.server) survives a rebuild.
    out.mkdir(parents=True, exist_ok=True)
    for child in out.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    shutil.copytree(PACKAGE / "static", out, dirs_exist_ok=True)
    index = out / "index.html"
    html = index.read_text(encoding="utf-8")
    index.write_text(html.replace('<html lang="en">', '<html lang="en" data-demo>', 1), encoding="utf-8")
    py = out / "py" / "tanksim"
    py.mkdir(parents=True)
    for name in MODULES:
        shutil.copy(PACKAGE / f"{name}.py", py)
    (out / ".nojekyll").touch()
    print(f"Built {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "site"))
