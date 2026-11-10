# magpie 字幕工具

Windows x64 自用字幕 CLI。项目直接调用北京百炼 `qwen3-asr-flash-filetrans`，管理上传、识别和任务恢复；magpie 只负责翻译上游的凭据、限流与故障切换，项目负责字幕语义、校验与排版。

## 开始使用

```powershell
.\scripts\setup.ps1
# 完成下方配置并启动 magpie
.\scripts\transcribe.ps1 --configure-key
.\scripts\transcribe.ps1 --check-mt
.\scripts\transcribe.ps1 "D:\Videos\课程.mkv"
```

`--check-mt` 用合成短句检查翻译。统一免费优先，免费不可用时商业组接替。多个文件顺序处理，`--help` 显示全部参数。

## 配置 magpie

本机适配 magpie 0.1.3565。在 Magpie 中配置渠道，在 `data/settings.json` 中填写 `magpie_data_dir`，再预览和验收候选：

```powershell
.\.venv\Scripts\python.exe scripts\configure-mt.py --list-candidates
.\.venv\Scripts\python.exe scripts\configure-mt.py --apply
# 筛选本轮发现的渠道；现有字幕池仍参与验收
.\.venv\Scripts\python.exe scripts\configure-mt.py --providers github-copilot,vercel --apply
.\.venv\Scripts\python.exe scripts\configure-mt.py --providers transcribe-openrouter-free --apply --refresh-trial
```

候选渠道和型号规则保存在 `data/mt-channels.json`，初始内容见[配置模板](subtitle_tool/mt-channels.default.json)。模板包含 GitHub Copilot、Cloudflare Workers AI、OpenCode Zen Free、vLLM-SR、Vercel、OpenRouter、七牛和百炼。新渠道由用户登记，`--list-candidates` 可只读预览。

脚本使用合成文本验收，备份配置后更新托管分组，保留人工成员和开关。角色分配按底层型号隔离，每个角色都有商业后备：

| 角色 | 主组 | 商业后备 |
| --- | --- | --- |
| 正文 | `group/transcribe` | `group/transcribe-paid` |
| 术语及轻量复核 | `group/transcribe-review` | `group/transcribe-review-paid` |
| 疑难裁决 | `group/transcribe-judge` | `group/transcribe-judge-paid` |

主组按免费组 → 商业组选择。同档供应商等权、供应商内部有序尝试，试用子组占一个供应商位置，各组 `stays=off`。免费组会继续尝试其他候选，冷却、繁忙和阶段总时限可能提前触发商业后备。

费用按型号识别：Copilot 套餐和 Workers AI 按商业入口处理；Zen 使用明确免费的型号；Vercel 使用官方零价信息，赠送余额单独计量。OpenRouter 只选通过验收的明确 `:free` 型号。商业调用次数与实际现金支出分别记录，资源包和代金券的抵扣以账单为准。

vLLM-SR `auto` 用于翻译，其公开后端集合统一归属翻译角色。需要在 Magpie 提供者设置中添加响应头透传 `X-Vsr-Selected-Model`。项目校验实际型号及验收资格，身份缺失或冲突时使用商业后备。

### 自动维护

```powershell
.\.venv\Scripts\python.exe scripts\manage-mt.py --enable
.\.venv\Scripts\python.exe scripts\manage-mt.py --status
.\.venv\Scripts\python.exe scripts\manage-mt.py --once
.\.venv\Scripts\python.exe scripts\manage-mt.py --add-provider 供应商ID
.\.venv\Scripts\python.exe scripts\manage-mt.py --disable
```

启用后，首次联网字幕请求启动后台，任务退出时停止。默认每 15 分钟刷新已登记渠道的目录，每轮最多验收 1 个型号、每天 UTC 最多 2 个；每型号最多 12 次候选请求和 1 次独立评判。预算在请求前保存，重启继续累计。`--once` 使用相同预算；`configure-mt.py` 用于手动批量验收。`--status` 只读本机状态，登记和启停命令只修改管理配置。

`mt-channels.json` 的主要规则：

| 字段 | 用途 |
| --- | --- |
| `providers` | 明确登记的渠道及适配器、候选和费用规则 |
| `excluded_providers` | 项目级渠道排除，适用于维护、验收、评测和字幕请求 |
| `include` / `exclude` | 型号名称通配符 |
| `preferred` | 优先型号及明确允许的商业候选 |
| `billing` / `max_candidates` | 允许费用档位及每轮目录候选数 |
| `max_input_usd_per_million` / `max_output_usd_per_million` | 新商业候选单价上限；Vercel 默认输入 2、输出 10 美元 |

