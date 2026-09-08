#!/usr/bin/env bash
# Wrapper for scheduled runs (systemd timer / cron). Appends to run.log.
#
# Picks a python3 that actually has the project's dependencies installed.
# The interactive shell finds Anaconda's python via .bashrc, but cron and
# systemd run with a minimal PATH where `python3` is /usr/bin/python3 (no deps).
# Override with WEEKEND_PLANNER_PYTHON=/path/to/python3 if needed.
cd "$(dirname "$0")" || exit 1

for py in "$WEEKEND_PLANNER_PYTHON" "$HOME/anaconda3/bin/python3" "$(command -v python3)"; do
    [ -n "$py" ] && [ -x "$py" ] || continue
    if "$py" -c 'import dotenv, anthropic, agentmail, googleapiclient' 2>/dev/null; then
        exec "$py" weekend_planner.py >> run.log 2>&1
    fi
done

echo "$(date '+%Y-%m-%d %H:%M:%S'): run.sh could not find a python3 with the required packages" >> run.log
exit 1
