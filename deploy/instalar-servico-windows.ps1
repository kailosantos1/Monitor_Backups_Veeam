# Windows Server: instala como serviço usando NSSM (https://nssm.cc).
# Rode no PowerShell como administrador, de dentro da pasta do sistema.
$dir = (Get-Location).Path
$py  = Join-Path $dir ".venv\Scripts\python.exe"
nssm install VeeamMonitor $py "manage.py servir"
nssm set VeeamMonitor AppDirectory $dir
nssm set VeeamMonitor AppStdout (Join-Path $dir "data\servico.log")
nssm set VeeamMonitor AppStderr (Join-Path $dir "data\servico.log")
nssm set VeeamMonitor AppRotateFiles 1
nssm set VeeamMonitor AppRotateBytes 10485760
nssm set VeeamMonitor Start SERVICE_AUTO_START
nssm start VeeamMonitor
