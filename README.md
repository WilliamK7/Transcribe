# 百炼命令行字幕工具

Windows x64 本地 CLI。通过北京工作空间的 `qwen3-asr-flash-filetrans` 识别语音，用七牛／OpenRouter／百炼翻译池生成简体中文字幕，也支持单个翻译后端。翻译池免费优先，默认允许付费后备。

## 开始使用

在可写项目目录中打开 PowerShell：

```powershell
.\scripts\setup.ps1
.\scripts\transcribe.ps1 --configure
.\scripts\transcribe.ps1 "D:\Videos\课程.mkv"
```

环境使用 Python 3.12.10 x64、锁定依赖和项目 FFmpeg。已有解释器可传 `setup.ps1 -PythonPath "C:\Python312\python.exe"`。无参数或 `--help` 显示帮助；多个文件顺序处理。

`--configure` 填写北京 Workspace ID 并隐藏输入 Key；`--configure-key` 只更新 Key。Key 明文保存到 `data/key.txt`，环境变量 `DASHSCOPE_API_KEY` 优先。`data` 还保存任务、转写和缓存，应留在本机。`--workspace-id ID` 可为新任务指定工作空间。

## 选择翻译后端

默认 `mt_provider:"pool"`；已有设置明确选择其他后端时沿用该设置。ASR 始终使用百炼账户。翻译配置用于新任务和 `--reflow`；`--resume` 沿用任务保存的翻译身份和策略。

### 七牛／OpenRouter 翻译池

配置七牛 Key 并检查免费模型：

```powershell
.\scripts\transcribe.ps1 --configure-mt qiniu
.\scripts\transcribe.ps1 --check-mt qiniu
.\scripts\transcribe.ps1 --check-mt --mt-paid deny
.\scripts\transcribe.ps1 --mt-paid deny "D:\Videos\课程.mkv"
```

`--configure-mt qiniu` 隐藏输入 Key，明文保存到 `data/qiniu-key.txt` 并选择翻译池；`QINIU_API_KEY` 优先。`--check-mt` 检查当前配置，使用合成短句，首个候选成功后报告入口和模型。检查会联网，启用付费候选时可能产生费用。

| 等级 | 入口 ID | 默认模型 | 凭据 |
| --- | --- | --- | --- |
| 免费 | `qiniu_free` | `qiniu/conatus-free` | `QINIU_API_KEY` 或 `data/qiniu-key.txt` |
| 免费 | `openrouter_free` | `openrouter/free` | `OPENAI_API_KEY` 或 `data/mt-key.txt` |
| 付费 | `qiniu_paid` | `deepseek/deepseek-v4.1-flash` | 七牛 Key |
| 付费 | `bailian_paid` | `qwen3.8-flash` | 百炼 Key 及原工作空间 |

每个等级内默认权重为 3:1。缺少凭据的入口跳过；只配置百炼 Key 时可能直接使用百炼付费候选。免费候选累计等待最多 15 秒，然后尝试允许的付费候选。每阶段最多 180 秒，每后端最多 2 次请求，单次最多 60 秒，总并发默认 4。

`--mt-paid deny` 禁止付费候选的新请求，`allow` 允许；新建、恢复和重排均可指定，恢复时保存新的许可状态。已有匹配缓存仍可复用。池使用此选项管理后备；单后端使用下文的 `--mt-fallback`。

