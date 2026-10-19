# Desfaz o instalar-limite-jobrunner.ps1. Rode como administrador NO SERVIDOR:
#
#   powershell -ExecutionPolicy Bypass -File desinstalar-limite-jobrunner.ps1
#
# Ao parar, o servico tira o teto de todos os JobRunners no ar (evento 1001):
# nenhum processo do RM precisa ser reiniciado. Cada JobRunner segue dentro de
# um Job Object sem limite nenhum ate terminar - o mesmo que nao ter job.
#
# Remove: servico RMonLimiteJobRunner, C:\Program Files\RMon\LimiteJobRunner,
# e HKLM\SOFTWARE\RMon\LimiteJobRunner.
# O historico (C:\ProgramData\RMon\LimiteJobRunner) fica, a nao ser com -ApagarHistorico.
param([switch]$ApagarHistorico)
$ErrorActionPreference = 'Stop'
$Servico = 'RMonLimiteJobRunner'
$Origem = 'RMon-LimiteJobRunner'
$Pasta = Join-Path $env:ProgramFiles 'RMon\LimiteJobRunner'
$Dados = Join-Path $env:ProgramData 'RMon\LimiteJobRunner'
$Chave = 'HKLM:\SOFTWARE\RMon\LimiteJobRunner'

$eu = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $eu.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { Write-Host 'ERRO: rode como administrador.'; exit 1 }

Write-Host "== 1/4 parando $Servico (tira o teto dos JobRunners)"
$svc = Get-Service -Name $Servico -ErrorAction SilentlyContinue
if ($svc) {
    if ($svc.Status -ne 'Stopped') {
        Stop-Service -Name $Servico -Force
        $svc.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
    }
    $ev = Get-WinEvent -FilterHashtable @{ LogName = 'Application'; ProviderName = $Origem; Id = 1001 } -MaxEvents 1 -ErrorAction SilentlyContinue
    if ($ev) { Write-Host "   $($ev.Message)" }
    & sc.exe delete $Servico | Out-Null
    Write-Host '   servico removido'
} else { Write-Host '   servico nao existe' }

Write-Host '== 2/4 programa e configuracao'
for ($i = 0; $i -lt 10 -and (Test-Path -LiteralPath $Pasta); $i++) {
    try { Remove-Item -LiteralPath $Pasta -Recurse -Force } catch { Start-Sleep -Seconds 1 }
}
if (Test-Path -LiteralPath $Pasta) { Write-Host "   ATENCAO: nao consegui apagar $Pasta (arquivo em uso?)" }
$pai = Split-Path $Pasta
if ((Test-Path -LiteralPath $pai) -and -not (Get-ChildItem -LiteralPath $pai -Force)) { Remove-Item -LiteralPath $pai -Force }
if (Test-Path $Chave) { Remove-Item -Path $Chave -Recurse -Force }

# A origem do Event Log fica registrada: sem ela, o Visualizador de Eventos nao
# mostra mais o texto dos eventos ja gravados.
Write-Host "== 3/4 origem $Origem do Event Log: mantida (os eventos antigos seguem legiveis)"

Write-Host '== 4/4 historico'
if ($ApagarHistorico) {
    if (Test-Path -LiteralPath $Dados) { Remove-Item -LiteralPath $Dados -Recurse -Force }
    Write-Host '   apagado'
} else { Write-Host "   mantido em $Dados (use -ApagarHistorico para apagar)" }
Write-Host ''
Write-Host 'Pronto. O RMon passa a mostrar "nao instalado" na proxima coleta.'
