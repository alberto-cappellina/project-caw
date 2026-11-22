#!/bin/sh
# Run the whole pipeline once, on demand (the same thing the weekly schedule does).
#
#   ./take_a_spin.sh                # download, parse, mark in Gmail, score, report
#   ./take_a_spin.sh --all          # the report lists all offers that are still open
#   ./take_a_spin.sh --no-fetch     # skip Gmail, work on the local archive only
#   ./take_a_spin.sh --notify       # also show a macOS notification at the end
#
# Any option is passed to `python -m caw.run`. Output goes to the terminal and is also
# appended to logs/run.log. Exit code: 0 = ok, 1 = at least one step failed.
set -e
PROJECT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT"

if [ ! -x .venv/bin/python ]; then
  echo "Missing .venv. Create it first:" >&2
  echo "  python3 -m venv .venv && .venv/bin/pip install pyyaml" >&2
  exit 2
fi

mkdir -p logs
export PYTHONPATH="$PROJECT/src"
echo "=== $(date '+%Y-%m-%d %H:%M:%S') take_a_spin.sh $* ===" >> logs/run.log

# `tee` hides the exit code of python, so save it in a file. `set +e`: a failing step must not
# stop the script before the code is saved.
set +e
status_file="$(mktemp)"
{ .venv/bin/python -m caw.run "$@"; echo $? > "$status_file"; } 2>&1 | tee -a logs/run.log
status="$(cat "$status_file")"
rm -f "$status_file"
exit "$status"
