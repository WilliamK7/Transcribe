$ErrorActionPreference = 'Stop'
$cliExitCode = 3
$failureMessage = '字幕工具启动失败，请检查项目环境并运行 scripts\setup.ps1。'
try {
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    if ($args.Count -eq 0) {
        $cliExitCode = 2
        $failureMessage = '请选择视频或音频文件，再使用“发送到 → !百炼字幕”。'
        throw 'No files supplied.'
    }
    $cliExitCode = 2
    $failureMessage = '请选择现有的视频或音频文件。'
    $files = @(
        foreach ($source in $args) {
            $path = [IO.Path]::GetFullPath([string]$source)
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw 'Invalid file.' }
            $path
        }
    )
    $cliExitCode = 3
    $failureMessage = '字幕工具启动失败，请检查项目环境并运行 scripts\setup.ps1。'
    $launcher = Join-Path $PSScriptRoot 'transcribe.ps1'
    # Inherit the console and pass each file separately; -- keeps filenames out of CLI options.
    & $launcher '--' @files
    $cliExitCode = $LASTEXITCODE
} catch {
    [Console]::Error.WriteLine($failureMessage)
}
if ($cliExitCode -ne 0) {
    [Console]::WriteLine("处理未完成（退出码 $cliExitCode）。按回车关闭窗口。")
    try { Read-Host | Out-Null } catch {}
}
exit $cliExitCode
