; SPDX-License-Identifier: AGPL-3.0-or-later
; Copyright © 2026 ca-works
;
; Per-user installer for ENG Drawing Viewer (Inno Setup 6). Built by build_windows.ps1 -Installer:
;   iscc /DAppVersion=<version> /DSourceDir=<app folder> /DOutputDir=<folder> /DOutputName=<name> /DRepoRoot=<repo> installer.iss
;
; Installs into %LOCALAPPDATA%\Programs\ENG Drawing Viewer without administrator rights, adds a
; Start menu shortcut and "Open with" entries for PDFs and images (it never takes over the default
; app), and registers an uninstaller under Settings > Apps. Settings in %APPDATA% are kept on
; uninstall.

#ifndef AppVersion
  #error AppVersion is required
#endif

#define AppName "ENG Drawing Viewer"
#define AppExe "ENG Drawing Viewer.exe"
#define AppUserModelID "ca-works.ENGDrawingViewer"
#define ProgId "ENGDrawingViewer.Document"

[Setup]
AppId={{6B0F3E71-2C55-4F0B-9E7A-5D8C4A1E2B93}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=ca-works
AppPublisherURL=https://ca-works.github.io/eng-drw/
AppSupportURL=https://github.com/ca-works/eng-drawing-viewer/issues
AppCopyright=Copyright (C) 2026 ca-works
VersionInfoVersion={#AppVersion}
VersionInfoCompany=ca-works
VersionInfoDescription={#AppName} Setup
PrivilegesRequired=lowest
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}
SetupIconFile={#RepoRoot}\src\eng_viewer\resources\ENG_Drawing_Viewer.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
LicenseFile={#RepoRoot}\LICENSE
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
ChangesAssociations=yes
CloseApplications=yes
RestartApplications=no
TimeStampsInUTC=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; An update replaces the application files completely.
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\legal"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "{#AppUserModelID}"; Comment: "View, mark up and convert engineering drawings"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "{#AppUserModelID}"; Tasks: desktopicon

[Registry]
; "Open with" for PDFs and images, for this user only. The default app for each type is unchanged.
Root: HKCU; Subkey: "Software\Classes\{#ProgId}"; ValueType: string; ValueData: "Drawing (ENG Drawing Viewer)"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\{#ProgId}"; ValueName: "AppUserModelID"; ValueType: string; ValueData: "{#AppUserModelID}"
Root: HKCU; Subkey: "Software\Classes\{#ProgId}\DefaultIcon"; ValueType: string; ValueData: """{app}\{#AppExe}"",0"
Root: HKCU; Subkey: "Software\Classes\{#ProgId}\shell\open\command"; ValueType: string; ValueData: """{app}\{#AppExe}"" ""%1"""
Root: HKCU; Subkey: "Software\Classes\Applications\{#AppExe}"; ValueName: "FriendlyAppName"; ValueType: string; ValueData: "{#AppName}"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\Applications\{#AppExe}\shell\open\command"; ValueType: string; ValueData: """{app}\{#AppExe}"" ""%1"""
#define Ext(e) 'Root: HKCU; Subkey: "Software\Classes\' + e + '\OpenWithProgids"; ValueName: "' + ProgId + '"; ValueType: string; ValueData: ""; Flags: uninsdeletevalue' + NewLine + 'Root: HKCU; Subkey: "Software\Classes\Applications\' + AppExe + '\SupportedTypes"; ValueName: "' + e + '"; ValueType: string; ValueData: ""'
#emit Ext(".pdf")
#emit Ext(".png")
#emit Ext(".jpg")
#emit Ext(".jpeg")
#emit Ext(".tif")
#emit Ext(".tiff")
#emit Ext(".bmp")
#emit Ext(".gif")
#emit Ext(".webp")
#emit Ext(".heic")
#emit Ext(".heif")

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
