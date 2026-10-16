# Скачать веса визуальной башни SigLIP2 (90 МБ, Apache-2.0).
#
# Версия для Windows: там нет bash, а `scripts/pull-model.sh` без него
# не запускается. Хеш сверяется так же, как в sh-версии, — он и был
# единственной причиной, по которой скрипт существует.
#
# Запуск из PowerShell:
#   .\scripts\pull-model.ps1
#
# Если PowerShell ругается на политику выполнения:
#   powershell -ExecutionPolicy Bypass -File .\scripts\pull-model.ps1

[CmdletBinding()]
param(
    [string]$DestDir = (Join-Path (Split-Path -Parent $PSScriptRoot) "models")
)

$ErrorActionPreference = 'Stop'
$ModelRepo = 'onnx-community/siglip2-base-patch16-224-ONNX'
$ModelFile = 'onnx/vision_model_int8.onnx'
$ModelSha256 = '0dd31785a2713f1113ef2272472165c69d580473dae38d7b47568ac587795e70'
$Dest = Join-Path $DestDir 'vision_model_int8.onnx'

function Get-Sha256([string]$Path) {
    # -LiteralPath, а не -Path: путь может содержать квадратные скобки,
    # а такие есть в именах фотографий.
    (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

New-Item -ItemType Directory -Force -Path $DestDir | Out-Null

if (Test-Path -LiteralPath $Dest) {
    if ((Get-Sha256 $Dest) -eq $ModelSha256) {
        Write-Host "уже на месте: $Dest"
        exit 0
    }
    Write-Host "хеш не сходится, перекачиваю: $Dest"
    Remove-Item -LiteralPath $Dest -Force
}

$Url = "https://huggingface.co/$ModelRepo/resolve/main/$ModelFile"
Write-Host "качаю $Url -> $Dest"

# Качаем во временный файл: оборванная закачка не должна выглядеть как
# готовые веса, иначе проверка хеша ниже сработает не на том файле.
$Partial = "$Dest.partial"
Invoke-WebRequest -Uri $Url -OutFile $Partial -UseBasicParsing

Write-Host "проверяю sha256"
$have = Get-Sha256 $Partial
if ($have -ne $ModelSha256) {
    Remove-Item -LiteralPath $Partial -Force -ErrorAction SilentlyContinue
    Write-Error @"
SHA-256 не совпал
  ожидал: $ModelSha256
  получил: $have
"@
    exit 1
}

Move-Item -LiteralPath $Partial -Destination $Dest -Force
Write-Host "ок, $Dest"