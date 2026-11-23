"""Install the declared dependencies with visible, resumable package steps."""
from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from tempfile import TemporaryDirectory


def requirements(root: Path, formats: bool = True) -> list[str]:
    project = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))['project']
    result = list(project['dependencies'])
    if formats:
        result.extend(project.get('optional-dependencies', {}).get('formats', []))
    return result


def check_runtime(*, formats: bool = False) -> None:
    import numpy as np
    from scipy.signal import find_peaks

    modules = ['numpy', 'scipy', 'pybaselines', 'pyqtgraph', 'PySide6.QtWidgets',
               'certifi', 'pyreadr', 'rdata', 'ijson']
    if formats:
        modules.extend(['ramanchada2.spectrum', 'pyarrow.parquet'])
    for name in modules:
        importlib.import_module(name)
        print(f'OK: {name}', flush=True)
    assert find_peaks(np.array([0., 1., 0.]))[0].tolist() == [1]
    print('Runtime self-test passed.', flush=True)


def register_application(root: Path, pip: list[str]) -> int:
    # Setuptools writes build and egg-info files beside the source.
    with TemporaryDirectory(prefix="raman-build-") as folder:
        stage = Path(folder)
        shutil.copy2(root / "pyproject.toml", stage)
        shutil.copytree(
            root / "Vibrational_Finder" / "vibrational_finder",
            stage / "Vibrational_Finder" / "vibrational_finder",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
        return subprocess.run(
            [*pip, "install", "--no-deps", "--no-build-isolation", "--force-reinstall", str(stage)],
            check=False,
        ).returncode


def install(root: Path, log: Path | None = None) -> int:
    packages = requirements(root)
    pip_version = tuple(int(part) for part in importlib.metadata.version('pip').split('.')[:2])
    resume_options = ['--resume-retries', '30'] if pip_version >= (25, 2) else []
    pip = [sys.executable, '-m', 'pip']
    if log is not None:
        pip.extend(['--log', str(log)])
    pip.extend(['--disable-pip-version-check', '--no-input'])
    for index, package in enumerate(packages, 1):
        print(f'[{index}/{len(packages)}] Installing package: {package}', flush=True)
        for attempt in range(1, 3):
            command = [*pip, 'install', '--timeout', '90',
                       '--retries', '3', *resume_options,
                       '--prefer-binary', '--progress-bar', 'on', package]
            result = subprocess.run(command, check=False)
            if result.returncode == 0:
                break
            if attempt == 2:
                print(f'ERROR: could not install {package}. See the error above.', flush=True)
                return result.returncode
            print(f'Retrying {package}: attempt {attempt + 1}/2 in 5 seconds', flush=True)
            time.sleep(5)
    print('Registering Raman...', flush=True)
    result = register_application(root, pip)
    if result:
        return result
    print('Testing installed scientific packages...', flush=True)
    check_runtime(formats=True)
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--log', type=Path)
    args = parser.parse_args()
    if args.list:
        print('\n'.join(requirements(args.root)))
    elif args.check:
        check_runtime()
    else:
        raise SystemExit(install(args.root, args.log))
