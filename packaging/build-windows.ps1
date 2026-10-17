# Сборка kurator.exe и установщика kurator-setup.exe для Windows.
#
# Что делает:
#   1. ставит Inno Setup, если его нет;
#   2. качает веса, если их нет;
#   3. собирает kurator.exe через PyInstaller;
#   4. собирает установщик kurator-setup.exe через Inno Setup.
#
# Собирать надо на Windows: PyInstaller не умеет кросс-компиляцию, и на
# macOS этот скрипт бесполезен. То же самое делает джоб `build` в
# .github/workflows/ci.yml, так что вручную запускать придётся разве что
# для отладки.
#
# Запуск:
#   powershell -ExecutionPolicy Bypass -File .\packaging\build-windows.ps1
#   powershell -ExecutionPolicy Bypass -File .\packaging\build-windows.ps1 -NoInstaller
param(
    [switch]$NoInstaller,
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

function Say([string]$Message) {
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Fail([string]$Message) {
    Write-Error $Message
    exit 1
}

if ($env:OS -ne 'Windows_NT') {
    Fail "сборка идёт только на Windows: текущая система $($env:OS)"
}

Say 'проверяю Python'
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { Fail 'не найден python. Поставь Python 3.11 или новее.' }
$version = & python -c "import sys; print('%d.%d' % sys.version_info[:2])"
Write-Host "    python $version"

Say 'ставлю uv'
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Invoke-WebRequest -Uri 'https://astral.sh/uv/install.ps1' -UseBasicParsing |
        Invoke-Expression
    # Установщик uv кладёт себя в профиль PowerShell, а текущая сессия
    # его ещё не подхватила. Дописываем путь руками, иначе следующая же
    # строка скажет «uv не найден».
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) { Fail 'uv не установился' }

Say 'зависимости'
& uv sync --group dev
if ($LASTEXITCODE -ne 0) { Fail 'uv sync не прошёл' }

Say 'веса модели'
if (-not (Test-Path -LiteralPath 'models\vision_model_int8.onnx')) {
    & .\scripts\pull-model.ps1
    if ($LASTEXITCODE -ne 0) { Fail 'веса не скачались' }
} else {
    Write-Host '    уже на месте'
}

Say 'значок для Windows'
# Только проверка, не перезапись: если иконка разошлась с исходным PNG,
# это ошибка в репозитории, и молча её чинить нельзя. Иначе сборка
# оставляла бы изменение в рабочей копии, которое обнаружилось бы
# только при следующем коммите — и выглядело бы как чужое.
& uv run python scripts/make_ico.py --check
if ($LASTEXITCODE -ne 0) {
    Fail 'иконка разошлась с исходником — обнови: uv run python scripts/make_ico.py'
}

if ($Clean) {
    Say 'чистка'
    Remove-Item -Recurse -Force 'dist\kurator' -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force 'dist\build' -ErrorAction SilentlyContinue
}

Say 'сборка kurator.exe'
if ($LASTEXITCODE -ne 0) { Fail 'сборка не прошла' }
& uv run --group dev pyinstaller `
    --noconfirm `
    --clean `
    --distpath dist `
    --workpath dist\build `
    packaging\kurator.spec
if ($LASTEXITCODE -ne 0) { Fail 'сборка не прошла' }

if (-not (Test-Path -LiteralPath 'dist\kurator\kurator.exe')) {
    Fail 'kurator.exe не появился — сборка прошла не до конца'
}

$sizeMb = [math]::Round((Get-ChildItem -Recurse 'dist\kurator' |
        Measure-Object -Property Length -Sum).Sum / 1MB)
Say "приложение собрано, $sizeMb МБ"

# Проверяем собранное приложение, а не только факт сборки. Сборка, которая
# создалась, но не открывает страницу или не считает эмбеддинги, считалась
# бы успехом — такое уже случалось дважды, и оба раза приложение выглядело
# исправным до первой индексации.
Say 'проверяю собранное приложение'
& uv run python packaging\verify_bundle.py dist\kurator
if ($LASTEXITCODE -ne 0) { Fail 'собранное приложение не работает — см. выше' }

if ($NoInstaller) {
    Say 'установщик пропущен (-NoInstaller)'
    exit 0
}

Say 'Inno Setup'
$iscc = @(
    "$env:ProgramFiles(x86)\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

if (-not $iscc) {
    # winget есть в Windows 10 1809 и новее. Без него остаётся ручная
    # установка, и об этом лучше сказать прямо, чем упасть позже.
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        Fail 'Inno Setup не найден. Поставь вручную: https://jrsoftware.org/isdl.php'
    }
    Write-Host '    ставлю через winget'
    & winget install --id JRSoftware.InnoSetup --silent --accept-package-agreements `
        --accept-source-agreements
    $iscc = @(
        "$env:ProgramFiles(x86)\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if (-not $iscc) { Fail 'Inno Setup установился, но ISCC.exe не найден' }

Say 'сборка установщика'
& $iscc packaging\installer.iss
if ($LASTEXITCODE -ne 0) { Fail 'Inno Setup не прошёл' }

if (-not (Test-Path -LiteralPath 'dist\kurator-setup.exe')) {
    Fail 'установщик не появился'
}

Say 'готово'
Get-ChildItem 'dist\kurator-setup.exe' |
    ForEach-Object { '    установщик  {0:N0} МБ' -f ($_.Length / 1MB) }
Write-Host '    путь: dist\kurator-setup.exe'