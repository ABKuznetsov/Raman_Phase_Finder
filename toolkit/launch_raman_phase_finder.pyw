"""Start the Windows GUI in the existing Sci interpreter."""
from __future__ import annotations

from pathlib import Path
import os
import sys
import traceback


def main() -> int:
    import ctypes

    root = Path(__file__).resolve().parents[1]
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root / 'Vibrational_Finder'))
    from vibrational_finder.core.app_paths import raman_data_root, raman_logs_root

    logs = raman_logs_root()
    log = logs / f'startup-{os.getpid()}.log'
    try:
        logs.mkdir(parents=True, exist_ok=True)
        with log.open('w', encoding='utf-8', buffering=1) as stream:
            sys.stdout = sys.stderr = stream
            data = raman_data_root()
            os.environ.update(
                IR_RAMAN_DATA_DIR=str(data),
                IR_RAMAN_PHASE_FINDER_CACHE_DIR=str(data / 'cache'),
                IR_RAMAN_LOG_DIR=str(logs),
                MPLCONFIGDIR=str(data.parent / 'matplotlib'),
                IR_RAMAN_CHECK_UPDATES='1',
                QT_OPENGL='software',
                QT_QUICK_BACKEND='software',
                QT_ANGLE_PLATFORM='warp',
                QT_QPA_PLATFORM='windows',
            )
            for name in ('PREPARED_FILE', 'SHOW_SIGNAL_FILE', 'READY_FILE'):
                os.environ.pop(f'IR_RAMAN_PHASE_FINDER_{name}', None)
            try:
                from vibrational_finder.apps.finder_gui_release import main as run_gui

                return run_gui()
            except Exception:
                traceback.print_exc()
                raise
    except Exception as error:
        ctypes.windll.user32.MessageBoxW(
            None, f'{error}\n\nStartup log: {log}', 'Raman Phase Finder', 0x10,
        )
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
