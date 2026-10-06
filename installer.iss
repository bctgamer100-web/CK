; Inno Setup script: builds dist\CookieRunBot-Setup.exe from build\app\CookieRunBot (run build_app.bat first).
; Installs per user (no admin needed) so the app can save its settings next to the exe.

#define AppName "CookieRunBot"
#define AppVersion "1.1.0"

[Setup]
AppId={{8E3B6C1A-5D2F-4B7E-9A41-C00C1E20B071}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
UsePreviousTasks=no
OutputDir=dist
OutputBaseFilename=CookieRunBot-Setup
SetupIconFile=app.ico
UninstallDisplayIcon={app}\CookieRunBot.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; personal data created by running the app from the build folder is never shipped
Source: "build\app\CookieRunBot\*"; DestDir: "{app}"; Excludes: "web-config.json,stats-history.json,\results\*,.send_hearts_now"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\CookieRunBot.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\CookieRunBot.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\CookieRunBot.exe"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: files; Name: "{app}\web-config.json"
Type: filesandordirs; Name: "{localappdata}\{#AppName}"
