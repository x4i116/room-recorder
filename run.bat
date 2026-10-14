@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "SELF=%~f0"
set "SELF_DIR=%~dp0"
title Room Recorder

set "STARTUP_FILE=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Room Recorder.bat"

rem ---------- defaults (overwritten by settings.ini) ----------
set "MODE=voice"
set "DEVICE="
set "MARGIN=8"
set "THRESHOLD="
set "HANG=5"
set "MAXGB=20"
set "CHUNK=30"
set "FORMAT=flac"
set "SPECTRO=on"
set "DENOISE=medium"
set "BOOST=0"
set "SETTINGS_VER="
set "OUTDIR=recordings"

if exist "settings.ini" for /f "usebackq eol=# tokens=1,* delims==" %%A in ("settings.ini") do set "%%A=%%B"
if exist "settings.ini" if not defined SETTINGS_VER call :migrate

if not exist "room_recorder.py" (
    echo.
    echo  room_recorder.py was not found. Put run.bat in the same folder as room_recorder.py.
    echo.
    pause
    exit /b 1
)

rem ---------- find Python ----------
set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY (python --version >nul 2>nul && set "PY=python")
if not defined PY goto nopython

rem ---------- first-run library check ----------
%PY% -c "import sounddevice, soundfile, numpy, matplotlib" >nul 2>nul
if errorlevel 1 goto install_first

:after_check
rem keep auto-start pointing here, even if this folder was moved
if exist "%STARTUP_FILE%" call :write_startup
if /i "%~1"=="/auto" goto start

rem =====================================================================
:menu
call :labels
cls
echo  ===================================================
echo     ROOM RECORDER
echo  ===================================================
echo     Mode:         %MODE_LABEL%
echo     Microphone:   %DEV_LABEL%
echo     Sensitivity:  %SENS_LABEL%
echo     Noise filter: %DENOISE%   Boost: %BOOST% dB
echo     Storage cap:  %MAXGB% GB   Folder: %OUTDIR%
echo     Auto-start:   %AUTO_LABEL%
echo  ---------------------------------------------------
echo     [1] Start recording
echo     [2] Switch mode  (voice-activated / 24-7)
echo     [3] Settings
echo     [4] Sound level meter
echo     [5] Open recordings folder
echo     [6] Make spectrogram images for older recordings
echo     [7] Install / repair required libraries
echo     [8] Create desktop shortcut (to pin to taskbar)
echo     [0] Exit
echo  ===================================================
set "CH="
set /p "CH=   Choose an option: "
if "%CH%"=="1" goto start
if "%CH%"=="2" goto toggle_mode
if "%CH%"=="3" goto settings
if "%CH%"=="4" goto meter
if "%CH%"=="5" goto open_folder
if "%CH%"=="6" goto make_spectro
if "%CH%"=="7" goto install
if "%CH%"=="8" goto make_shortcut
if "%CH%"=="0" exit /b 0
goto menu

rem =====================================================================
:start
call :labels
call :buildargs
cls
echo  ===================================================
echo     RECORDING - %MODE_LABEL%
echo  ===================================================
echo     Files are saved to: %OUTDIR%
echo     Press Ctrl+C to stop.
echo     If Windows asks "Terminate batch job (Y/N)?", press N to return to the menu.
echo  ===================================================
echo.
%PY% room_recorder.py %ARGS%
echo.
pause
goto menu

:toggle_mode
if "%MODE%"=="voice" (set "MODE=continuous") else (set "MODE=voice")
call :save
goto menu

:meter
call :buildargs
cls
echo  Talk, clap and stay quiet to see your room's levels.
echo  Press Ctrl+C to stop. If asked "Terminate batch job (Y/N)?", press N.
echo.
%PY% room_recorder.py --meter %DEVARG% --denoise %DENOISE% --boost %BOOST%
echo.
pause
goto menu

:open_folder
if not exist "%OUTDIR%" mkdir "%OUTDIR%"
start "" "%OUTDIR%"
goto menu

:make_spectro
cls
echo  Drawing a waveform + spectrogram .jpg for any recording that doesn't have one yet...
echo.
%PY% room_recorder.py --make-spectrograms --out "%OUTDIR%"
echo.
pause
goto menu

:make_shortcut
cls
set "RR_BAT=%SELF%"
set "RR_DIR=%SELF_DIR%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$w=New-Object -ComObject WScript.Shell; $s=$w.CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Room Recorder.lnk'); $s.TargetPath=$env:ComSpec; $s.Arguments='/c '+[char]34+$env:RR_BAT+[char]34; $s.WorkingDirectory=$env:RR_DIR; if (Test-Path ($env:RR_DIR+'RoomRecorder.ico')) { $s.IconLocation=$env:RR_DIR+'RoomRecorder.ico,0' }; $s.Save()"
if errorlevel 1 (
    echo.
    echo  Couldn't create the shortcut. See the README for how to make one by hand.
) else (
    echo.
    echo  "Room Recorder" shortcut added to your desktop.
    echo.
    echo  To pin it: right-click the shortcut and choose "Pin to taskbar"
    echo  ^(on Windows 11 it may be under "Show more options"^).
    echo  After pinning you can delete the desktop shortcut.
)
echo.
pause
goto menu

