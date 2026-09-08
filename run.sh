#!/usr/bin/env bash
# Wrapper script for cron/scheduled runs (logs to run.log)
cd "$(dirname "$0")"
python3 weekend_planner.py >> run.log 2>&1
