@echo off
schtasks /Delete /TN "Commodities - oil and gas update" /F
echo The scheduled task was removed. You can delete this folder now.
pause
