"""Copy the standalone Raman application without datasets or developer artifacts."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil


RUNTIME_FILES = (
    "README.md", "LICENSE", "PORTABLE_README.txt", "pyproject.toml", "reference_template.json",
    "icon.png", "icon.ico", "install_macos.command", "update_macos.command", "update_from_github.bat",
    "run_finder.bat", "run_finder.command", "run_finder.sh", "run_finder_cli.bat",
    "setup_env.bat", "setup_env.command", "setup_env.sh", "launch_ir_raman_phase_finder.bat",
    "Vibrational_Finder/app.json", "toolkit/fast_start.py", "toolkit/manifest.json",
    "toolkit/setup_sci_env.bat", "toolkit/setup_sci_env.command",
    "toolkit/launch_ir_raman_phase_finder_preview.command", "toolkit/launch_ir_raman_phase_finder_preview.ps1",
    "toolkit/launch_ir_raman_phase_finder_preview_macos.py",
)
SOURCE_FILES = (
    ".gitignore", "toolkit/updates/ir_raman_phase_finder.json",
    "scripts/build_macos_pkg.command", "scripts/sanitize_macos_pkg.command",
    "scripts/stage_raman_release.py",
)
LEGACY_MODULES = {"apps/ftir_cli.py", "apps/finder_gui.py", "services/cif_structure_source.py", "services/jarvis_source.py"}
SUBPACKAGES = {"apps", "core", "io", "layers", "plot_export", "services", "ui"}


def stage(root: Path, target: Path, *, source: bool = False) -> None:
    if target.is_symlink() or (target.exists() and any(target.iterdir())):
        raise ValueError(f"Staging directory must be empty: {target}")
    target.mkdir(parents=True, exist_ok=True)
    paths = {Path(name) for name in (*RUNTIME_FILES, *(SOURCE_FILES if source else ()))}
    base = Path("Vibrational_Finder/vibrational_finder")
    folders = [base, *(base / subpackage for subpackage in SUBPACKAGES)]
    paths.update(p.relative_to(root) for p in (root / base / "ui/assets").glob("*.svg"))
    for folder in folders:
        paths.update(
            p.relative_to(root) for p in (root / folder).glob("*.py")
            if p.relative_to(root / base).as_posix() not in LEGACY_MODULES
        )
    for relative in sorted(paths):
        original = root / relative
        if not original.is_file() or original.is_symlink() or any(p.is_symlink() for p in original.parents if p != root):
            continue
        output = target / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--source", action="store_true", help="Also include source distribution build tools.")
    args = parser.parse_args()
    stage(args.root.resolve(), args.target, source=args.source)
