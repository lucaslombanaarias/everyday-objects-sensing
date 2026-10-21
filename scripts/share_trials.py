"""Publish a day of dashboard trials to data/controlled/ and push them.

    python scripts/share_trials.py [<date>]

data/raw/ and data/analysis/ are not committed, so trials recorded on one computer do not
reach the others through git. This pulls, copies each trial's .json and .mat from
data/raw/<date>/ to data/controlled/raw/<date>/ (the per-sample CSVs hold the same data and
are left out), copies data/analysis/<date>/ with its event tags to
data/controlled/analysis/<date>/, then commits and pushes. Without a date it takes the
newest folder in data/raw/. Run it from the repo root.
"""

import argparse
import shutil
import subprocess
from pathlib import Path

RAW = Path("data/raw")
ANALYSIS = Path("data/analysis")
CONTROLLED = Path("data/controlled")


def git(*args):
    subprocess.run(["git", *args], check=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("date", nargs="?", help="a data/raw/<date> folder; default: the newest")
    args = parser.parse_args(argv)

    days = sorted(p.name for p in RAW.glob("*") if p.is_dir())
    if not days:
        raise SystemExit(f"no trials in {RAW}/ (run this from the repo root)")
    date = args.date or days[-1]
    src = RAW / date
    trials = sorted(src.glob("*.json")) + sorted(src.glob("*.mat"))
    if not trials:
        raise SystemExit(f"no .json or .mat trials in {src}/ (folders: {', '.join(days)})")

    git("pull")
    dst = CONTROLLED / "raw" / date
    dst.mkdir(parents=True, exist_ok=True)
    for f in trials:
        shutil.copy2(f, dst / f.name)
    print(f"copied {len(trials)} files from {src}/ to {dst}/")
    if (ANALYSIS / date).is_dir():
        shutil.copytree(ANALYSIS / date, CONTROLLED / "analysis" / date, dirs_exist_ok=True)
        print(f"copied {ANALYSIS / date}/ to {CONTROLLED / 'analysis' / date}/")

    git("add", str(CONTROLLED))
    staged = subprocess.run(["git", "diff", "--cached", "--quiet", "--", str(CONTROLLED)])
    if staged.returncode == 0:
        print("nothing new to push")
        return
    git("commit", "-m", f"Add floor trials from {date}", "--", str(CONTROLLED))
    git("push")


if __name__ == "__main__":
    main()
