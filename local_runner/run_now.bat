@echo off
schtasks /Run /TN "Commodities - oil and gas update" >nul 2>&1 && (echo Started in the background. Check update_log.txt in a few minutes.) || (echo The scheduled task is not set up - run setup.bat first.)
pause
