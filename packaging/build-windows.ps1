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

# CI передаёт решение через переменную окружения, а не через аргумент:
# подстановка в `run:` приходит строкой, а параметр-переключатель строку не
# принимает. Поэтому флаг дублируется из NO_INSTALLER.
if ($env:NO_INSTALLER -eq 'true') { $NoInstaller = $true }
if ($env:NO_INSTALLER -eq 'false') { $NoInstaller = $false }

function Say([string]$Message) {
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Fail([string]$Message) {
    Write-Error $Message
    exit 1
}

# Где искать ISCC.exe. Захардкоженные два пути перестали работать:
# winget ставит Inno Setup в папку пользователя, а имя папки у новых
# версий включает номер. Угадывать варианты дальше бессмысленно — ищем
# файл, а список мест поиска печатаем, чтобы следующая неудача была
# понятна сразу, а не после догадок.
$innoSearchRoots = @(
    $env:ProgramFiles,
    ${env:ProgramFiles(x86)},
    "$env:LOCALAPPDATA\Programs"
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }

function Find-InnoCompiler {
    $known = @(
        "$env:ProgramFiles(x86)\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if ($known) { return $known }

    foreach ($root in $innoSearchRoots) {
        $hit = Get-ChildItem -LiteralPath $root -Filter 'ISCC.exe' -Recurse -File `
            -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($hit) {
            Write-Host "    найден: $($hit.FullName)"
            return $hit.FullName
        }
    }
    return $null
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

# Защитник проверяет каждый открытый файл, а в бандле их около тысячи.
# Собранное приложение стартует пятнадцать минут и всё это время молчит:
# процесс жив, сокета нет, след старта не создан — то есть до Python дело
# не доходит. Исключение для каталога сборки снимает ожидание очереди,
# и приложение поднимается за секунды. На нём же проверка: если без
# исключения стартует медленно, а с ним быстро — причина найдена, а не
# угадана.
#
# Исключение действует только на этом раннере и только внутри джоба.
Say 'исключение для защитника на каталог сборки'
try {
    # Каталога может не быть: PyInstaller создаст его сам через минуту.
    New-Item -ItemType Directory -Force -Path 'dist' | Out-Null
    Add-MpPreference -ExclusionPath (Resolve-Path 'dist').Path
    Write-Host '    dist исключён из проверки'
} catch {
    Write-Host "    не вышло: $($_.Exception.Message) — проверка пойдёт без этого"
}

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

# Второй, консольный вариант — только ради проверки. Поставляться он не
# будет: у пользователя при запуске мелькнуло бы чёрное окно.
#
# Смысл в том, что оконное приложение при падении показывает диалог
# Windows. В раннере без рабочего стола закрывать некому, и вместо
# ошибки получается процесс, который живёт и молчит: разбираться не с
# чем, а проверка утверждает, что приложение сломано. Консольный
# вариант печатает ту же ошибку текстом.
Say 'сборка проверочного консольного варианта'
$env:KURATOR_CONSOLE = '1'
& uv run --group dev pyinstaller `
    --noconfirm `
    --clean `
    --distpath dist-console `
    --workpath dist\build-console `
    packaging\kurator.spec
Remove-Item Env:\KURATOR_CONSOLE
if ($LASTEXITCODE -ne 0) { Fail 'сборка проверочного варианта не прошла' }

# Проверяем собранное приложение, а не только факт сборки. Сборка, которая
# создалась, но не открывает страницу или не считает эмбеддинги, считалась
# бы успехом — такое уже случалось дважды, и оба раза приложение выглядело
# исправным до первой индексации.
#
# Порядок неудобный, но единственно полезный: сначала консольный вариант.
# Оконный при поломке молчит пятнадцать минут и не говорит ничего, и,
# проверенный первым, он съел бы всё время, не дав дойти до причины.
Say 'проверяю проверочный консольный вариант'
& uv run python packaging\verify_bundle.py dist-console\kurator
if ($LASTEXITCODE -ne 0) {
    Fail 'проверочный вариант не работает — причина в выводе выше'
}

Say 'проверяю kurator.exe'
& uv run python packaging\verify_bundle.py dist\kurator
if ($LASTEXITCODE -ne 0) { Fail 'собранное приложение не работает — см. выше' }

if ($NoInstaller) {
    Say 'установщик пропущен (-NoInstaller)'
    exit 0
}

Say 'Inno Setup'
$iscc = Find-InnoCompiler

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
    $iscc = Find-InnoCompiler
}
if (-not $iscc) {
    Fail ('Inno Setup поставился, но ISCC.exe не найден. Искали в: ' +
        ($innoSearchRoots -join ', '))
}

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