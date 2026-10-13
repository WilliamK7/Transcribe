# 百炼命令行字幕工具

Windows x64 本地 CLI，版本 0.2.0。使用北京工作空间的 `qwen3-asr-flash-filetrans` 自动识别语音，使用 `qwen-mt-flash` 翻译为简体中文。

## 开始使用

在可写项目目录中打开 PowerShell：

```powershell
.\scripts\setup.ps1
.\scripts\transcribe.ps1 --configure
.\scripts\transcribe.ps1 "D:\Videos\课程.mkv"
```

准备环境使用 Python 3.12.10 x64、哈希锁定依赖和项目 FFmpeg。已有对应 Python 可使用 `setup.ps1 -PythonPath "C:\Python312\python.exe"`。

无参数或 `--help` 显示帮助。也可运行 `.\.venv\Scripts\python.exe -m subtitle_tool.main`。多个文件顺序处理，MT 默认并发 2，请求开始间隔至少 1.05 秒。

`--configure` 填写北京 Workspace ID 并隐藏输入 Key；`--configure-key` 只更新 Key。Workspace ID 是 1～63 位字母、数字或内部连字符组成的域名标签。`--workspace-id ID` 可为本次新任务指定工作空间。

Key **明文保存到 `data/key.txt`**，环境变量 `DASHSCOPE_API_KEY` 优先。`data` 还包含上传引用、转写和任务缓存，应留在本机。旧 `key.dpapi` 成功解密并原子写入新文件后移除；解密失败可运行 `--configure-key` 重新输入。

## 字幕与音频

- 默认输出视频同目录的 `视频名.srt`，只替换最终扩展名。`--output-dir` 可指定目录。
- 外语原文在上、简体中文译文在下；中文／粤语保留原文并跳过 MT。无语音时完成任务且不生成字幕。
- 已有 SRT 默认跳过；`--overwrite` 先备份再覆盖。同名不同扩展视频的目标冲突需要调整文件名或输出目录。
- 优先复制 AAC→M4A、MP3→MP3、FLAC→FLAC、Opus／Vorbis→OGG；需要声道处理或时间轴校正时转换为 16kHz 单声道 FLAC。
- 保留音轨偏移、静音和编码延迟；超过 12 小时或上传容量时按 PCM 样本精确分段。上传容量取 1 GB 与服务返回限额中的较小值。

## 复核与任务管理

音轨、声道或语种存在歧义时，终端会要求确认。语种复核逐条显示原文，可选择保留或翻译；手动语言只影响 MT。

```powershell
.\scripts\transcribe.ps1 --non-interactive --channel mix "D:\Videos\课程.mkv"
.\scripts\transcribe.ps1 --jobs
.\scripts\transcribe.ps1 --review 本地任务ID
.\scripts\transcribe.ps1 --resume 本地任务ID
.\scripts\transcribe.ps1 --resume 本地任务ID --track 音轨索引 --channel right
.\scripts\transcribe.ps1 --cancel 本地任务ID
```

`--channel left` 选择首声道，`right` 选择第二声道，`mix` 混音。`--track` 使用 FFprobe 音轨索引。恢复时可重选尚未提交任务的音轨／声道，已提交的识别输入固定。

`--non-interactive` 或重定向输入时，歧义保存为 `needs_review` 并以退出码 2 结束。待复核或未完成的字幕暂不输出。

任务库由单个进程独占。运行中按 Ctrl+C 保存状态并停止本地处理，网络请求结束或超时前可能需要等待；已提交云端任务仍可能计费。`--cancel` 停止已保存任务，之后用 `--resume` 显式恢复。

恢复复用已知云端 ID、本地转写和成功译文，仅补缺失项。任务绑定原账号、工作空间、源文件指纹和处理方案；旧北京任务沿用公共端点，旧新加坡任务保留数据但不支持恢复。

提交超时或响应缺少 ID 时，任务保存为 `submission_unknown`。可绑定已有 ID，或明确接受可能重复计费后重发：

```powershell
.\scripts\transcribe.ps1 --resume 本地任务ID --bind-task-id 云端task_id
.\scripts\transcribe.ps1 --resume 本地任务ID --allow-resubmit
```

进度写入 stderr，任务结果以每行 JSON 写入 stdout；`--report "结果.json"` 另存 JSON 数组。报告包含任务状态和调用计数，省略 Key、上传链接及转写文字。

退出码：`0` 完成／跳过或管理操作成功；`2` 待复核／任务失败；`3` 启动／本地异常；`130` Ctrl+C。

## 本机检查

```powershell
.\scripts\transcribe.ps1 --self-test --report "本机诊断.json"
.\scripts\test.ps1
```

诊断使用隔离目录和合成音频；回归测试使用模拟云端响应、真实 FFmpeg 和 Windows 进程锁。真实北京工作空间上传→ASR→MT、真实纯中文零 MT、识别与译文质量、播放器同步仍待验证。

[环境维护](docs/BUILD.md) · [接口与恢复契约](docs/API_CONTRACT.md) · [第三方组件](docs/THIRD_PARTY.txt) · [源码许可](LICENSE)
