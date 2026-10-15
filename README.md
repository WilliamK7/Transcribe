# 百炼命令行字幕工具

Windows x64 本地 CLI。使用北京工作空间的 `qwen3-asr-flash-filetrans` 自动识别语音，使用 `qwen-mt-flash` 翻译为简体中文。

## 开始使用

在可写项目目录中打开 PowerShell：

```powershell
.\scripts\setup.ps1
.\scripts\transcribe.ps1 --configure
.\scripts\transcribe.ps1 "D:\Videos\课程.mkv"
```

准备环境使用 Python 3.12.10 x64、哈希锁定依赖和项目 FFmpeg。已有对应 Python 可使用 `setup.ps1 -PythonPath "C:\Python312\python.exe"`。

无参数或 `--help` 显示帮助。也可运行 `.\.venv\Scripts\python.exe -m subtitle_tool.main`。多个文件顺序处理。

`--configure` 填写北京 Workspace ID 并隐藏输入 Key；`--configure-key` 只更新 Key。`--workspace-id ID` 可为本次新任务指定工作空间。

Key **明文保存到 `data/key.txt`**，环境变量 `DASHSCOPE_API_KEY` 优先。`data` 还包含上传引用、转写和任务缓存，应留在本机。

## 右键“发送到”

`setup.ps1` 环境准备成功后，会为当前用户安装“!百炼字幕”入口。在资源管理器中选择一个或多个视频／音频文件，右键 → 发送到 → !百炼字幕；Windows 11 可先点“显示更多选项”。文件依次处理，字幕默认保存在源文件同目录，已有 SRT 默认跳过。

窗口支持音轨、声道和语种复核。全部成功或跳过后自动关闭；失败、暂停复核或停止处理时保留提示，按回车关闭。任务恢复见下方 CLI 命令。

已有环境可单独安装／更新或卸载入口：

```powershell
.\scripts\install-sendto.ps1
.\scripts\install-sendto.ps1 -Remove
```

项目移动后，从新位置重新运行安装脚本。

## 字幕与音频

- 默认输出视频同目录的 `视频名.srt`，只替换最终扩展名。`--output-dir` 按调用位置解析，恢复时沿用保存的绝对路径。
- 外语原文在上、简体中文译文在下；中文／粤语保留原文并跳过 MT。无语音时完成任务且不生成字幕。
- 先翻译完整句作为参考，整句字幕直接复用参考译文，拆分片段结合所属句和相邻句的参考翻译。同批相同输入和上下文共享请求，已有译文按请求内容缓存复用。
- 翻译默认 4 并发，短任务无固定逐条间隔；按模型额度在 61 秒滚动窗口内最多发送 60 次，额度用尽时等待释放。真实限流仍共享退避并逐步提速，进度会区分配额等待与服务端限流。
- 原文、译文各以两行为目标，短句保持一行；保留完整单词和标点，平衡英文尾行。实际显示也受播放器字体和窗口影响。
- 文字过多或超过 7 秒时，按可靠词时间拆分，新片段至少 1 秒。零时长词与相邻词组合后参与分段。无法继续拆分时保留完整内容和已有时间，在报告中记录排版超限警告。
- 已有 SRT 默认跳过；`--overwrite` 先备份再覆盖。同名不同扩展视频的目标冲突需要调整文件名或输出目录。
- 优先复制 AAC→M4A、MP3→MP3、FLAC→FLAC、Opus／Vorbis→OGG；需要声道处理或时间轴校正时转换为 16kHz 单声道 FLAC。
- 保留音轨偏移、静音和编码延迟；超过 12 小时或上传容量时按 PCM 样本精确分段。上传容量取 1 GB 与服务返回限额中的较小值。

可在 `data/settings.json` 调整以下设置；已有任务的排版设置在重排时更新。

