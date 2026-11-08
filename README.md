# magpie 字幕工具

Windows x64 本地 CLI。语音识别使用北京百炼 `qwen3-asr-flash-filetrans`，翻译使用 magpie 字幕专用组。上游 Key、模型和故障切换由常驻 magpie 管理，字幕分组、校验、取消与恢复保留在项目内。

## 开始使用

已完成迁移的机器，保持 magpie 运行，直接处理视频：

```powershell
.\scripts\transcribe.ps1 --check-mt --mt-paid deny
.\scripts\transcribe.ps1 "D:\Videos\课程.mkv"
```

`--check-mt` 发送合成短句。`--mt-paid deny` 只使用已核验的免费组；默认允许免费服务不可用时使用付费后备。无参数或 `--help` 显示帮助；多个文件顺序处理。

`--check-mt magpie` 要求当前有效 `mt_provider` 为 `magpie`，并检查该配置的地址与组名。旧直连配置需先完成迁移或运行 `--configure-mt magpie`；环境变量覆盖同样参与检查，冲突时在发送请求前报错。

首次准备环境运行 `.\scripts\setup.ps1`，使用 Python 3.12.10 x64、锁定依赖和项目 FFmpeg。已有解释器可传 `-PythonPath "C:\Python312\python.exe"`。环境维护见 [BUILD](docs/BUILD.md)。

## 迁移到 magpie

适用于已有七牛、OpenRouter、百炼直连配置和 Windows 便携版 magpie。启动 magpie 后，先查看计划，再执行：

```powershell
.\.venv\Scripts\python.exe -m subtitle_tool.magpie_migration --magpie "D:\Program Files\magpie\magpie.com" --data-dir "D:\Program Files\magpie\data"
.\.venv\Scripts\python.exe -m subtitle_tool.magpie_migration --magpie "D:\Program Files\magpie\magpie.com" --data-dir "D:\Program Files\magpie\data" --apply
```

迁移读取原配置的服务地址与凭据，使用 magpie 官方 CLI 创建 `transcribe-*` 入口及字幕组。先备份至 `data/migrations/magpie-*`，成功核对入口后才切换项目设置；中断可重跑，已完成迁移时重跑不覆盖后续手工调整。原 Key 文件、任务、转写、缓存、字幕及归档保留用于恢复。备份含凭据，留在本机。

全新安装可先用 `--configure` 录入北京工作空间及百炼 Key、`--configure-mt qiniu` 录入七牛 Key，再执行上面的导入。已有 magpie 入口只需设置下表字段；`--configure-mt magpie` 会备份设置并清除旧池选择，使用默认字幕组，不导入上游 Key。

## magpie 字幕组

| 组 | 成员与顺序 |
| --- | --- |
| `group/transcribe-free` | 七牛 `qiniu/conatus-free` → OpenRouter `openrouter/free` |
| `group/transcribe` | 免费组 → 七牛 `deepseek/deepseek-v4.1-flash` → 百炼 `qwen3.8-flash` |

表中是原默认池迁移后的配置；自定义模型沿用原地址、凭据与模型。免费组采用顺序路由，总组采用 magpie 的「免费优先」（`routing=free`），两组均关闭会话黏着，避免一次付费回退后继续黏在付费服务。免费入口无服务商级付费回退；OpenRouter 入口默认限制 20 RPM，已配置更严格限制时沿用。

项目按费用许可选一次组：`--mt-paid deny` 只请求免费组；允许付费时直接请求总组，由 magpie 先尝试免费成员。每阶段最多向网关提交一次，新任务的完整响应时限为 180 秒，包含网关排队与回退。网关故障或输出校验失败时保存进度，通过 `--resume` 继续。

| 职责 | 负责方 |
| --- | --- |
| 上游 Key、模型成员、免费优先、RPM、冷却、故障切换、上游用量与费用记录 | magpie |
| 字幕语义、术语、完整 ID 校验、排版、阶段缓存、断点恢复、取消与总时限 | 本项目 |
| 禁止付费、免费活动到期、ASR 账户与工作空间隔离 | 项目保留边界检查；magpie 的免费标签和估算费用不能代替云端保证 |

请求携带 `X-Magpie-Session: transcribe-<任务ID>`。在 magpie 的 Routing 页面按会话查看成员尝试及回退，Usage 页面查看实际用量；本地任务报告和 `--check-mt` 返回 `gateway_session`，响应的实际成员模型也保存到阶段记录。

