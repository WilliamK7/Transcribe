# 百炼命令行字幕工具

Windows x64 本地 CLI。使用北京工作空间的 `qwen3-asr-flash-filetrans` 自动识别语音，默认用 `qwen-mt-flash` 翻译为简体中文，也可配置 OpenAI Chat Completions 兼容翻译后端。

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

## 选择翻译后端

默认 `mt_provider: "bailian"`，使用百炼 Key。通过 `data/settings.json` 或环境变量选择兼容翻译服务；ASR 使用百炼 Key 和北京 Workspace ID。

| 配置项 | 默认值 | 环境变量 | 含义 |
| --- | --- | --- | --- |
| `mt_provider` | `bailian` | `MT_PROVIDER` | `bailian` 或 `openai_compatible` |
| `mt_base_url` | `https://api.openai.com/v1` | `MT_BASE_URL` | 兼容接口根地址，保留服务商要求的路径前缀 |
| `mt_model` | 空 | `MT_MODEL` | 兼容后端的模型名称，启用时必填 |
| `mt_rpm` | `0` | `MT_RPM` | 兼容后端本地每分钟请求上限，非负整数；0 不主动限制 |

环境变量覆盖同名文件设置，仅对当前进程及其子进程生效。例如，先配置 ASR，再在 PowerShell 设置兼容服务：

```powershell
$env:MT_PROVIDER = 'openai_compatible'
$env:MT_BASE_URL = 'https://服务商地址/要求的路径/v1'
$env:MT_MODEL = '服务商提供的模型名称'
# 另行设置 OPENAI_API_KEY，或将独立 MT Key 写入本机 data/mt-key.txt
.\scripts\transcribe.ps1 "D:\Videos\课程.mkv"
```

独立 MT Key 优先读取 `OPENAI_API_KEY`，其次读取明文 `data/mt-key.txt`。新任务在提交 ASR 前验证翻译配置与本机凭据是否齐全。

