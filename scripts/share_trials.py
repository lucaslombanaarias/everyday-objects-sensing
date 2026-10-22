"""Publish a day of dashboard trials to data/controlled/ and push them.

    python scripts/share_trials.py [<date> | <folder>]

data/raw/ and data/analysis/ are not committed, so trials recorded on one computer do not
reach the others through git. This pulls, copies each trial's .json and .mat from the day's
folder to data/controlled/raw/<date>/ (the per-sample CSVs hold the same data and are left
out), copies the day's analysis outputs with their event tags to
data/controlled/analysis/<date>/, then commits and pushes. Trials already published under
another name (renamed after sharing) are skipped, and so are the folder's day-wide summary
CSVs then, since they carry the old names.

Without an argument it takes the newest day in this clone's data/raw/; give a date for
another day, or the path of any folder of trials (one recorded in another copy of the repo
or with --out-dir). It needs only Python and git, and can be run from any folder.
"""

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = REPO / "data" / "raw"
CONTROLLED = REPO / "data" / "controlled"


def git(*args, check=True):
    return subprocess.run(["git", *args], cwd=REPO, check=check)


def find_day(arg: str | None) -> Path:
    if arg and Path(arg).is_dir():
        return Path(arg).resolve()
    days = sorted(p.name for p in DEFAULT_OUT_DIR.glob("*") if p.is_dir())
    if not days:
        raise SystemExit(f"no day folders in {DEFAULT_OUT_DIR} (give the folder of the "
                         "trials: python scripts/share_trials.py <folder>)")
    if arg and arg not in days:
        raise SystemExit(f"no folder {arg} in {DEFAULT_OUT_DIR} (days there: "
                         f"{', '.join(days)}); give the folder of the trials instead")
    return DEFAULT_OUT_DIR / (arg or days[-1])


def published_under_other_names(sidecars: list[Path], dst: Path) -> list[tuple[str, str]]:
    """(local name, published name) of each trial already in dst under another trial ID,
    matched by start_time, as after the published copies were renamed."""
    published = {}
    for p in dst.glob("*.json"):
        m = json.loads(p.read_text(encoding="utf-8"))
        published[m.get("start_time")] = m.get("trial_id")
    pairs = []
    for p in sidecars:
        start = json.loads(p.read_text(encoding="utf-8")).get("start_time")
        if published.get(start, p.stem) != p.stem:
            pairs.append((p.stem, published[start]))
    return pairs


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("day", nargs="?",
                        help="a date in data/raw/ or a folder of trials; default: the newest day")
    args = parser.parse_args(argv)

    day = find_day(args.day)
    print(f"looking in {day}")
    sidecars = sorted(day.glob("*.json"))
    trials = sidecars + sorted(day.glob("*.mat"))
    if not trials:
        raise SystemExit(f"no .json or .mat trials in {day}")
    date = day.name
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        date = json.loads(sidecars[0].read_text()).get("date", date) if sidecars else date

    git("pull")
    dst = CONTROLLED / "raw" / date
    renamed = published_under_other_names(sidecars, dst)
    if renamed:
        old = {name for name, _ in renamed}
        trials = [f for f in trials if f.stem not in old]
        print(f"skipping {len(renamed)} trials already published under other names, e.g. "
              f"{renamed[0][0]} as {renamed[0][1]}")
        if not trials:
            raise SystemExit("nothing new to share")
    dst.mkdir(parents=True, exist_ok=True)
    for f in trials:
        shutil.copy2(f, dst / f.name)
    print(f"copied {len(trials)} files to {dst}")
    # Where everyday_sensing.impacts.analysis_folder puts the det-v1 outputs for the day.
    analysis = day.parent.parent / "analysis" / day.name if day.parent.name == "raw" else (
        day / "analysis")
    if analysis.is_dir() and renamed:
        new = {f.stem for f in trials}
        files = [f for f in analysis.glob("*") if f.name.removeprefix("events_").split(".")[0]
                 in new]
        (CONTROLLED / "analysis" / date).mkdir(parents=True, exist_ok=True)
        for f in files:
            shutil.copy2(f, CONTROLLED / "analysis" / date / f.name)
        print(f"copied the analysis of the {len(new)} new trials from {analysis}")
    elif analysis.is_dir():
        shutil.copytree(analysis, CONTROLLED / "analysis" / date, dirs_exist_ok=True)
        print(f"copied {analysis} to {CONTROLLED / 'analysis' / date}")

    git("add", str(CONTROLLED))
    if git("diff", "--cached", "--quiet", "--", str(CONTROLLED), check=False).returncode == 0:
        print("nothing new to push")
        return
    git("commit", "-m", f"Add floor trials from {date}", "--", str(CONTROLLED))
    git("push")


if __name__ == "__main__":
    main()
