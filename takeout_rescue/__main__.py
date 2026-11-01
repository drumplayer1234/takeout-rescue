"""Command line entry point.

    python -m takeout_rescue scan <export_dir>
    python -m takeout_rescue apply <export_dir> <out_dir>
    python -m takeout_rescue undo <out_dir>
    python -m takeout_rescue            (no arguments: opens the window)
"""

import argparse
import sys

from . import core


def _progress(done, total, name):
    # Print about every 10% so big libraries do not flood the screen.
    step = max(1, total // 10)
    if done == total or done % step == 0:
        print("  %d / %d  %s" % (done, total, name))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        from . import gui
        gui.main()
        return 0

    parser = argparse.ArgumentParser(prog="takeout_rescue",
                                     description="Rescue photos from a Google Takeout export.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("scan", help="show what is in the export (changes nothing)")
    p.add_argument("export_dir")
    p = sub.add_parser("apply", help="copy photos into a clean OUTPUT/YYYY/MM library")
    p.add_argument("export_dir")
    p.add_argument("out_dir")
    p = sub.add_parser("undo", help="delete the files a previous apply created")
    p.add_argument("out_dir")
    args = parser.parse_args(argv)

    try:
        if args.command == "scan":
            plan = core.scan(args.export_dir)
            print("\n".join(plan.summary_lines()))
        elif args.command == "apply":
            print("Scanning...")
            plan = core.scan(args.export_dir)
            print("\n".join(plan.summary_lines()))
            print("Copying...")
            counts = core.apply(plan, args.out_dir, progress_cb=_progress)
            print("Done. Copied %(copied)d files, %(exif_written)d with metadata written, "
                  "%(errors)d errors." % counts)
            print("Report: %s" % core.REPORT_NAME)
            return 1 if counts["errors"] else 0
        elif args.command == "undo":
            n = core.undo(args.out_dir)
            print("Removed %d files." % n)
    except core.TakeoutError as e:
        print("Error: %s" % e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
