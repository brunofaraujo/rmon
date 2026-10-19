# Teto de memoria por processo para o RM.Host.JobRunner (docs/OPERACAO.md,
# "Teto de memoria do JobRunner"). Rode como administrador NO SERVIDOR do RM:
#
#   powershell -ExecutionPolicy Bypass -File instalar-limite-jobrunner.ps1
#       instala (ou atualiza) no modo "registrar": nada e bloqueado, so avisa no
#       Event Log quando um JobRunner passaria do teto. E o modo para calibrar.
#
#   powershell -ExecutionPolicy Bypass -File instalar-limite-jobrunner.ps1 -Modo aplicar
#       o Windows passa a negar a memoria acima do teto: o job falha por falta
#       de memoria e o servidor segue de pe.
#
# Ou da VM do RMon, sem copiar nada: deploy/limite_jobrunner_remoto.py.
#
# O que faz (e so isso):
#  - compila deploy/windows/RMonLimiteJobRunner.cs com o csc.exe do .NET Framework
#    4 do proprio servidor (nada de binario baixado) em
#    C:\Program Files\RMon\LimiteJobRunner\RMonLimiteJobRunner.exe;
#  - grava a configuracao em HKLM\SOFTWARE\RMon\LimiteJobRunner;
#  - cria a origem RMon-LimiteJobRunner no log Application;
#  - cria o servico RMonLimiteJobRunner (LocalSystem, automatico, reinicia
#    sozinho se cair) e confere que ele pos os JobRunners sob o teto.
#
# Parametros:
#   -Modo registrar|aplicar   padrao: registrar
#   -TetoMB N                 padrao: 6144 MB, sem passar de 40% da RAM
#   -Processo / -Pais         padrao: RM.Host.JobRunner, filho de RM.Host.Service*
#
# Desfazer: desinstalar-limite-jobrunner.ps1 (na mesma pasta).
param(
    [ValidateSet('registrar', 'aplicar')][string]$Modo = 'registrar',
    [int]$TetoMB = 0,
    [string]$Processo = 'RM.Host.JobRunner',
    [string]$Pais = 'RM.Host.Service*',
    [ValidateRange(500, 60000)][int]$IntervaloMs = 2000,
    [string]$Fonte = '',
    [string]$FonteTexto = ''
)
$ErrorActionPreference = 'Stop'
$Servico = 'RMonLimiteJobRunner'
$Origem = 'RMon-LimiteJobRunner'
$Pasta = Join-Path $env:ProgramFiles 'RMon\LimiteJobRunner'
$Exe = Join-Path $Pasta 'RMonLimiteJobRunner.exe'
$Dados = Join-Path $env:ProgramData 'RMon\LimiteJobRunner'
$Chave = 'HKLM:\SOFTWARE\RMon\LimiteJobRunner'
$Csc = Join-Path $env:windir 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'

function Falha($msg) { Write-Host "ERRO: $msg"; exit 1 }

$eu = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $eu.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { Falha 'rode como administrador.' }

