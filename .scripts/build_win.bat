
setlocal EnableExtensions EnableDelayedExpansion

:: Defender's real-time scan slows down installing and deleting the environments'
:: files several times over; the runner is thrown away after the job
powershell -NoProfile -Command "Set-MpPreference -DisableRealtimeMonitoring $true"

:: The builds, rattler's package cache (on the same drive, for hardlinks) and the
:: temporary files on D:, about three times as fast as C: on GitHub's runners
set CONDA_BLD_PATH=D:\bld
set VINCA_CUSTOM_CMAKE_BUILD_DIR=D:\x\
set RATTLER_CACHE_DIR=D:\rattler
set TMP=D:\tmp
set TEMP=D:\tmp
mkdir D:\tmp
powershell -NoProfile -Command "Get-PSDrive C, D | Format-Table Name, @{n='free GB'; e={[math]::Round($_.Free / 1GB)}}"
echo "PATH is %PATH%"

rmdir /Q/S C:\Strawberry\
rmdir /Q/S "C:\Program Files (x86)\Windows Kits\10\Include\10.0.17763.0\"

:: ROBOSTACK_DISTRO is set by the generated build workflow (tools/robostack.py gha).
cd distros\%ROBOSTACK_DISTRO%\work
set "FEEDSTOCK_ROOT=%cd%"

mkdir %CONDA_BLD_PATH%

:: Enable long path names on Windows
reg add HKLM\SYSTEM\CurrentControlSet\Control\FileSystem /v LongPathsEnabled /t REG_DWORD /d 1 /f
:: ... and in git, for sources that vendor packages clone during the build
git config --global core.longpaths true

:: All of the job's recipes in one build-ci run (tools/robostack.py), which builds
:: independent ones in parallel and adds the variant config and the distro's channels.
set RECIPE_ARGS=
for %%X in (%CURRENT_RECIPES%) do set "RECIPE_ARGS=!RECIPE_ARGS! --recipe %FEEDSTOCK_ROOT%\recipes\%%X\"
pixi run -v rs %ROBOSTACK_DISTRO% build-ci !RECIPE_ARGS! --output-dir %CONDA_BLD_PATH%
if errorlevel 1 exit 1

:: Upload the win-64 and noarch packages one-by-one; the upload task (tools/robostack.py)
:: uploads them to prefix.dev and anaconda.org and skips packages that are already there
:: (noarch ones, e.g. the compatibility packages, are uploaded from every platform).
set FOUND=0
for %%D in (win-64 noarch) do (
    for %%F in ("%CONDA_BLD_PATH%\%%D\*.conda") do (
        set FOUND=1
        echo Uploading %%~fF
        pixi run rs %ROBOSTACK_DISTRO% upload "%%~fF"
        if errorlevel 1 exit 1
    )
)
if "!FOUND!" == "0" (
    echo Warning: No .conda files found in %CONDA_BLD_PATH%\win-64 or %CONDA_BLD_PATH%\noarch
    echo This might be due to all the packages being skipped
)
