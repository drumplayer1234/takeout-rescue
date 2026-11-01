"""PyInstaller entry point for the Windows exe.

PyInstaller cannot run takeout_rescue/__main__.py directly (its relative
imports break), so the exe starts here instead.

Hidden check used to test the frozen exe without opening a window:
    TakeoutRescue.exe --selftest <export_dir> <out_dir> <log_file>
Runs scan + apply, writes the summary to <log_file>, exits non-zero on errors.
"""

import sys

from takeout_rescue import gui
from takeout_rescue.__main__ import main as cli_main


def main():
    if len(sys.argv) == 5 and sys.argv[1] == "--selftest":
        export_dir, out_dir, log_file = sys.argv[2:]
        # The exe is windowed and has no console, so send output to a file.
        with open(log_file, "w", encoding="utf-8") as log:
            sys.stdout = sys.stderr = log
            code = cli_main(["apply", export_dir, out_dir])
        return code
    gui.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
