#!/usr/bin/env bash
# Interactive wrapper — runs the full pipeline
cd "$(dirname "$0")"
echo "Running full weekend planner pipeline..."
echo
python3 weekend_planner.py
