# 环境准备与维护

在 Windows x64 的可写项目目录，用 PowerShell 执行：

```powershell
.\scripts\setup.ps1
# 使用已有 Python 3.12.10 x64
.\scripts\setup.ps1 -PythonPath "C:\Python312\python.exe"
```

脚本优先复用 `.venv`，检查 Python 3.12.10 和 64 位架构。缺少解释器时，通过 uv 下载到 `.build/python`，使用 `--no-bin --no-registry` 保持系统路径与注册表不变。

依赖统一从 `requirements.lock` 按完整版本和哈希安装。有 uv 时使用 `uv pip sync`，否则使用 `pip install --require-hashes`。已有环境可传 `-SkipDependencyInstall`，仍会检查解释器和媒体资产。

`scripts/prepare_assets.py` 按 `scripts/release-assets.json` 下载并校验 FFmpeg 归档，只提取 `ffmpeg.exe`、`ffprobe.exe`、`LICENSE` 和 `README.txt` 到 `tools`。程序使用这些本地工具的绝对路径。

环境准备成功后自动安装“发送到”入口，管理命令见 [README](../README.md#右键发送到)。

依赖维护：

```powershell
# 修改 requirements.in 后生成锁文件，核对版本和哈希
uv pip compile requirements.in --python .venv\Scripts\python.exe --generate-hashes -o requirements.lock
uv pip sync requirements.lock --require-hashes --python .venv\Scripts\python.exe
```

本机验证：

```powershell
.\scripts\test.ps1
.\scripts\transcribe.ps1 --self-test --report ".build\self-test.json"
```

测试用模拟 HTTP、真实 SQLite、合成媒体和 Windows 子进程验证行为；诊断使用隔离目录及假 Key，零云端调用。实际结果以命令退出码和输出为准。

`scripts/transcribe.ps1` 保留调用者当前目录，相对媒体与报告路径按调用位置解释。运行数据位于项目 `data`，成功任务清理音频中间文件，保留任务库、转写和规范化字幕供恢复使用。
