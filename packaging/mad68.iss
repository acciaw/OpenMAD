; Inno Setup script for the OpenMAD installer.
;
; Build the executable first, then compile this:
;     pyinstaller packaging/mad68.spec
;     iscc packaging/mad68.iss
;
; Output: packaging/Output/OpenMAD-Setup-<version>.exe
;
; The app needs no driver and no admin rights at runtime -- it talks to the
; keyboard over raw HID, which Windows exposes to ordinary user processes. So
; this installs per-user and never asks for elevation.

; Identity comes from version.ini at the repository root, the same file the app
; reads at runtime. ReadIni runs in the preprocessor, at compile time, so the
; values are baked into the installer. Nothing here needs editing per release.
#define VerFile   AddBackslash(SourcePath) + "..\version.ini"
#define AppName    ReadIni(VerFile, "app", "name", "OpenMAD")
#define AppVersion ReadIni(VerFile, "app", "version", "0.0.0")
#define Publisher  ReadIni(VerFile, "app", "publisher", "")
#define AppUrl     ReadIni(VerFile, "app", "url", "")
#define AppExe     AppName + ".exe"

[Setup]
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#Publisher}
AppPublisherURL={#AppUrl}
AppSupportURL={#AppUrl}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
OutputBaseFilename={#AppName}-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Per-user install, so no UAC prompt.
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#AppExe}

[Files]
Source: "..\dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\version.ini";     DestDir: "{app}"; Flags: ignoreversion

[Files]
; Branding, if it has been supplied. `skipifsourcedoesntexist` keeps the build
; working before the artwork exists.
Source: "..\assets\logo.png"; DestDir: "{app}\assets"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\assets\icon.ico"; DestDir: "{app}\assets"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\{#AppName}";       Filename: "{app}\{#AppExe}"; Tasks: startmenuicon
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: startup

; Every task is checked by default. Inno checks a task unless its Description
; carries the `unchecked` flag, so simply listing them gives the wizard page the
; user sees at install time, all pre-ticked.
[Tasks]
Name: "startup";       Description: "Start automatically when I sign in"
Name: "desktopicon";   Description: "Create a desktop shortcut"
Name: "startmenuicon"; Description: "Create a Start-menu shortcut"
Name: "autoupdate";    Description: "Check for updates automatically"

[Registry]
; Mirrors the settings the app reads. Removed on uninstall so nothing is left
; pointing at a program that is gone.
Root: HKCU; Subkey: "Software\{#AppName}"; ValueType: dword; ValueName: "CheckUpdates"; \
    ValueData: "1"; Tasks: autoupdate; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\{#AppName}"; ValueType: dword; ValueName: "CheckUpdates"; \
    ValueData: "0"; Check: not WizardIsTaskSelected('autoupdate'); Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
    ValueName: "{#AppName}"; ValueData: """{app}\{#AppExe}"""; \
    Tasks: startup; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName}"; Flags: nowait postinstall skipifsilent
