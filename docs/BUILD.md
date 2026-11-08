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

环境准备成功后自动安装“发送到”入口，管理命令见 [README](../README.md#字幕与交互)。

依赖维护：

```powershell
# 修改 requirements.in 后生成锁文件，核对版本和哈希
uv pip compile requirements.in --python .venv\Scripts\python.exe --generate-hashes -o requirements.lock
uv pip sync requirements.lock --require-hashes --python .venv\Scripts\python.exe
```

离线回归：

```powershell
.\scripts\test.ps1
```

测试用模拟 HTTP、真实 SQLite、合成媒体和 Windows 子进程验证行为，不使用真实凭据。CLI 诊断见[本机检查](../README.md#本机检查)。

`scripts/transcribe.ps1` 保留调用者当前目录，相对媒体与报告路径按调用位置解释。运行数据及恢复资料见[数据与恢复](API_CONTRACT.md#数据与恢复)。

运行时连接常驻 magpie 网关，配置见 [README](../README.md#配置-magpie)。升级 magpie 后核对原生 POST 单次转发、组费用隔离及会话关联；可用合成短句和只读上传策略请求检查接入。真实长视频识别与人工字幕质量另行验证。
