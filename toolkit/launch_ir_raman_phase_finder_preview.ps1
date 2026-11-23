param([string]$AppId = 'ir_raman_phase_finder')

# Keep existing Windows shortcuts working after the switch to the GUI entry point.
$launcher = Join-Path $PSScriptRoot '..\launch_raman_phase_finder.vbs'
Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\wscript.exe') -ArgumentList ('"' + $launcher + '"')