:install
cls
%PY% -m pip install --upgrade sounddevice soundfile numpy matplotlib
echo.
pause
goto menu

rem =====================================================================
:settings
call :labels
cls
echo  ===================================================
echo     SETTINGS
echo  ===================================================
echo     [1] Microphone ..................... %DEV_LABEL%
echo     [2] Sensitivity (voice mode) ....... %SENS_LABEL%
echo     [3] Quiet time before clip ends .... %HANG% sec
echo     [4] Storage cap .................... %MAXGB% GB
echo     [5] File length in 24/7 mode ....... %CHUNK% min
echo     [6] File format .................... %FORMAT%
echo     [7] Recordings folder .............. %OUTDIR%
echo     [8] Start when Windows starts ...... %AUTO_LABEL%
echo     [9] Spectrogram image per clip ..... %SPECTRO%
echo     [N] Noise reduction ................ %DENOISE%
echo     [B] Volume boost ................... %BOOST% dB
echo     [0] Back
echo  ===================================================
set "CH="
set /p "CH=   Choose an option: "
if "%CH%"=="1" goto set_device
if "%CH%"=="2" goto set_sens
if "%CH%"=="3" goto set_hang
if "%CH%"=="4" goto set_maxgb
if "%CH%"=="5" goto set_chunk
if "%CH%"=="6" goto toggle_format
if "%CH%"=="7" goto set_outdir
if "%CH%"=="8" goto toggle_startup
if "%CH%"=="9" goto toggle_spectro
if /i "%CH%"=="N" goto set_denoise
if /i "%CH%"=="B" goto set_boost
if "%CH%"=="0" goto menu
goto settings

:set_device
cls
%PY% room_recorder.py --list-devices
echo.
echo  The device marked with ">" is the Windows default input.
echo  Enter your microphone's number, or just press Enter to use the default.
set "IN="
set /p "IN=   Device number: "
if not defined IN (set "DEVICE=" & goto saved)
echo %IN%| findstr /r "^[0-9][0-9]*$" >nul || goto bad
set "DEVICE=%IN%"
goto saved

:set_sens
cls
echo  How easily should sound start a recording?
echo.
echo     [1] High    - catches quiet sounds, more false alarms
echo     [2] Normal  - recommended
echo     [3] Low     - only clear, loud sounds
echo.
echo     These are measured above the room's learned background noise,
echo     so a loud hard drive or fan doesn't count against you.
echo     [4] Fixed level - type a number you saw in the level meter
echo     [0] Back
echo.
set "IN="
set /p "IN=   Choose an option: "
if "%IN%"=="1" (set "MARGIN=5" & set "THRESHOLD=" & goto saved)
if "%IN%"=="2" (set "MARGIN=8" & set "THRESHOLD=" & goto saved)
if "%IN%"=="3" (set "MARGIN=12" & set "THRESHOLD=" & goto saved)
if "%IN%"=="4" goto set_threshold
goto settings

:set_threshold
echo.
echo  Use the level meter: pick a number a bit above what a quiet room shows.
echo  Example: quiet room reads -55, talking reads -30, so enter -45.
echo  Closer to 0 = less sensitive.
set "IN="
set /p "IN=   Trigger level (e.g. -45): "
if not defined IN goto settings
echo %IN%| findstr /r "^-[0-9][0-9.]*$" >nul || goto bad
set "THRESHOLD=%IN%"
goto saved

:set_hang
echo.
set "IN="
set /p "IN=   Seconds of quiet before a clip ends (now %HANG%): "
if not defined IN goto settings
echo %IN%| findstr /r "^[0-9][0-9.]*$" >nul || goto bad
set "HANG=%IN%"
goto saved

:set_maxgb
echo.
echo  When the folder grows past this size, the oldest recordings are deleted.
set "IN="
set /p "IN=   Storage cap in GB (now %MAXGB%): "
if not defined IN goto settings
echo %IN%| findstr /r "^[0-9][0-9.]*$" >nul || goto bad
set "MAXGB=%IN%"
goto saved

:set_chunk
echo.
set "IN="
set /p "IN=   Minutes per file in 24/7 mode (now %CHUNK%): "
if not defined IN goto settings
echo %IN%| findstr /r "^[0-9][0-9.]*$" >nul || goto bad
set "CHUNK=%IN%"
goto saved

:toggle_format
if "%FORMAT%"=="flac" (set "FORMAT=wav") else (set "FORMAT=flac")
goto saved

:toggle_spectro
if "%SPECTRO%"=="on" (set "SPECTRO=off") else (set "SPECTRO=on")
goto saved

