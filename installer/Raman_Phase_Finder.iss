#ifndef AppVersion
  #define AppVersion "1.0.1"
#endif
#define Launcher "launch_raman_phase_finder.vbs"

[Setup]
AppId={{F6C66467-457D-4F4F-AB11-4D22B2258B15}
AppName=Raman Phase Finder
AppVersion={#AppVersion}
AppPublisher=ABKuznetsov
AppPublisherURL=https://github.com/ABKuznetsov/Raman_Phase_Finder
AppUpdatesURL=https://github.com/ABKuznetsov/Raman_Phase_Finder/releases
DefaultDirName={autopf}\Raman Phase Finder
DefaultGroupName=Raman Phase Finder
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
UsePreviousAppDir=no
UsePreviousGroup=no
DisableDirPage=yes
DisableProgramGroupPage=yes
OutputDir=output
OutputBaseFilename=Raman_Phase_Finder_Setup_{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\icon.ico
UninstallDisplayIcon={app}\icon.ico
VersionInfoVersion={#AppVersion}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"
Name: "setupenv"; Description: "Prepare or update the shared Sci Python environment"

[Files]
Source: "..\Vibrational_Finder\*"; DestDir: "{app}\Vibrational_Finder"; Flags: ignoreversion recursesubdirs; Excludes: "__pycache__,*.pyc,*.pyo,*.egg-info"
Source: "..\toolkit\*"; DestDir: "{app}\toolkit"; Flags: ignoreversion recursesubdirs; Excludes: "__pycache__,*.pyc,*.pyo,*.command,\updates\*"
Source: "..\*.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\{#Launcher}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\icon.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\icon.png"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\pyproject.toml"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\reference_template.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Raman Phase Finder"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\{#Launcher}"""; WorkingDir: "{app}"; IconFilename: "{app}\icon.ico"
Name: "{autodesktop}\Raman Phase Finder"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\{#Launcher}"""; WorkingDir: "{app}"; IconFilename: "{app}\icon.ico"; Tasks: desktopicon
Name: "{group}\Update Sci Environment"; Filename: "{app}\setup_env.bat"; WorkingDir: "{app}"
Name: "{group}\Uninstall Raman Phase Finder"; Filename: "{uninstallexe}"

[Run]
Filename: "{sys}\wscript.exe"; Parameters: """{app}\{#Launcher}"" --register-install"; WorkingDir: "{app}"; Flags: runasoriginaluser
Filename: "{cmd}"; Parameters: "/d /c """"{app}\setup_env.bat"""""; WorkingDir: "{app}"; Description: "Prepare Sci environment in Command Prompt"; Flags: postinstall skipifsilent waituntilterminated runasoriginaluser; Tasks: setupenv
