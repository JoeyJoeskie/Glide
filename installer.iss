; Glide installer (Inno Setup)
#define MyAppName "Glide"
#define MyAppVersion "0.9.3"
#define MyAppPublisher "Glide (open source)"
#define MyAppURL "https://github.com/JoeyJoeskie/Glide"
#define MyAppExeName "glide.exe"

[Setup]
AppId={{F3A9C1E2-7B4D-4E5A-9C6F-0A1B2C3D4E5F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
DefaultDirName={autopf}\Glide
DefaultGroupName=Glide
DisableProgramGroupPage=yes
LicenseFile=LICENSE
OutputDir=installer_out
OutputBaseFilename=GlideSetup
SetupIconFile=assets\glide.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"
Name: "startupicon"; Description: "Start Glide when Windows starts"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "dist\glide.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Glide"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\Glide"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userstartup}\Glide"; Filename: "{app}\{#MyAppExeName}"; Tasks: startupicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Glide now"; Flags: nowait postinstall skipifsilent