新商业型号需要明确候选授权，或有可核实且符合上限的价格。无法确定价格时状态为 `price_unknown`；单价筛选独立于账户总支出。已验收的资格按账户、接口能力和底层型号集合复用。临时故障进入冷却，目录查询失败保留已有池。

维护状态保存在 `data/gateway-maintenance.json`，备份在 `data/gateway-backups/`。人工删除和停用形成持久排除；恢复时需同时恢复 Magpie 开关，并清除相关 `manual_hold` 或 `gateway-managed.json` 排除项。中断更新会先核对已执行变更的归属，再按当前规则规划。成功改变路由时递增策略版本，历史任务继续按原快照恢复。

### 百炼试用

`data/bailian-quota-binding.json` 绑定账户。后台每 60 秒查询有效额度，逐个验收并维持最多三个合格候选，每款型号使用独立入口。额度拒绝会停用对应型号，即使该请求最终通过其他入口成功；耗尽状态保持至本任务结束。共享账户 RPM 预算包含商业入口，模型与工作空间配额可进一步收紧。

`data/bailian-trial-state.json` 的 `paused` 和 `excluded` 控制人工暂停与排除。原 `transcribe-bailian-trial` 保存账户模板，实际入口使用带哈希后缀的 ID。百炼额度维护有独立验收预算。七牛限时免费活动到期后，专用免费入口退出选路。

自动管理作用于上表的默认本机角色组。远程网关及自定义角色组由用户维护。运行诊断与恢复细节见[接口契约](docs/API_CONTRACT.md)。

### Decisions / System One 评测

```powershell
.\.venv\Scripts\python.exe scripts\evaluate-decisions.py
# 使用指定的 Magpie System One 成员
.\.venv\Scripts\python.exe scripts\evaluate-decisions.py --models 供应商/型号,供应商/另一型号
```

独立评测通过 Magpie `/v1/systemone` 比较 D3 Flash、D3 Ultra、OpenRouter Decisions 与当前复核入口。32 个带标签合成样本覆盖八类质量问题、跨句信息和指令干扰。报告位于 `.build/mt-diagnostics/decisions-*.json`，包含漏报、误报、置信度、延迟和接口可取得的用量成本，连接失败单独统计。评测结果供人工比较，生产复核、裁决和阈值继续使用现有配置。

## 项目配置

在 `data/settings.json` 填写工作空间和 magpie 数据目录，其余字段可使用默认值：

```json
{
  "workspace_id": "your-workspace-id",
  "magpie_data_dir": "D:\\Program Files\\magpie\\data"
}
```

| 设置 | 默认值或用途 |
| --- | --- |
| `mt_base_url` | `http://127.0.0.1:3425/v1`，仅用于翻译 |
| `mt_model` | `group/transcribe`，免费优先的正文主组 |
| `mt_fallback_model` | `group/transcribe-paid`，主组超时或结果校验失败后的付费后备 |
| `mt_policy_version` | 整数 `1`；更换翻译策略并要求重译时递增 |
| `mt_routing.strategy` | `free_first` |
| `mt_review_model` / `mt_review_fallback_model` | 上表复核主组与商业后备 |
| `mt_judge_model` / `mt_judge_fallback_model` | 上表裁决主组与商业后备 |
| `mt_quality.manual_glossary` | 人工术语映射，最多 200 项 |
| `mt_workers` | 默认 `16`，允许任意正整数；控制同时在途的翻译阶段数 |
| `zh_line_width` / `other_line_width` | `44` / `84`，范围 12～72 / 24～160 |
| `chinese_first` | `false`，译文是否位于原文上方 |

ASR Key 使用 `DASHSCOPE_API_KEY`，未设置时读取 `data/key.txt`；`--configure-key` 隐藏输入并保存本地 Key。提交、轮询和上传策略直接使用该 Key，OSS 上传和签名下载不携带它；恢复仍核验原账户指纹。

翻译地址和组名支持 `MT_BASE_URL`、`MT_MODEL`、`MT_FALLBACK_MODEL`、`MT_REVIEW_MODEL`、`MT_REVIEW_FALLBACK_MODEL`、`MT_JUDGE_MODEL`、`MT_JUDGE_FALLBACK_MODEL` 环境覆盖。网关凭据使用 `MAGPIE_API_KEY` 或 `data/magpie-key.txt`，本机缺省为 `magpie-transcribe`；远程翻译网关须配置凭据。免费证明绑定本机注册表所属监听地址，端口修改需同步 magpie 的保存设置。

新任务与 `--reflow` 使用当前配置；`--resume` 沿用任务保存的翻译身份和排版，线程数与轮询等待使用当前值。需要按新策略重新翻译时，递增 `mt_policy_version` 后执行 `--reflow`；保持版本可复用匹配缓存。

## 字幕与交互