七牛 Conatus 活动截至北京时间 **2026 年 11 月 30 日 23:59**，工具在活动结束后跳过该免费模型。实名认证、资格和额度见[官方活动页](https://www.qiniu.com/ai/free-models/conatus-2026)。

### 使用百炼免费额度

百炼免费额度按地域、模型及快照核算，用完或到期后默认转付费。为免费入口开启“免费额度用完即停”，等待生效后再使用，详见[官方额度规则](https://help.aliyun.com/zh/model-studio/new-free-quota)。

使用 Windows npm 安装的官方 CLI **2.7.2**，确保 PATH 可找到 `bl.cmd` 和 Node.js。可登录控制台会话：

```powershell
bl auth login --console
.\scripts\transcribe.ps1 --inspect-bailian-quota
```

检查只读查询余额、到期日期及 `auto_stop`，从到期日零点起跳过模型。`observation_usable` 表示额度字段有效，账户绑定和保护确认另行检查。`bl auth status` 只显示本地凭据状态；实际可用性以额度查询为准。

长期运行可在 CLI 所选配置中保存 OpenAPI AK/SK，工具用它生成并续期查询令牌；已有 AK/SK 且 `bl usage free` 查询成功时，无需再登录浏览器。AK/SK 登录与 RAM 用户权限见[官方鉴权说明](https://help.aliyun.com/zh/model-studio/cli/installation)和[权限管理](https://help.aliyun.com/zh/model-studio/permission-management-overview)。`BailianGateway.Team.NotAuthorised` 需要由主账号将 RAM 用户加入对应百炼团队并授权，重复登录不能替代授权。

确认 CLI 所选 AK/SK（未配置时为控制台登录账户）与工具的北京工作空间、Key 属于同一主账号，然后添加模型并绑定：

```powershell
.\scripts\transcribe.ps1 --configure-mt-pool --bailian-free-model qwen3.8-27b --bailian-free-model qwen3.8-max-0902
.\scripts\transcribe.ps1 --bind-bailian-account --confirm-same-account
```

在[百炼控制台](https://bailian.console.aliyun.com/)为这两个精确模型开启“免费额度用完即停”，等待生效，再记录确认：

```powershell
.\scripts\transcribe.ps1 --confirm-bailian-protection qwen3.8-27b --confirm-bailian-protection qwen3.8-max-0902 --confirm-propagation-waited
.\scripts\transcribe.ps1 --mt-paid deny "D:\Videos\课程.mkv"
```

添加模型保留已有池条目；默认池没有百炼免费入口。命令使用已保存的北京工作空间，新入口用于新任务，旧任务通过 `--reflow` 应用。

配置 AK/SK 后，重新绑定并确认模型保护一次即可启用自动续期。Key、AK/SK、工作空间或 CLI 认证身份改变时重新确认；单纯的令牌续期保留确认。观察到保护开关关闭时撤销对应模型确认。绑定记录只保存指纹，详见[额度与绑定契约](docs/API_CONTRACT.md#翻译池请求契约-5)。

只使用浏览器登录时，会话失效后重新登录、绑定并确认模型保护，再 `--resume`。使用 AK/SK 时由工具自动续期；凭据被撤销或权限不足时修正相应配置。查询失败会跳过百炼免费候选，其他允许的候选继续使用。

### 自定义翻译池

在 `data/settings.json` 合并以下设置，保留 Workspace ID 等其他配置：

```json
{
  "mt_provider": "pool",
  "mt_backends": [
    {"id": "qiniu_free"},
    {"id": "openrouter_free", "rpm": 20},
    {"id": "qiniu_paid"},
    {"id": "bailian_paid"}
  ],
  "mt_routing": {"paid_enabled": false, "free_wait_seconds": 15},
  "mt_quality": {"manual_glossary": {"idempotency": "幂等性"}}
}
```

`mt_backends` 为 `null` 或省略时使用默认池；显式列表完整替换默认列表。默认 ID 可只写覆盖项，自定义入口须填写完整身份和能力。字段范围及示例见[池配置契约](docs/API_CONTRACT.md#翻译池配置)。

### 单后端

将 `mt_backends` 设为 `null`，通过文件设置或环境变量选择服务；环境变量优先。

| 设置 | 环境变量 | 用途 |
| --- | --- | --- |
| `mt_provider` | `MT_PROVIDER` | `bailian`、`qiniu`、`openai_compatible` 或 `pool` |
| `mt_base_url` | `MT_BASE_URL` | 兼容接口根地址，默认 `https://api.openai.com/v1` |
| `mt_model` | `MT_MODEL` | 兼容后端必填；七牛留空使用 `qiniu/conatus-free` |
| `mt_rpm` | `MT_RPM` | 兼容后端每分钟请求上限，0 表示不主动限流 |

百炼单后端使用 `qwen-mt-flash`。兼容后端使用 `OPENAI_API_KEY` 或明文 `data/mt-key.txt`，根地址填写到 `/chat/completions` 之前，保留服务商要求的路径前缀。

```powershell
$env:MT_PROVIDER = 'openai_compatible'
$env:MT_BASE_URL = 'https://openrouter.ai/api/v1'
$env:MT_MODEL = 'openrouter/free'
$env:MT_RPM = '20'
.\scripts\transcribe.ps1 "D:\Videos\课程.mkv"
```

OpenRouter 单后端默认启用百炼后备，可能产生费用。暂时故障、格式错误或内容拒绝时，当前组转百炼；可信免费额度耗尽时，剩余组转百炼。每次需要禁用后备时传 `--mt-fallback none`，恢复时也保留该选项。重试及旧任务恢复规则见[接口契约](docs/API_CONTRACT.md#数据与恢复)。

## 右键“发送到”

环境准备成功后安装“!百炼字幕”入口。在资源管理器中选中文件，右键 → 发送到 → !百炼字幕；Windows 11 可先点“显示更多选项”。字幕默认位于源文件同目录，已有 SRT 默认跳过。

窗口支持音轨、声道和语种复核；全部成功或跳过后自动关闭，其余情况保留提示。项目移动后重新安装入口：

```powershell
.\scripts\install-sendto.ps1
.\scripts\install-sendto.ps1 -Remove
```

## 字幕与音频

- 默认输出 `视频名.srt`，外语原文在上、简体中文译文在下；中文／粤语保留原文。无语音时完成任务且不生成字幕。
- 翻译池按组完成术语、整句翻译、片段对齐和自动复核，最多两轮纠正。完成一组即保存，每 15 秒报告活动阶段。辅助复核不可用或仍有疑点时保留完整译文并报告警告；自动复核不能保证语义质量。
- 输入预算不足时裁剪上下文，再按可靠词时间拆分。实际译文使后续请求超预算时保存进度并返回 `mt_input_budget`；可配置更大输入能力的候选后重排，全局上限为 32768 字节。
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
| `--resume` | 保留原身份、策略及成功结果，补齐未完成部分 | 原账户及需要使用的翻译凭据 |
| `--reflow` | 用当前翻译配置从转写重建字幕，保留 ASR 身份和人工决定 | 完整本地转写、原 ASR 与当前 MT 凭据 |
| `--retime` | 更新显示时间，保留完整译文和排版，零云端调用 | 完整最终字幕及对应账户凭据 |

重排或校时使用 `--overwrite` 备份后替换，或 `--output-dir "D:\Subtitles"` 另存。操作保存恢复快照，中断后用 `--resume` 继续。更换模型、Key 或提示 `cache_version` 时使用 `--reflow`。

`--track` 使用 FFprobe 音轨索引；`--channel left|right|mix` 选择首声道、第二声道或混音。恢复时可重选尚未提交识别的输入。已有完整规范化结果、本地转写或云端任务 ID 时，原媒体移走后仍可恢复；原路径被其他媒体占用时另选输出目录。

任务库由一个进程独占。Ctrl+C 保存进度；翻译池、OpenRouter 契约 2／3 及其后备最多等待 5 秒保存在途完整结果，再次 Ctrl+C 立即结束等待。其他请求等待返回或超时。云端已提交请求仍可能计费；取消的任务可用 `--resume` 恢复。

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

使用隔离目录及合成音频检查凭据写入、独占锁、FFmpeg 和字幕输出，云端调用数为零。

[环境维护](docs/BUILD.md) · [接口与恢复契约](docs/API_CONTRACT.md) · [第三方组件](docs/THIRD_PARTY.txt) · [源码许可](LICENSE)