根地址填写到 `/chat/completions` 之前的路径，使用无凭据、查询参数和片段的 HTTPS URL；回环地址也支持 HTTP。服务需支持非流式文本 Chat Completions 和编号 JSON 译文，详细请求见[接口契约](docs/API_CONTRACT.md#云端请求)。

更换服务或模型后，对已有任务使用 `--reflow`，详见下方恢复说明。

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
- 完整句结合前后原文翻译，译文用于排版及整句字幕；需要拆分时，同一句的字幕片段联合翻译。同批相同请求共享结果，恢复时复用匹配的翻译缓存。
- 新任务结合前后各 30 秒内、最多各 12 句的原文理解上下文，允许最多 15 秒的句间停顿；实际范围受语种、人工决定和输入预算限制。
- 联合翻译连续两次校验失败后，自动缩小请求组重试，最小为单片，字幕范围与时间保持原样。单片仍失败时保存进度，供后续恢复。
- 翻译默认 4 并发，短任务无固定逐条间隔；百炼在 61 秒滚动窗口内最多发送 60 次，兼容后端按 `mt_rpm` 等待额度释放。真实限流仍共享退避并逐步提速，进度会区分配额等待与服务端限流。
- 原文、译文各以两行为目标，短句保持一行；保留完整单词和标点，平衡英文尾行。实际显示也受播放器字体和窗口影响。
- 文字过多或超过 7 秒时，按可靠词时间拆分，新片段至少 1 秒。零时长词与相邻词组合后参与分段。无法继续拆分时保留完整内容和已有时间，在报告中记录排版超限警告。
- 新任务自动收短相邻句 1～100ms 的小重叠，调整后前条至少显示 1 秒。其他时间冲突保存进度并返回 `timeline`，需人工确认；详细规则见[时间轴与字幕](docs/API_CONTRACT.md#时间轴与字幕)。
- 已有 SRT 默认跳过；`--overwrite` 先备份再覆盖。同名不同扩展视频的目标冲突需要调整文件名或输出目录。
- 优先复制 AAC→M4A、MP3→MP3、FLAC→FLAC、Opus／Vorbis→OGG；需要声道处理或时间轴校正时转换为 16kHz 单声道 FLAC。
- 保留音轨偏移、静音和编码延迟；超过 12 小时或上传容量时按 PCM 样本精确分段。上传容量取 1 GB 与服务返回限额中的较小值。
- 需要转换为 PCM 的音轨约以 37.28 小时为上限；遇到 `media_pcm_limit` 时，请先分割输入。

可在 `data/settings.json` 调整以下设置，新任务和 `--reflow` 使用当前设置，`--resume` 沿用任务保存的排版设置。宽度计量见[时间轴与字幕](docs/API_CONTRACT.md#时间轴与字幕)。

| 设置 | 默认值 | 含义 |
| --- | --- | --- |
| `mt_workers` | `4` | 翻译并发数，1～4 |
| `zh_line_width` | `44` | 中文每行全角字符宽度，12～72 |
| `other_line_width` | `84` | 其他语种每行显示宽度，24～160 |
| `chinese_first` | `false` | 译文是否排在原文上方 |

## 复核与任务管理

音轨、声道或语种存在歧义时，终端会要求确认。语种复核逐条显示原文，可选择保留或翻译；手动语言只影响 MT。同一完整句的片段一起确认，每条决定立即保存，输入 `q` 暂停后可继续复核。

```powershell
.\scripts\transcribe.ps1 --non-interactive --channel mix "D:\Videos\课程.mkv"
.\scripts\transcribe.ps1 --jobs
.\scripts\transcribe.ps1 --review 本地任务ID
.\scripts\transcribe.ps1 --resume 本地任务ID
.\scripts\transcribe.ps1 --reflow 本地任务ID --overwrite
.\scripts\transcribe.ps1 --retime 本地任务ID --overwrite
.\scripts\transcribe.ps1 --resume 本地任务ID --track 音轨索引 --channel right
.\scripts\transcribe.ps1 --resume 本地任务ID --output-dir "D:\Subtitles" --overwrite
.\scripts\transcribe.ps1 --cancel 本地任务ID
```

`--channel left` 选择首声道，`right` 选择第二声道，`mix` 混音。`--track` 使用 FFprobe 音轨索引。恢复时可重选尚未提交任务的音轨／声道，已提交的识别输入固定。

`--non-interactive` 或重定向输入时，歧义保存为 `needs_review` 并以退出码 2 结束。待复核或未完成的字幕暂不输出。

任务库由单个进程独占。运行中按 Ctrl+C 保存状态并停止本地处理，网络请求结束或超时前可能需要等待；已提交云端任务仍可能计费。`--cancel` 停止已保存任务，之后用 `--resume` 显式恢复。

`--resume` 沿用保存的 ASR 账号、MT 后端／地址／模型、识别输入、翻译策略、上下文窗口、排版设置、显示时间策略及人工决定，校验原 Key 后保留成功译文并补齐缺失项。历史任务沿用百炼翻译；需要更新显示时间时使用 `--retime`。并发与 RPM 使用当前运行设置。

已有完整规范化结果、完整本地转写，或缺失转写均有云端任务 ID 时，源媒体移走后仍可 `--resume`；需要新提交识别时须恢复原媒体。原路径换成其他媒体后，必须用 `--output-dir` 写到其他目录。Key 校验与账户隔离仍适用于纯本地恢复。

程序升级后若提示 `cache_version`，用 `--reflow JOB_ID` 更新任务处理策略；需要完整本地转写，目标已存在时搭配 `--overwrite` 或另选 `--output-dir`。

翻译校验失败的诊断保存在 `.build/mt-diagnostics/{job_id}.jsonl`，包含失败分类、数量、片段范围及请求 ID。

重复传入同一文件与同一输出目标时，翻译身份相同才复用未结束任务；配置变化时须明确使用 `--resume` 或 `--reflow`。`--overwrite` 启用覆盖，省略时沿用原设置。用 `--resume JOB_ID --output-dir 目录` 修改未完成任务的目标；已完成或已跳过任务可重新传入文件，或使用 `--reflow` 重排。

旧任务若提示 `output_path_required`，用 `--output-dir` 指定目标目录后恢复。

写入字幕时中断，可用 `--resume` 继续，已写入的相同字幕和备份会复用。若目标或备份被外部修改，返回 `output_conflict` 并保留文件，可另选 `--output-dir`。

`--reflow` 使用当前翻译配置、字幕策略和排版设置，从完整本地转写重建字幕，保留原 ASR 身份及人工决定，源媒体可已移走。更换 MT 后端、地址、模型或 Key 后，使用新身份的缓存或重新翻译，无需旧 MT Key。目标已存在时搭配 `--overwrite` 备份后替换，或用 `--output-dir` 写到其他目录；准备失败保留原任务，中断后用 `--resume` 沿新配置继续。详见[接口与恢复契约](docs/API_CONTRACT.md#数据与恢复)。

`--retime` 使用当前显示时间规则，复用完整的最终字幕、译文和原排版设置，云端调用数为零。需要原账户及 MT 凭据和有效的已保存结果，源媒体和原始转写可已移走。目标已存在时搭配 `--overwrite` 备份后替换，或用 `--output-dir` 另存；中断后用 `--resume` 继续。原任务快照保存在本机 `.build/retime`。

提交超时或响应缺少 ID 时，任务保存为 `submission_unknown`。可绑定已有 ID，或明确接受可能重复计费后重发：

```powershell
.\scripts\transcribe.ps1 --resume 本地任务ID --bind-task-id 云端task_id
.\scripts\transcribe.ps1 --resume 本地任务ID --allow-resubmit
```

进度写入 stderr；任务结果以每行 JSON 写入 stdout，`--report "结果.json"` 另存 JSON 数组。报告包含任务实际 `mt_provider`、`models.mt`、状态、调用／复用计数、限流和耗时统计及排版警告；凭据、上传链接和字幕文字保持私有。字段含义见[报告字段](docs/API_CONTRACT.md#报告字段)。

报告请另存到 `.build` 或其他独立位置；输入媒体、任务 SRT、备份及内部数据受路径冲突保护（`report_conflict`）。同名报告可覆盖；自检默认保存到 `data/self-test.json`。

退出码：`0` 完成／跳过或管理操作成功；`2` 待复核／任务失败；`3` 启动／本地异常；`130` Ctrl+C。

## 本机检查

```powershell
.\scripts\transcribe.ps1 --self-test --report "本机诊断.json"
```

诊断在隔离目录检查本机凭据写入、独占锁、FFmpeg 和字幕输出，使用合成音频，云端调用数为零。

[环境维护](docs/BUILD.md) · [接口与恢复契约](docs/API_CONTRACT.md) · [第三方组件](docs/THIRD_PARTY.txt) · [源码许可](LICENSE)
