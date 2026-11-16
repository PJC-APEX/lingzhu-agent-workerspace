"""Build a Windows EXE and fail if its actual window runtime is unusable."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys

root = Path(__file__).resolve().parents[2]
os.chdir(root)
# Tcl on Windows can fail to initialize from non-ASCII Python installation
# paths. Stage its data in the project before PyInstaller probes availability.
stage = root / ".pytest_tmp" / "tk-build"
stage.mkdir(parents=True, exist_ok=True)
for name in ("tcl8.6", "tk8.6", "tcl8"):
    shutil.copytree(Path(sys.base_prefix) / "tcl" / name, stage / name, dirs_exist_ok=True)
os.environ["TCL_LIBRARY"] = str(stage / "tcl8.6")
os.environ["TK_LIBRARY"] = str(stage / "tk8.6")
import tkinter
tkinter.Tcl()  # Abort instead of silently building without the UI.
subprocess.run([
    sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
    "--name", "LingzhuJianyingImporter", "--distpath", "releases/windows",
    "--workpath", ".pytest_tmp/pyinstaller", "--specpath", ".pytest_tmp",
    "--paths", "apps/server", "--paths", "tools/jianying_import",
    "--collect-all", "pyJianYingDraft", "--collect-all", "pymediainfo",
    "--collect-all", "imageio", "--hidden-import", "tkinter",
    "--hidden-import", "tkinter.ttk", "--hidden-import", "tkinter.filedialog",
    "--hidden-import", "tkinter.messagebox", "tools/jianying_import/windows_app.py",
], check=True)
report = stage / "frozen-gui-report.json"
report.unlink(missing_ok=True)
env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("PYTHON", "TCL", "TK", "VIRTUAL_ENV"))}
env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
subprocess.run([str(root / "releases/windows/LingzhuJianyingImporter.exe"),
                "--gui-smoke-report", str(report)], cwd=stage, env=env, check=True, timeout=60)
assert json.loads(report.read_text(encoding="utf-8"))["ok"]
print("Frozen Tk GUI startup verified")
