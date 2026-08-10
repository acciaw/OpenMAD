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
; In-app updating runs this installer with /SILENT while OpenMAD is still
; shutting down. Windows will not overwrite a running executable, so let the
; Restart Manager close any instance still holding it rather than failing the
; file copy -- with /SUPPRESSMSGBOXES there would be no prompt to answer.
; RestartApplications is off because the [Run] entry below does the relaunch,
; and both would start a second copy.
; `force`, not `yes`. With `yes` Inno *asks* before closing an application that
; holds one of its files -- and a silent run started by the in-app updater
; cannot show that prompt, so with /SUPPRESSMSGBOXES it resolved to "cancel".
; The log read "User canceled the installation process" followed by a rollback,
; a second after starting, with nobody having cancelled anything. That was the
; silent update failure. `force` closes the application instead of asking.
CloseApplications=force
RestartApplications=no

[Files]
; The whole one-folder build: OpenMAD.exe and the _internal folder beside it.
; PyInstaller produces a directory rather than a single file on purpose -- see
; the note at the top of mad68.spec -- so this copies the tree, not one file.
Source: "..\dist\{#AppName}\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs
; version.ini is bundled inside _internal too, but the app looks beside the
; executable first (mad68.version._locate) so that the number tracks the
; release being installed rather than whenever the exe happened to be built.
Source: "..\version.ini"; DestDir: "{app}"; Flags: ignoreversion
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
; Only the interactive entry above lives here: a tick-box on the final wizard
; page, which a silent run never shows -- that is what `skipifsilent` means.
;
; A silent run is what the in-app updater does, and it deliberately gets no
; [Run] entry. There used to be one (`Flags: nowait; Check: WizardSilent`) and
; it started the new build six milliseconds after this installer finished
; writing it, which was too soon: the PyInstaller onefile bootloader failed
; part-way through unpacking itself and died with "Failed to load Python DLL
; ... python313.dll". The updater now restarts the app itself, once this
; installer has fully exited and after a pause -- see _RESTART_SETTLE_S in
; src/mad68/updater.py.