Write-Host '== 1/6 conferencia'
$os = Get-CimInstance Win32_OperatingSystem
if ([version]$os.Version -lt [version]'10.0') { Falha "Windows $($os.Version): precisa do Server 2016 ou mais novo (job aninhado)." }
if (-not [Environment]::Is64BitOperatingSystem) { Falha 'Windows de 32 bits.' }
if (-not (Test-Path -LiteralPath $Csc)) { Falha "nao achei o compilador do .NET Framework 4 ($Csc)." }
if (-not $FonteTexto) {
    if (-not $Fonte) { $Fonte = Join-Path $PSScriptRoot 'RMonLimiteJobRunner.cs' }
    if (-not (Test-Path -LiteralPath $Fonte)) { Falha "nao achei $Fonte." }
    $FonteTexto = [IO.File]::ReadAllText($Fonte)
}
if ($Processo -notmatch '^[A-Za-z0-9._-]+$') { Falha "nome de processo invalido: $Processo" }
$ramMB = [math]::Floor((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1MB)
$tetoAuto = [math]::Min(6144, [math]::Floor($ramMB * 0.4 / 256) * 256)
if ($TetoMB -le 0) { $TetoMB = [int]$tetoAuto }
if ($TetoMB -lt 256) { Falha "teto de $TetoMB MB: o minimo e 256 MB." }
Write-Host "   $env:COMPUTERNAME, $($os.Caption), RAM $ramMB MB"
Write-Host "   modo $Modo, teto $TetoMB MB por processo (automatico seria $tetoAuto MB)"
if ($TetoMB -gt $ramMB * 0.5) { Write-Host "   ATENCAO: o teto passa de metade da RAM ($ramMB MB); um JobRunner sozinho ainda pode apertar o servidor." }
$jr = @(Get-CimInstance Win32_Process -Filter "Name='$Processo.exe'")
Write-Host "   $($jr.Count) processo(s) $Processo no ar agora"

Write-Host '== 2/6 compilando'
$tmp = Join-Path $env:TEMP ("rmon-lj-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    $src = Join-Path $tmp 'RMonLimiteJobRunner.cs'
    [IO.File]::WriteAllText($src, $FonteTexto, (New-Object Text.UTF8Encoding($false)))
    $saida = & $Csc /nologo /target:exe /platform:x64 /optimize+ "/out:$tmp\RMonLimiteJobRunner.exe" `
        /r:System.ServiceProcess.dll /r:System.Web.Extensions.dll $src 2>&1
    if ($LASTEXITCODE -ne 0) { Falha ("a compilacao falhou:`n" + ($saida | Out-String)) }

    Write-Host '== 3/6 parando a versao anterior (se houver)'
    $velho = Get-Service -Name $Servico -ErrorAction SilentlyContinue
    if ($velho -and $velho.Status -ne 'Stopped') {
        Stop-Service -Name $Servico -Force
        $velho.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
        Write-Host '   parado (os JobRunners ficam sem teto ate o passo 6)'
    }
    New-Item -ItemType Directory -Path $Pasta -Force | Out-Null
    # o processo do servico pode levar um instante para soltar o .exe
    for ($i = 1; $i -le 10; $i++) {
        try { Copy-Item -LiteralPath "$tmp\RMonLimiteJobRunner.exe" -Destination $Exe -Force; break }
        catch { if ($i -eq 10) { throw }; Start-Sleep -Seconds 1 }
    }
    Copy-Item -LiteralPath $src -Destination (Join-Path $Pasta 'RMonLimiteJobRunner.cs') -Force
} finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
New-Item -ItemType Directory -Path $Dados -Force | Out-Null
# so SYSTEM e administradores escrevem o estado: o servidor tem dezenas de
# usuarios de RDP, e o ProgramData deixa qualquer um criar arquivo
& icacls.exe $Dados /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' | Out-Null

Write-Host "== 4/6 configuracao em $Chave e origem $Origem no Event Log"
New-Item -Path $Chave -Force | Out-Null
Set-ItemProperty -Path $Chave -Name Modo -Value $Modo -Type String
Set-ItemProperty -Path $Chave -Name TetoMB -Value $TetoMB -Type DWord
Set-ItemProperty -Path $Chave -Name Processo -Value $Processo -Type String
Set-ItemProperty -Path $Chave -Name Pais -Value $Pais -Type String
Set-ItemProperty -Path $Chave -Name IntervaloMs -Value $IntervaloMs -Type DWord
if (-not [Diagnostics.EventLog]::SourceExists($Origem)) { New-EventLog -LogName Application -Source $Origem }

Write-Host "== 5/6 servico $Servico"
if (-not (Get-Service -Name $Servico -ErrorAction SilentlyContinue)) {
    New-Service -Name $Servico -BinaryPathName "`"$Exe`"" -StartupType Automatic `
        -DisplayName 'RMon - teto de memoria do RM.Host.JobRunner' | Out-Null
}
# conta: LocalSystem (padrao do New-Service). Sem sc.exe config binPath=: o
# PowerShell 5.1 estraga aspas dentro de argumento de programa externo.
Set-Service -Name $Servico -StartupType Automatic `
    -Description "Poe cada $Processo num Job Object com teto de memoria (RMon, docs/OPERACAO.md). Parar = tirar o teto."
# cai -> volta em 1 min, sempre; o contador zera em 1 dia
& sc.exe failure $Servico reset= 86400 actions= restart/60000/restart/60000/restart/60000 | Out-Null
& sc.exe failureflag $Servico 1 | Out-Null

Write-Host '== 6/6 iniciando e conferindo'
$antes = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
Start-Service -Name $Servico
$arq = Join-Path $Dados 'estado.json'
$e = $null
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 1
    try {
        $e = Get-Content -LiteralPath $arq -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($e.ativo -and $e.atualizado_epoch -ge $antes) { break }
    } catch { }
    $e = $null
}
$svc = Get-Service -Name $Servico
if (-not $e) { Falha "o servico esta $($svc.Status), mas nao gravou $arq em 20 s. Veja o log Application, origem $Origem." }
Write-Host "   servico $($svc.Status); modo $($e.modo), teto $($e.teto_mb) MB"
Write-Host "   $($e.sob_teto) de $($e.processos) processo(s) $Processo sob o teto; $($e.falhas) falha(s); $($e.ignorados) fora dos pais aceitos"
if ($e.erro) { Write-Host "   ultimo erro: $($e.erro)" }
if ($e.falhas -gt 0) { Write-Host "   ATENCAO: ha processo que nao foi posto sob o teto (evento 1090 no log Application)." }
Write-Host ''
Write-Host "Pronto. Eventos: Get-WinEvent -FilterHashtable @{LogName='Application'; ProviderName='$Origem'} -MaxEvents 20"
Write-Host "Trocar o modo sem reinstalar: Set-ItemProperty '$Chave' Modo aplicar   (vale em ate $([math]::Ceiling($IntervaloMs / 1000)) s)"