| 设置 | 默认值 | 含义 |
| --- | --- | --- |
| `mt_workers` | `4` | 翻译并发数，1～4 |
| `zh_line_width` | `22` | 中文每行宽度，12～36 |
| `other_line_width` | `42` | 其他语种每行宽度，24～80 |
| `chinese_first` | `false` | 译文是否排在原文上方 |

## 复核与任务管理

音轨、声道或语种存在歧义时，终端会要求确认。语种复核逐条显示原文，可选择保留或翻译；手动语言只影响 MT。

```powershell
.\scripts\transcribe.ps1 --non-interactive --channel mix "D:\Videos\课程.mkv"
.\scripts\transcribe.ps1 --jobs
.\scripts\transcribe.ps1 --review 本地任务ID
.\scripts\transcribe.ps1 --resume 本地任务ID
.\scripts\transcribe.ps1 --reflow 本地任务ID --overwrite
.\scripts\transcribe.ps1 --resume 本地任务ID --track 音轨索引 --channel right
.\scripts\transcribe.ps1 --resume 本地任务ID --output-dir "D:\Subtitles" --overwrite
.\scripts\transcribe.ps1 --cancel 本地任务ID
```

`--channel left` 选择首声道，`right` 选择第二声道，`mix` 混音。`--track` 使用 FFprobe 音轨索引。恢复时可重选尚未提交任务的音轨／声道，已提交的识别输入固定。

`--non-interactive` 或重定向输入时，歧义保存为 `needs_review` 并以退出码 2 结束。待复核或未完成的字幕暂不输出。

任务库由单个进程独占。运行中按 Ctrl+C 保存状态并停止本地处理，网络请求结束或超时前可能需要等待；已提交云端任务仍可能计费。`--cancel` 停止已保存任务，之后用 `--resume` 显式恢复。

`--resume` 沿用保存的账号、识别输入、排版设置及人工决定，保留成功译文并补齐缺失项。已有两阶段任务恢复时也启用整句复用；旧单阶段任务沿用原流程，可用 `--reflow` 更新。

重复传入同一文件与同一输出目标时，复用未结束任务；`--overwrite` 启用覆盖，省略时沿用原设置。用 `--resume JOB_ID --output-dir 目录` 修改未完成任务的目标；已完成或已跳过任务可重新传入文件，或使用 `--reflow` 重排。

旧任务若提示 `output_path_required`，用 `--output-dir` 指定目标目录后恢复。

`--reflow` 使用当前字幕策略和排版设置，从完整本地转写重建字幕，源媒体可已移走；复用有效翻译缓存，仅补必要翻译。目标已存在时搭配 `--overwrite` 备份后替换，或用 `--output-dir` 写到其他目录；中断后用 `--resume` 继续。缓存、人工语种决定及历史数据的处理见[接口与恢复契约](docs/API_CONTRACT.md#数据与恢复)。

提交超时或响应缺少 ID 时，任务保存为 `submission_unknown`。可绑定已有 ID，或明确接受可能重复计费后重发：

```powershell
.\scripts\transcribe.ps1 --resume 本地任务ID --bind-task-id 云端task_id
.\scripts\transcribe.ps1 --resume 本地任务ID --allow-resubmit
```

进度写入 stderr；任务结果以每行 JSON 写入 stdout，`--report "结果.json"` 另存 JSON 数组。报告包含状态、调用／复用计数、限流和耗时统计及排版警告，省略凭据、上传链接和字幕文字。字段含义见[报告字段](docs/API_CONTRACT.md#报告字段)。

退出码：`0` 完成／跳过或管理操作成功；`2` 待复核／任务失败；`3` 启动／本地异常；`130` Ctrl+C。

## 本机检查

```powershell
.\scripts\transcribe.ps1 --self-test --report "本机诊断.json"
```

诊断在隔离目录检查本机凭据写入、独占锁、FFmpeg 和字幕输出，使用合成音频，云端调用数为零。

[环境维护](docs/BUILD.md) · [接口与恢复契约](docs/API_CONTRACT.md) · [第三方组件](docs/THIRD_PARTY.txt) · [源码许可](LICENSE)