:set_denoise
cls
echo  Noise reduction learns the steady background - hard drive hum, fans,
echo  PC whine, HVAC - and removes it, so you can run the mic gain high.
echo.
echo     [1] Off
echo     [2] Low     - gentle, most natural sound
echo     [3] Medium  - recommended
echo     [4] High    - strongest, voices can sound a little thin
echo     [0] Back
echo.
set "IN="
set /p "IN=   Choose an option: "
if "%IN%"=="1" (set "DENOISE=off" & goto saved)
if "%IN%"=="2" (set "DENOISE=low" & goto saved)
if "%IN%"=="3" (set "DENOISE=medium" & goto saved)
if "%IN%"=="4" (set "DENOISE=high" & goto saved)
goto settings

:set_boost
echo.
echo  Extra volume on saved recordings. Loud peaks are softly limited, not clipped.
echo  Try 6 to 12. Use 0 for none.
set "IN="
set /p "IN=   Boost in dB (now %BOOST%): "
if not defined IN goto settings
echo %IN%| findstr /r "^[0-9][0-9.]*$" >nul || goto bad
set "BOOST=%IN%"
goto saved

:set_outdir
echo.
echo  Enter a full path (e.g. D:\Recordings) or a folder name next to run.bat.
set "IN="
set /p "IN=   Recordings folder (now %OUTDIR%): "
if not defined IN goto settings
set "OUTDIR=%IN:"=%"
goto saved

:toggle_startup
if exist "%STARTUP_FILE%" (del "%STARTUP_FILE%" & goto settings)
call :write_startup
echo.
echo  Room Recorder will now start minimized in its current mode when you log in.
pause
goto settings

:saved
call :save
goto settings

:bad
echo.
echo  That isn't a valid value - nothing was changed.
pause
goto settings

rem =====================================================================
:install_first
cls
echo  First-time setup: installing the libraries Room Recorder needs...
echo.
%PY% -m pip install --upgrade sounddevice soundfile numpy matplotlib
if errorlevel 1 goto install_failed
goto after_check

:install_failed
echo.
echo  The install failed. Check your internet connection and run this again.
pause
exit /b 1

:nopython
echo.
echo  Python wasn't found. Install it from python.org/downloads and check
echo  "Add python.exe to PATH" on the first installer screen, then run this again.
echo.
pause
exit /b 1

rem =====================================================================
:labels
if "%MODE%"=="continuous" (set "MODE_LABEL=24/7 continuous") else (set "MODE_LABEL=Voice-activated")
if defined DEVICE (set "DEV_LABEL=Device #%DEVICE%") else (set "DEV_LABEL=Windows default")
set "SENS_LABEL=Custom, %MARGIN% dB over room noise"
if "%MARGIN%"=="5" set "SENS_LABEL=High"
if "%MARGIN%"=="8" set "SENS_LABEL=Normal"
if "%MARGIN%"=="12" set "SENS_LABEL=Low"
if defined THRESHOLD set "SENS_LABEL=Fixed at %THRESHOLD% dBFS"
if exist "%STARTUP_FILE%" (set "AUTO_LABEL=On") else (set "AUTO_LABEL=Off")
exit /b

:buildargs
set "DEVARG="
if defined DEVICE set "DEVARG=--device %DEVICE%"
set ARGS=--mode %MODE% --out "%OUTDIR%" --format %FORMAT% --max-gb %MAXGB% --hang %HANG% --chunk-minutes %CHUNK% --margin %MARGIN% %DEVARG%
if defined THRESHOLD set ARGS=%ARGS% --threshold %THRESHOLD%
if "%SPECTRO%"=="off" set ARGS=%ARGS% --no-spectrograms
set ARGS=%ARGS% --denoise %DENOISE% --boost %BOOST%
exit /b

:save
> "settings.ini" echo MODE=%MODE%
>> "settings.ini" echo DEVICE=%DEVICE%
>> "settings.ini" echo MARGIN=%MARGIN%
>> "settings.ini" echo THRESHOLD=%THRESHOLD%
>> "settings.ini" echo HANG=%HANG%
>> "settings.ini" echo MAXGB=%MAXGB%
>> "settings.ini" echo CHUNK=%CHUNK%
>> "settings.ini" echo FORMAT=%FORMAT%
>> "settings.ini" echo SPECTRO=%SPECTRO%
>> "settings.ini" echo DENOISE=%DENOISE%
>> "settings.ini" echo BOOST=%BOOST%
>> "settings.ini" echo SETTINGS_VER=2
>> "settings.ini" echo OUTDIR=%OUTDIR%
exit /b

:migrate
if "%MARGIN%"=="8" (set "MARGIN=5") else if "%MARGIN%"=="12" (set "MARGIN=8") else if "%MARGIN%"=="18" (set "MARGIN=12")
call :save
exit /b

:write_startup
> "%STARTUP_FILE%" echo @echo off
>> "%STARTUP_FILE%" echo start "Room Recorder" /min "%SELF%" /auto
exit /b
