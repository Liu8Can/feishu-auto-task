#define AppName "飞书动态下班打卡助手"
#define AppVersion "1.0.0"
#define AppExeName "FeishuClockoutAssistant.exe"

[Setup]
AppId={{A47E6172-BCB3-48DB-A0E1-B491F4E1A969}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\FeishuClockoutAssistant
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist-installer
OutputBaseFilename=FeishuClockoutAssistant-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#AppExeName}

[Files]
Source: "..\dist\FeishuClockoutAssistant\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: unchecked

[Run]
Filename: "{app}\{#AppExeName}"; Description: "启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "FeishuClockoutAssistant"; ValueData: "{quote}{app}\{#AppExeName}{quote} --startup"; Flags: uninsdeletevalue