七牛活动截至北京时间 **2026 年 11 月 30 日 23:59**。之后首次翻译会通过 magpie CLI 停用迁移创建的 `transcribe-qiniu-free`，继续使用 OpenRouter 免费服务；停用失败则停止相关路由，避免活动结束后按免费使用。自定义同类入口需在 magpie 停用或移出组。活动资格以[官方规则](https://www.qiniu.com/ai/free-models/conatus-2026)为准。

百炼试用模型按额度、到期和“用完即停”开关决定是否免费。迁移保留原模型到已关闭的 `transcribe-bailian-trial`，不把试用额度当成无条件免费。当前免费组只接受七牛上述活动模型及 OpenRouter 的 `openrouter/free`／`:free` 模型；百炼试用须另行接入可验证的网关额度保护。

## 项目配置

迁移自动填写 `data/settings.json`；更换地址或组名时修改相应字段：

| 设置 | 默认值或用途 |
| --- | --- |
| `mt_provider` | `magpie` |
| `mt_base_url` / `magpie_base_url` | `http://127.0.0.1:3425/v1`，分别用于翻译和 ASR |
| `mt_free_model` | `group/transcribe-free`，免费组 |
| `mt_model` | `group/transcribe`，允许付费时的组 |
| `magpie_data_dir` | 本机常驻 magpie 的 `data` 目录，用于核对账户及免费边界 |
| `asr_provider` | 迁移后为 `magpie`；未迁移旧配置为 `bailian` |
| `magpie_asr_provider` / `magpie_upload_provider` | `transcribe-bailian` / `transcribe-upload` |

翻译支持 `MT_PROVIDER`、`MT_BASE_URL`、`MT_MODEL`、`MT_FREE_MODEL` 环境覆盖。本机默认用 `Bearer magpie-transcribe` 标记用量；需要网关认证时使用 `MAGPIE_API_KEY` 或 `data/magpie-key.txt`，不借用上游 Key。远程翻译网关必须配置自己的凭据；无法核验免费组时不会发送免费等级请求；本机注册表不能为另一个网关提供免费保证。端口修改需同步 magpie 的保存设置。

ASR 提交、查询、上传策略均通过 magpie 原生代理；OSS 文件上传和签名结果下载仍直接访问存储，不附带 API Key。项目只读取 magpie 中的百炼 Key 来保持原账户指纹，网络只发送网关凭据。更换百炼 Key 要同时更新 ASR 与上传入口；两个入口必须属于相同账户、原北京工作空间。

新配置用于新任务和 `--reflow`。`--resume` 使用原任务冻结的翻译地址、组名、凭据指纹和能力；历史任务需通过 `--reflow` 从原始转写重建。magpie 组内成员可在网关调整，每次响应的实际模型另行记录。要让旧任务改走网关，从原始转写显式执行 `--reflow`。详细契约见 [API_CONTRACT](docs/API_CONTRACT.md)。

## 右键“发送到”

环境准备成功后安装“!百炼字幕”入口。在资源管理器中选中文件，右键 → 发送到 → !百炼字幕；Windows 11 可先点“显示更多选项”。字幕默认位于源文件同目录，已有 SRT 默认跳过。

窗口支持音轨、声道和语种复核；全部成功或跳过后自动关闭，其余情况保留提示。项目移动后重新安装入口：

```powershell
.\scripts\install-sendto.ps1
.\scripts\install-sendto.ps1 -Remove
```

## 字幕与音频

- 默认输出 `视频名.srt`，外语原文在上、简体中文译文在下；中文／粤语保留原文。无语音时完成任务且不生成字幕。
- 翻译按组完成术语、整句翻译、必要的片段对齐和自动复核；原文与字幕片段一一对应时省去对齐请求。草稿、对齐、复核和纠正共享保存的上下文与术语，拆句时保留同句前后证据；自动术语允许同词多义，人工术语优先。最多两轮纠正，译文重复，或疑点及所涉译文均未变化时提前结束。每组完成即保存，每 15 秒报告活动阶段；复核不可用或仍有疑点时保留完整译文并提示人工检查。
- 输入预算不足时裁剪上下文，再按可靠词时间拆分。实际译文使后续请求超预算时保存进度并返回 `mt_input_budget`；无法继续拆分时需缩短输入后新建任务，全局上限为 32768 字节。
- 根据模型输入与输出容量调整每组句数，长句沿可靠片段边界分组。分组方案随任务保存，恢复时继续使用；重排及人工语种复核后重新规划。
- 原文和译文各以两行为目标，超长字幕按可靠词时间拆分。无法继续拆分时保留完整内容并记录警告。可修整的小幅句间重叠自动收短，其他冲突返回 `timeline`；规则见[时间轴与字幕](docs/API_CONTRACT.md#时间轴与字幕)。
- 已有 SRT 默认跳过；`--overwrite` 先备份再覆盖。同名不同扩展媒体可指定不同输出目录。
- 优先复制 AAC、MP3、FLAC、Opus／Vorbis；需要声道或时间轴处理时转换为 16kHz 单声道 FLAC，保留偏移、静音和编码延迟。超过 12 小时或上传容量时分段；`media_pcm_limit` 表示输入需先分割。

在 `data/settings.json` 调整排版，新任务及 `--reflow` 使用当前设置，`--resume` 沿用已保存设置。并发使用当前值。

| 设置 | 默认值 | 范围或含义 |
| --- | --- | --- |
| `mt_workers` | `4` | 1～4 |
| `zh_line_width` | `44` | 12～72 |
| `other_line_width` | `84` | 24～160 |
| `chinese_first` | `false` | 译文是否位于原文上方 |

## 复核与任务管理

音轨、声道或语种有歧义时交互确认。语种复核显示原文，可选择保留或翻译；同句片段一起确认，决定立即保存，输入 `q` 暂停。`--non-interactive` 或重定向输入时，歧义保存为 `needs_review`。

```powershell
.\scripts\transcribe.ps1 --jobs
.\scripts\transcribe.ps1 --review 本地任务ID
.\scripts\transcribe.ps1 --resume 本地任务ID
.\scripts\transcribe.ps1 --resume 本地任务ID --mt-paid deny
.\scripts\transcribe.ps1 --reflow 本地任务ID --overwrite
.\scripts\transcribe.ps1 --retime 本地任务ID --overwrite
.\scripts\transcribe.ps1 --cancel 本地任务ID
```

| 操作 | 用途 | 所需本地资料 |
| --- | --- | --- |
| `--resume` | 在当前执行契约内保留身份与成功结果，补齐未完成部分 | 原账户及需要使用的翻译凭据 |
| `--reflow` | 用当前翻译配置从转写重建字幕，保留 ASR 身份和人工决定 | 完整本地转写、原 ASR 与当前 MT 凭据 |
| `--retime` | 更新显示时间，保留完整译文和排版，零云端调用 | 完整最终字幕及对应账户凭据 |

重排或校时使用 `--overwrite` 备份后替换，或 `--output-dir "D:\Subtitles"` 另存。操作保存恢复快照，当前版本任务中断后用 `--resume` 继续。更换模型、Key 或提示 `cache_version` 时使用 `--reflow`，从完整本地转写重建，可能需要重新翻译。原归档保留。

`--track` 使用 FFprobe 音轨索引；`--channel left|right|mix` 选择首声道、第二声道或混音。恢复时可重选尚未提交识别的输入。已有完整规范化结果、本地转写或云端任务 ID 时，原媒体移走后仍可恢复；原路径被其他媒体占用时另选输出目录。

`--jobs` 中标记为 `archived` 的任务保存于 `data/archives`，包含原始转写、人工决定和校验后的 SRT：

```powershell
# 原字节导出，不需要 Key，不访问云端；文件名为任务 ID.srt
.\scripts\transcribe.ps1 --export-archive 旧任务ID --output-dir "D:\Subtitles\Archive"
# 从归档转写创建新任务；不重新识别，必要时按当前网关配置重新翻译
.\scripts\transcribe.ps1 --reflow 旧任务ID --output-dir "D:\Subtitles\Rebuilt"
```

归档重建返回新的任务 ID，使用当前账户和翻译配置，保留可对应的人工语种决定；对应关系不明确时停止。原归档保留。

任务库由一个进程独占。Ctrl+C 保存进度；翻译最多等待 5 秒保存在途完整结果，再次 Ctrl+C 立即结束等待。其他请求等待返回或超时。云端已提交请求仍可能计费；取消的任务可用 `--resume` 恢复。

提交状态未知时，可绑定已有云端任务 ID，或接受重复计费风险后重发：

```powershell
.\scripts\transcribe.ps1 --resume 本地任务ID --bind-task-id 云端task_id
.\scripts\transcribe.ps1 --resume 本地任务ID --allow-resubmit
```

同一文件和输出目标复用未结束任务；翻译身份改变时明确选择恢复或重排。`--output-dir` 按调用位置解析，恢复沿用已保存路径；`output_path_required` 表示需要重新指定。写字幕中断后恢复会复用已写结果和备份，文件被外部修改时返回 `output_conflict`，可另选目录。

进度写入 stderr，结果以每行 JSON 写入 stdout；`--report ".build\结果.json"` 另存 JSON 数组。报告省略凭据、链接和字幕文字；输入、输出及内部数据路径受冲突保护。翻译脱敏诊断位于 `.build/mt-diagnostics/{job_id}.jsonl`，字段见[报告契约](docs/API_CONTRACT.md#报告字段)。

退出码：`0` 成功／跳过；`2` 待复核／任务失败；`3` 启动／本地异常；`130` Ctrl+C。

## 本机检查

```powershell
.\scripts\transcribe.ps1 --self-test --report ".build\self-test.json"
```

使用隔离目录及合成音频检查凭据写入、独占锁、FFmpeg 和字幕输出，云端调用数为零。离线回归与依赖维护见[环境维护](docs/BUILD.md)。

[环境维护](docs/BUILD.md) · [接口与恢复契约](docs/API_CONTRACT.md) · [第三方组件](docs/THIRD_PARTY.txt) · [源码许可](LICENSE)
