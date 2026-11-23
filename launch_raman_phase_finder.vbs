Option Explicit

Dim shell, files, root, python, setup, args, argument, result
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
python = shell.ExpandEnvironmentStrings("%LocalAppData%\Sci\env\Scripts\pythonw.exe")

If WScript.Arguments.Count = 1 Then
    If WScript.Arguments(0) = "--register-user" Then
        RegisterUser
        WScript.Quit
    End If
    If WScript.Arguments(0) = "--register-install" Then
        UpdateLegacyShortcuts
        RegisterUser
        WScript.Quit
    End If
End If

If Not files.FileExists(python) Then
    setup = Quote(root & "\setup_env.bat")
    result = shell.Run(Quote(shell.ExpandEnvironmentStrings("%ComSpec%")) & " /d /c " & Quote(setup), 1, True)
    If result <> 0 Then WScript.Quit result
    If Not files.FileExists(python) Then
        MsgBox "Sci Python is unavailable. Run Update Sci Environment from the Start menu.", vbExclamation, "Raman Phase Finder"
        WScript.Quit 1
    End If
End If

args = ""
For Each argument In WScript.Arguments
    args = args & " " & Quote(argument)
Next
shell.Run Quote(python) & " " & Quote(root & "\toolkit\launch_raman_phase_finder.pyw") & args, 1, False

Sub UpdateLegacyShortcuts()
    Dim oldRoot, oldRoots, programs, launcher
    oldRoots = Array(shell.ExpandEnvironmentStrings("%LocalAppData%\Programs\Raman_Phase_Finder"), _
                     files.BuildPath(files.GetParentFolderName(root), "Raman"))
    programs = shell.SpecialFolders("Programs") & "\Raman Phase Finder"
    launcher = Quote(root & "\launch_raman_phase_finder.vbs")
    For Each oldRoot In oldRoots
        UpdateShortcut shell.SpecialFolders("Desktop") & "\Raman Phase Finder.lnk", oldRoot, launcher
        UpdateShortcut programs & "\Raman Phase Finder.lnk", oldRoot, launcher
        UpdateShortcut programs & "\Update Sci Environment.lnk", oldRoot, launcher
    Next
End Sub

Sub RegisterUser()
    Dim bin, command, launcher
    launcher = Quote(root & "\launch_raman_phase_finder.vbs")
    bin = shell.ExpandEnvironmentStrings("%LocalAppData%\Sci\bin")
    If files.FolderExists(bin) Then
        Set command = files.CreateTextFile(bin & "\raman-phase-finder.cmd", True)
        command.WriteLine "@echo off"
        command.WriteLine "start """" ""%SystemRoot%\System32\wscript.exe"" " & launcher & " %*"
        command.Close
        If Not files.FileExists(bin & "\raman.cmd") Then
            Set command = files.CreateTextFile(bin & "\raman.cmd", False)
            command.WriteLine "@echo off"
            command.WriteLine "call ""%~dp0raman-phase-finder.cmd"" %*"
            command.Close
        End If
    End If
End Sub

Sub UpdateShortcut(path, oldRoot, launcher)
    Dim link
    If Not files.FileExists(path) Then Exit Sub
    Set link = shell.CreateShortcut(path)
    If StrComp(link.Arguments, Quote(oldRoot & "\launch_raman_phase_finder.vbs"), vbTextCompare) = 0 Then
        If StrComp(files.GetFileName(link.TargetPath), "wscript.exe", vbTextCompare) <> 0 Then Exit Sub
        link.Arguments = launcher
        link.IconLocation = root & "\icon.ico,0"
    ElseIf StrComp(link.TargetPath, oldRoot & "\setup_env.bat", vbTextCompare) = 0 Then
        link.TargetPath = root & "\setup_env.bat"
    Else
        Exit Sub
    End If
    link.WorkingDirectory = root
    link.Save
End Sub

Function Quote(value)
    Quote = Chr(34) & value & Chr(34)
End Function
