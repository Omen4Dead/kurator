; Inno Setup — установщик kurator для Windows.
;
; Собирает один kurator.exe, который ставит kurator в «Программы», создаёт
; ярлык и добавляет пункт в меню «Пуск». Ничего не спрашивает при запуске,
; кроме пути установки, и не лезет в сеть.
;
; Отдельно этот файл не запускается: его собирает packaging/build-windows.ps1.
; Читать стоит при смене имени папки, иконки или пунктов меню — больше
; тут нечего менять.

#define AppName "kurator"
#define AppVersion "0.3.0"
#define AppPublisher "Omen4Dead"
#define AppURL "https://github.com/Omen4Dead/kurator"
#define AppExeName "kurator.exe"

[Setup]
AppId={7B3E9F21-4C8D-4A16-9E5B-2D7F1C0A8834}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}

; Куда ставить по умолчанию. `{autopf}` — это «Программы», а туда без
; прав администратора не попасть, поэтому установщик их запрашивает
; (PrivilegesRequired в Inno Setup по умолчанию admin, и здесь он не
; переопределён). Раньше здесь стояло обоснование, что приложение ничего
; не пишет в свою папку и потому права не нужны: это верно про само
; приложение и неверно про установку.
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Относительные пути Inno Setup считает ОТ КАТАЛОГА ЭТОГО ФАЙЛА, а не от
; рабочего каталога. Скрипт сборки запускается из корня репозитория, и
; без `..\` установщик искал значок в packaging\assets, где его нет, и
; писал себя в packaging\dist вместо dist. Все три пути ниже — с `..\`.
OutputDir=..\dist
OutputBaseFilename=kurator-setup
SetupIconFile=..\assets\kurator.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma/max
SolidCompression=yes

; Один файл установщика, без внешних архивов: логика и полезная нагрузка
; должны лежать в kurator.exe, всё остальное — данные, которые он читает.
WizardStyle=modern

[Languages]
Name: "russian"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать значок на рабочем столе"; \
    GroupDescription: "Дополнительно:"; Flags: unchecked

[Files]
; Всё содержимое dist\kurator — это собранное приложение: интерпретатор,
; onnxruntime, модель на 90 МБ и три файла веб-интерфейса.
Source: "..\dist\kurator\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Удалить {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Запустить {#AppName}"; \
    Flags: nowait postinstall skipifsilent