默认生成同目录、同名 UTF-8 SRT，外语原文在上、简体中文译文在下；中文／粤语保留原文，无语音则完成任务。已有 SRT 默认跳过，`--overwrite` 先备份再替换，也可用 `--output-dir` 另存。

默认每 8 组增量提取术语，超预算拆分；一次翻译直接输出全部字幕编号。每组保留独立语义复核，有疑点才裁决并修正，最多两轮。完整译文逐组保存，仍有疑点时提示人工检查。原文与译文各以两行为目标，按可靠词时间拆分长句；具体规则见[接口与恢复契约](docs/API_CONTRACT.md)。

音频优先复制兼容压缩音轨，声道或时间轴需要处理时转为 16kHz 单声道 FLAC，保留偏移、静音及编码延迟。超过 12 小时或上传容量时分段；`media_pcm_limit` 表示需先分割输入。

音轨、声道或语种有歧义时在终端确认，语种复核显示原文，同句片段一起确认。输入 `q` 暂停；`--non-interactive` 或重定向输入时将歧义保存为 `needs_review`。`--track` 使用 FFprobe 音轨索引，`--channel left|right|mix` 选择声道；尚未提交识别的输入可在恢复时重选。

环境准备会安装资源管理器“发送到 → !百炼字幕”入口；全部成功或跳过后窗口自动关闭，其余情况保留提示。项目移动后运行 `.\scripts\install-sendto.ps1` 重装，移除入口使用 `-Remove`。

## 任务管理

```powershell
.\scripts\transcribe.ps1 --jobs
.\scripts\transcribe.ps1 --review 本地任务ID
.\scripts\transcribe.ps1 --resume 本地任务ID
.\scripts\transcribe.ps1 --reflow 本地任务ID --overwrite
.\scripts\transcribe.ps1 --retime 本地任务ID --overwrite
.\scripts\transcribe.ps1 --cancel 本地任务ID
```

| 操作 | 用途与所需资料 |
| --- | --- |
| `--resume` | 在当前执行契约内补齐未完成部分，使用原账户及所需网关凭据 |
| `--reflow` | 从完整本地转写按当前配置重建，保留 ASR 身份和人工决定，必要时重新翻译 |
| `--retime` | 校验已保存的完整字幕后更新显示时间，零云端调用 |

重排和校时保存恢复快照，中断后用 `--resume` 继续。已有完整规范化结果、本地转写或云端任务 ID 时，原媒体移走后仍可恢复；原路径被其他媒体占用时另选输出目录。`cache_version` 表示需要从完整转写重建。

归档位于 `data/archives`。导出按原字节校验字幕；重建创建新任务 ID，使用当前配置并保留可对应的人工决定和原归档：

```powershell
.\scripts\transcribe.ps1 --export-archive 归档任务ID --output-dir "D:\Subtitles\Archive"
.\scripts\transcribe.ps1 --reflow 归档任务ID --output-dir "D:\Subtitles\Rebuilt"
```

任务库由一个进程独占。Ctrl+C 保存进度，翻译共享最多 5 秒的收尾窗口，再次 Ctrl+C 立即结束等待；已提交的云端请求仍可能计费。提交状态未知时，可绑定已有云端 ID，或明确接受重复计费风险后重发：

```powershell
.\scripts\transcribe.ps1 --resume 本地任务ID --bind-task-id 云端task_id
.\scripts\transcribe.ps1 --resume 本地任务ID --allow-resubmit
```

进度写入 stderr，stdout 每行一个 JSON 结果；`--report ".build\结果.json"` 另存 JSON 数组。退出码：`0` 成功／跳过，`2` 待复核／任务失败，`3` 启动／本地异常，`130` Ctrl+C。

`--jobs` 查看最后保存的任务记录，保留状态和诊断信息；`lifecycle.next_actions` 提供与终端提示一致的下一步参数、必需输入和重发确认要求。`--state-machine` 输出当前状态契约 JSON，无需配置或凭据；字段与恢复边界见[接口契约](docs/API_CONTRACT.md)。

## 本机检查

```powershell
.\scripts\transcribe.ps1 --self-test --report ".build\self-test.json"
```

使用隔离目录及合成音频检查配置原子保存、独占锁、FFmpeg 和字幕输出，云端调用数为零。逐请求诊断位于 `.build/mt-diagnostics/{任务ID}.jsonl`，提供请求 ID、模型、用量与校验结果。通过 `gateway_session` 可在 magpie Routing／Usage 查看对应请求，字段见[报告契约](docs/API_CONTRACT.md#报告字段)。

[环境维护](docs/BUILD.md) · [接口与恢复契约](docs/API_CONTRACT.md) · [第三方组件](docs/THIRD_PARTY.txt) · [源码许可](LICENSE)
