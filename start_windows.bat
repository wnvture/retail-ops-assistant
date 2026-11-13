@echo off
cd /d "%~dp0"
py -3 -m retail_ops serve
if errorlevel 1 (
 echo Install Python 3.11 or newer from python.org, then try again.
 pause
)
