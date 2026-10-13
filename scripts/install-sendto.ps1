param([switch]$Remove, [string]$SendToDirectory = '')
$ErrorActionPreference = 'Stop'
$failureMessage = '无法安装或卸载“发送到”入口，请检查目录权限。'
try {
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    $root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
    if (-not $SendToDirectory) { $SendToDirectory = [Environment]::GetFolderPath('SendTo') }
    if (-not $SendToDirectory) { throw 'SendTo directory unavailable.' }
    $SendToDirectory = [IO.Path]::GetFullPath($SendToDirectory)
    $linkPath = Join-Path $SendToDirectory '!百炼字幕.lnk'
    $powershell = Join-Path ([Environment]::GetFolderPath('System')) 'WindowsPowerShell\v1.0\powershell.exe'
    $owner = 'Bailian Subtitle CLI SendTo v1'
    $shell = New-Object -ComObject WScript.Shell
    if (Test-Path -LiteralPath $linkPath) {
        $failureMessage = '同名“!百炼字幕.lnk”不属于本工具，已保留；请先处理冲突。'
        if (-not (Test-Path -LiteralPath $linkPath -PathType Leaf)) { throw 'Shortcut name conflict.' }
        $link = $shell.CreateShortcut($linkPath)
        if ($link.Description -ne $owner) { throw 'Shortcut name conflict.' }
    }
    $failureMessage = '无法安装或卸载“发送到”入口，请检查目录权限。'
    if ($Remove) {
        if (Test-Path -LiteralPath $linkPath) {
            Remove-Item -LiteralPath $linkPath
            Write-Output '已卸载“发送到 → !百炼字幕”。'
        } else {
            Write-Output '“发送到 → !百炼字幕”未安装，无需卸载。'
        }
    } else {
        $failureMessage = '缺少 scripts\sendto.ps1，请恢复项目脚本后重新安装。'
        $entry = Join-Path $PSScriptRoot 'sendto.ps1'
        if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) { throw 'SendTo script missing.' }
        $failureMessage = '无法安装“发送到”入口，请检查目录权限和系统 Windows PowerShell。'
        if (-not (Test-Path -LiteralPath $powershell -PathType Leaf)) { throw 'Windows PowerShell missing.' }
        [IO.Directory]::CreateDirectory($SendToDirectory) | Out-Null
        $link = $shell.CreateShortcut($linkPath)
        $link.TargetPath = $powershell
        $link.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $entry + '"'
        $link.WorkingDirectory = $root
        $link.WindowStyle = 1
        $link.Description = $owner
        $link.IconLocation = $powershell + ',0'
        $link.Save()
        Write-Output '已安装“发送到 → !百炼字幕”。'
    }
} catch {
    [Console]::Error.WriteLine($failureMessage)
    exit 3
}
exit 0
