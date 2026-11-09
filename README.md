# magpie 字幕工具

Windows x64 自用字幕 CLI。北京百炼 `qwen3-asr-flash-filetrans` 负责识别，magpie 字幕组负责翻译。magpie 管理上游凭据、限流与故障切换；项目负责字幕语义、校验、排版与任务恢复。

## 开始使用

```powershell
.\scripts\setup.ps1
# 完成下方配置并启动 magpie
.\scripts\transcribe.ps1 --check-mt --mt-paid deny
.\scripts\transcribe.ps1 "D:\Videos\课程.mkv"
```

`--check-mt` 用合成短句检查翻译。默认允许付费后备；`--mt-paid deny` 只使用已核验的免费组。多个文件顺序处理，`--help` 显示全部参数。

## 配置 magpie

在 magpie 中配置以下入口，并为两者设置同一北京工作空间账户的单一 Key：

| 入口 ID | `chat` 地址 |
| --- | --- |
| `transcribe-bailian` | `https://<workspace_id>.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` |
| `transcribe-upload` | `https://dashscope.aliyuncs.com/compatible-mode/v1` |

两个入口保持启用、自定义认证头为空。项目从本机注册表核验账户与工作空间，ASR 提交、查询和上传策略通过网关；OSS 上传与签名结果下载直接访问存储。

本机字幕组配置如下，模型和凭据在 magpie 中维护：

| 组 | 路由 | 成员顺序 |
| --- | --- | --- |
| `group/transcribe-free` | 顺序 `routing=order` | 七牛 `qiniu/conatus-free` → OpenRouter `openrouter/free` |
| `group/transcribe` | 免费优先 `routing=free` | 免费组 → 七牛 `deepseek/deepseek-v4.1-flash` → 百炼 `qwen3.8-flash` |
| `group/transcribe-paid` | 顺序 `routing=order` | 百炼 `qwen3.8-flash` → 七牛 `deepseek/deepseek-v4.1-flash` |

三组均设 `stays=off`。免费入口保持服务商级回退为空，OpenRouter 入口使用 20 RPM 或账户要求的更严格值。项目按费用许可选择免费组或总组，由 magpie 完成成员选择。主组超时、响应截断或格式修复失败时，已允许付费的任务自动切换到独立付费组，成功结果随进度保存。时限、重试次数及后备组要求见[阶段恢复](docs/API_CONTRACT.md#阶段恢复)。

七牛活动截至北京时间 **2026 年 11 月 30 日 23:59**，资格见[官方规则](https://www.qiniu.com/ai/free-models/conatus-2026)。活动到期后，项目通过 magpie CLI 停用专用入口 `transcribe-qiniu-free`；自动停用要求该入口仅配置活动模型，且 magpie 为带 `.portable` 标记、同目录 `data` 的 Windows 便携版。停用失败时停止相关请求；自定义活动入口由用户在 magpie 停用或移出组。

免费组支持上述七牛活动模型和 OpenRouter 的 `openrouter/free`／`:free` 模型，并在请求前核验成员和回退路径。百炼试用额度需另有可验证的网关保护。

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
| `mt_base_url` / `magpie_base_url` | `http://127.0.0.1:3425/v1`，分别用于翻译和 ASR |
| `mt_free_model` / `mt_model` | `group/transcribe-free` / `group/transcribe` |
| `mt_fallback_model` | `group/transcribe-paid`，主组超时或结果校验失败后的付费后备 |
| `magpie_asr_provider` / `magpie_upload_provider` | `transcribe-bailian` / `transcribe-upload` |
| `mt_policy_version` | 整数 `1`；更换翻译策略并要求重译时递增 |
| `mt_routing.paid_enabled` | `true`，可由本次 `--mt-paid allow\|deny` 覆盖 |
| `mt_quality.manual_glossary` | 人工术语映射，最多 200 项 |
| `mt_workers` | `4`，范围 1～4 |
| `zh_line_width` / `other_line_width` | `44` / `84`，范围 12～72 / 24～160 |
| `chinese_first` | `false`，译文是否位于原文上方 |

翻译地址和组名支持 `MT_BASE_URL`、`MT_MODEL`、`MT_FREE_MODEL`、`MT_FALLBACK_MODEL` 环境覆盖。网关凭据使用 `MAGPIE_API_KEY` 或 `data/magpie-key.txt`，本机缺省为 `magpie-transcribe`；远程翻译网关须配置凭据。免费证明及 ASR 身份绑定本机注册表所属监听地址，端口修改需同步 magpie 的保存设置。

新任务与 `--reflow` 使用当前配置；`--resume` 沿用任务保存的翻译身份和排版，线程数与轮询等待使用当前值。需要按新策略重新翻译时，递增 `mt_policy_version` 后执行 `--reflow`；保持版本可复用匹配缓存。

## 字幕与交互

默认生成同目录、同名 UTF-8 SRT，外语原文在上、简体中文译文在下；中文／粤语保留原文，无语音则完成任务。已有 SRT 默认跳过，`--overwrite` 先备份再替换，也可用 `--output-dir` 另存。

每组执行术语提取、整句翻译、必要的片段对齐和自动复核，最多两轮纠正。完整译文逐组保存，仍有疑点时提示人工检查。原文与译文各以两行为目标，按可靠词时间拆分长句；具体规则见[接口与恢复契约](docs/API_CONTRACT.md)。

音频优先复制兼容压缩音轨，声道或时间轴需要处理时转为 16kHz 单声道 FLAC，保留偏移、静音及编码延迟。超过 12 小时或上传容量时分段；`media_pcm_limit` 表示需先分割输入。

音轨、声道或语种有歧义时在终端确认，语种复核显示原文，同句片段一起确认。输入 `q` 暂停；`--non-interactive` 或重定向输入时将歧义保存为 `needs_review`。`--track` 使用 FFprobe 音轨索引，`--channel left|right|mix` 选择声道；尚未提交识别的输入可在恢复时重选。

环境准备会安装资源管理器“发送到 → !百炼字幕”入口；全部成功或跳过后窗口自动关闭，其余情况保留提示。项目移动后运行 `.\scripts\install-sendto.ps1` 重装，移除入口使用 `-Remove`。

## 任务管理

```powershell
.\scripts\transcribe.ps1 --jobs
.\scripts\transcribe.ps1 --review 本地任务ID
.\scripts\transcribe.ps1 --resume 本地任务ID --mt-paid deny
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
