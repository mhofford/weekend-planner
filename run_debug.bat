@echo off
cd /d "C:\Users\macke\Documents\Claude\Weekend_Plans"
echo Running weekend planner in DEBUG mode (skips Claude + email)...
echo Output will be written to debug_prompt.txt
echo.
"C:\Program Files\Python312\python.exe" weekend_planner.py --debug
echo.
echo Done! Check debug_prompt.txt for the generated prompt.
pause
