#!/usr/bin/env bash
# Interactive wrapper — runs in debug mode (skips Claude + email)
cd "$(dirname "$0")"
echo "Running weekend planner in DEBUG mode (skips Claude + email)..."
echo "Output will be written to debug_prompt.txt"
echo
python3 weekend_planner.py --debug
echo
echo "Done! Check debug_prompt.txt for the generated prompt."
