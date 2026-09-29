"""run_walk — orchestrates a partitioned UI walk + contact sheets.

Why partitioned: driving hundreds of UI actions in one process eventually
trips native fail-fasts on some stacks (Python 3.14 + PyQt6 + BLAS on this
class of machine dies with 0xC0000409 after minutes of widget churn). Fresh
process per phase group + one retry per phase keeps every phase completable;
numbering offsets merge all phases into one gallery.

Usage:
  python run_walk.py pyqt --app-config app.json --out C:/Temp/walk
  python run_walk.py uia --launch "notepad.exe" --out C:/Temp/walk

Ends by building contact sheets (unless --no-sheets) and printing the
next-step hint for the judge handoff (references/judge-handoff.md).
"""
import argparse
import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def run_pyqt(args):
    plan = [("boot,steps,menus", "0"), ("tools", "200")]
    for only, seq in plan:
        for attempt in (1, 2):
            proc = subprocess.run(
                [sys.executable, "-u", os.path.join(HERE, "driver_pyqt.py"),
                 "--app-config", args.app_config, "--out", args.out,
                 "--only", only, "--seq", seq],
                capture_output=True, text=True, timeout=args.timeout)
            tail = [l for l in proc.stdout.splitlines()
                    if l.startswith(("PASS", "FAIL"))]
            print(f"[{only}] try{attempt} exit={proc.returncode} "
                  f"steps={len(tail)}", flush=True)
            if proc.returncode == 0:
                break


def run_uia(args):
    cmd = [sys.executable, "-u", os.path.join(HERE, "driver_uia.py"),
           "--out", args.out]
    if args.launch:
        cmd += ["--launch", args.launch]
    if args.attach:
        cmd += ["--attach", args.attach]
    if args.dry_run:
        cmd += ["--dry-run"]
    if args.blocklist:
        cmd += ["--blocklist", args.blocklist]
    for attempt in (1, 2):
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=args.timeout)
        tail = [l for l in proc.stdout.splitlines()
                if l.startswith(("PASS", "FAIL"))]
        print(f"[uia] try{attempt} exit={proc.returncode} "
              f"steps={len(tail)}", flush=True)
        if proc.returncode == 0:
            break


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("driver", choices=["pyqt", "uia"])
    ap.add_argument("--app-config", help="pyqt: config JSON (see driver)")
    ap.add_argument("--launch", help="uia: command to launch")
    ap.add_argument("--attach", help="uia: window title to attach to")
    ap.add_argument("--blocklist", help="uia: extra skip regex")
    ap.add_argument("--dry-run", action="store_true",
                    help="uia: list planned clicks without executing")
    ap.add_argument("--out", default="C:/Temp/uiwalk")
    ap.add_argument("--timeout", type=int, default=900,
                    help="per-phase subprocess timeout (s)")
    ap.add_argument("--grid", default="3x3")
    ap.add_argument("--no-sheets", action="store_true")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    if args.driver == "pyqt":
        if not args.app_config:
            sys.exit("pyqt driver needs --app-config")
        run_pyqt(args)
    else:
        run_uia(args)

    if not args.no_sheets:
        pngs = glob.glob(os.path.join(args.out, "*.png"))
        if pngs:
            sheets = subprocess.run(
                [sys.executable, os.path.join(HERE, "contact_sheets.py"),
                 args.out, "--out", os.path.join(args.out, "sheets"),
                 "--grid", args.grid], capture_output=True, text=True)
            print(sheets.stdout.strip())
    print("next: dispatch visual-judge subagents with the sheets triage "
          "prompt from references/judge-handoff.md")


if __name__ == "__main__":
    main()
