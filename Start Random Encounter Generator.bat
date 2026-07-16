@echo off
title PF2e Random Encounter Generator
cd /d "%~dp0"

if exist "F:\Obsidian\PF2e_Encounter_Generator\services\loot_logic.py" (
  set "PF2E_LOOT_GENERATOR_PATH=F:\Obsidian\PF2e_Encounter_Generator"
) else if exist "%~dp0..\PF2e_Encounter_Generator\services\loot_logic.py" (
  set "PF2E_LOOT_GENERATOR_PATH=%~dp0..\PF2e_Encounter_Generator"
)

where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 server.py
goto finished

:use_python
python server.py

:finished
echo.
echo The generator has stopped.
pause
