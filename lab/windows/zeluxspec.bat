@echo off
rem Starts the preset ThermoZeluxMockSpectroMockThermo: from the Scripts folder of the environment, or from anywhere when the environment is active
if exist "%~dp0thermospectro.exe" (
    "%~dp0thermospectro.exe" ThermoZeluxMockSpectroMockThermo %*
) else (
    thermospectro ThermoZeluxMockSpectroMockThermo %*
)
