# 接口与恢复契约

## 云端请求

| 接口 | 请求 | 官方来源 |
| --- | --- | --- |
| ASR | POST `https://{workspace_id}.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/asr/transcription` | [Qwen-ASR](https://help.aliyun.com/zh/model-studio/qwen-asr-api-reference) |
| 查询 | GET 同工作空间 `/api/v1/tasks/{task_id}` | 同上 |
| 翻译 | POST 所选入口根地址 + `/chat/completions` | [百炼结构化输出](https://help.aliyun.com/zh/model-studio/qwen-structured-output)、[百炼兼容接口](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)、[七牛](https://developer.qiniu.com/aitokenapi/13390/chat-completions)、[OpenRouter](https://openrouter.ai/docs/guides/features/structured-outputs) |
| 上传策略 | GET `https://dashscope.aliyuncs.com/api/v1/uploads` | [临时上传](https://help.aliyun.com/zh/model-studio/get-temporary-file-url) |

ASR 模型为 `qwen3-asr-flash-filetrans`，输入为单数 `input.file_url`；`X-DashScope-Async`、`X-DashScope-OssResourceResolve` 均为 `enable`。参数为 `channel_id:[0]`、`enable_words:true`、`enable_itn:false`，省略语种以自动识别。提交成功即保存 `task_id`，查询成功后下载 `output.result.transcription_url`，解析 `transcripts[].sentences[]`。

上传使用返回策略，流式 multipart 的 `file` 字段最后；上传和结果下载不附带 ASR Authorization。容量取 1,000,000,000 字节和策略限制的较小值。提交 POST 不自动重试；超时、5xx、无有效任务 ID 时保留 `submission_unknown`，须绑定已知 ID 或明确接受重发风险。查询、下载失败沿用原 ID。

所有翻译配置统一执行策略／请求契约 5，单模型快捷配置生成一个池入口。

## 翻译池请求契约 5

池使用七牛 `https://api.qnaigc.com/v1`、OpenRouter `https://openrouter.ai/api/v1` 和原百炼工作空间根地址，分别读取独立七牛、OpenRouter 和百炼 Key。入口的 `credential_ref` 对应凭据来源：七牛和百炼使用固定平台地址，`openai_compatible` 使用显式配置的兼容根地址。缺少可选平台 Key 或禁用的入口在快照中以 `backend:null` 表示，不能借用其他平台 Key。

默认模型和权重见[翻译池入口表](../README.md#七牛openrouter-翻译池)。每个阶段先查匹配缓存，再尝试能承接当前请求的免费候选。免费候选排队、冷却及额度查询累计最多 15 秒，候选或等待预算耗尽后尝试允许的付费候选。每个后端每阶段最多两次 HTTP 尝试，单次最多 60 秒，阶段总时限 180 秒；并发最多 4。共享冷却及失败范围按候选身份处理。入口 RPM 冻结在快照中，使用 61 秒滚动窗口，0 表示不主动限流。

实际派发后收到 HTTP 429 或明确 `rate_limit_exceeded`，入口并发上限减半（初始 4、最低 1），发送间隔从 1 秒起逐次翻倍，最多 15 秒。同批在途限流响应只降低一次，更长的 `Retry-After` 仍延长共享冷却。连续 8 次完整合法结果后，并发上限加一、间隔减半，间隔降至不高于 125 毫秒后再经过 8 次成功解除限速。失败重置连续成功计数；缓存、额度检查及限流前派发的结果不参与恢复。

派发前预留发送位置，按实际发送时间计算间隔；准备失败或取消释放预留。发送间隔、RPM 和服务端冷却共同生效，等待计入免费候选原有预算。路由状态保存在本轮内存，新运行重新学习。限流处理依据见[百炼错误处理](https://help.aliyun.com/zh/model-studio/error-code)。

同等级内先应用可用性、冷却、响应健康和跨后端复核偏好，再按合法结果耗时排序。统计按入口、阶段及输出规模分档，每档保留最近 16 次已派发结果；成本为总耗时除以合法成功次数，再乘以在途数加一、除以权重。只有失败时成本为无穷，无观测时按加权并发排序。空闲入口通过正常请求首次采样或在 60 秒后探测恢复。同等排序下，同能力百炼免费模型优先使用即将到期的额度。

路由规模取目标原文、整句译文和片段译文三者的最大字符总量，加每个目标 32 的结构开销，以 512、2048、8192 为界分为四档。它仅影响候选排序；已保存分组、请求及缓存身份沿用原值。额度准备、缓存命中、取消及未发送请求排除于网络性能样本。

池的 OpenRouter 免费入口（`openrouter/free` 或 `:free` 模型）在缓存未命中且需要网络请求时，通过 `GET /key` 查询 [OpenRouter 免费额度](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key)，共享范围为地址和 Key 指纹。并发查询合并，发送前原子预占；实际发出的每次请求包括重试均占一份，确认未发送且额度观测未变化时归还，否则保守保留。首次额度未知、跨 UTC 日无法确认或明确耗尽时跳过该免费候选，可尝试其他允许的候选；同一 UTC 日本轮已有可信余额时，刷新失败可保守续用已有余额，不增加余量。额度查询计入免费等待和阶段总时限；七牛和付费入口不使用此查询，不猜测七牛额度。

`--inspect-bailian-quota` 通过 Windows npm 安装的官方 CLI 2.7.2 查询北京地域额度，输出 `models`：模型、类型、余额、总额、到期日期、`auto_stop` 和 `observation_usable`。后者要求文本模型、有效总额与余量比例、正余额、有效日期及开关开启。CLI 只返回到期日期，工具从该北京日期开始视为不可用。查询失败使用 `mt_quota_unknown`。

绑定及逐模型保护确认见 [README](../README.md#使用百炼免费额度)。`data/bailian-quota-binding.json` 保存 ASR Key、北京工作空间及 CLI 认证上下文的指纹。浏览器会话绑定（版本 1）校验完整配置和控制台令牌；AK/SK 绑定（版本 2）校验 AK/SK、可选 STS、代理身份、地域、站点、工作空间、目录及 Profile，令牌续期和界面偏好变化保留确认。重新绑定清空模型确认；身份变化或保护开关关闭撤销相应确认。实际计费保护依赖云端开关生效。

版本 2 从同一份配置快照取得指纹和 AK/SK，经隐藏子进程的私有管道交给官方 CLI 模块；模块版本与维护见 [BUILD](BUILD.md)。只使用这些 AK/SK 生成的令牌，查询范围限于模型目录、免费额度和保护状态。令牌按绑定隔离并在内存复用，只有响应解析及前后身份检查通过才提交；不写入 CLI 配置或临时凭据文件。每次查询最多生成一次令牌，已有令牌仅在明确会话失效时续期。管道传输、生成及完整查询共用 15 秒上限、4 MiB 响应上限；取消／到期终止并回收子进程。日志排除原始异常与令牌。令牌生成依据[官方鉴权接口](https://help.aliyun.com/zh/model-studio/cli/installation)。

默认池不添加百炼免费候选。用户显式添加的 `qwen3.8-27b` 与 `qwen3.8-max-0902` 分别使用 `json_object` 与 `json_schema`，输入／输出上限分别为 32768 字节／4096 token。百炼免费网络请求须匹配绑定及对应模型确认，并有有效、未到期、报告开关开启的额度观察；额度查询失败或观察无效时跳过该候选。通过身份校验且请求完全匹配的已有缓存仍可复用；百炼付费入口仍按付费许可执行。新人额度按模型及快照分别核算，不按 UTC 日重置；主账号与 RAM 子账号共享，实际余额和开关状态以[官方规则](https://help.aliyun.com/zh/model-studio/new-free-quota)为准。

百炼按确认账户及精确模型共享进程内 token 账本，并发查询合并；新运行重新查询，观察至多使用 20 秒。每次 HTTP 尝试在发送前以完整请求 JSON 的 UTF-8 字节数、输出 token 上限和 1024 的余量作保守预占；可用时按响应中的输入／输出 token 结算，未知用量保留预占，确认未发送的预占按观察代次保守退回。刷新扣除在途预占，本轮同模型本地余额不因刷新增加。该估算并非计费保证，其他程序的并发消耗及控制台更新延迟仍受云端开关约束。

百炼额度暂时查询失败后，同一绑定上下文从失败完成时起共享 10 秒重试间隔；期间跳过免费候选，不复用旧额度。官方 CLI 认证退出码 3 记录为 `quota_console_session_invalid`，终端每轮提示一次重新登录；同一运行内不再查询已确认失效的登录绑定，也不复用其旧额度。新绑定可立即查询，新一轮运行可重新检查（仍受共享的 10 秒间隔限制）。其余查询失败记录为 `quota_refresh_failed`。原始 CLI 响应及错误不进入诊断。

池请求收到百炼 HTTP 403 且错误码精确为 `AllocationQuota.FreeTierOnly` 时，在本轮按当前百炼 Key、工作空间及精确模型禁用该模型候选，其他模型不因此被禁用；`429` 和其他 `403` 不推断为该模型免费额度耗尽。切换等级不会关闭百炼的云端开关，池也不允许相同后端身份重复配置。错误码含义见[百炼错误码](https://help.aliyun.com/zh/model-studio/error-code)。

`qiniu/conatus-free` 仅在北京时间 2026-10-24 00:00 至 2026-11-30 23:59 的活动期内派发；本地判断使用上界 2026-12-01 00:00（不含）。服务端额度、实名认证和结束规则以[七牛活动页](https://www.qiniu.com/ai/free-models/conatus-2026)为准。该免费模型额度不足不会自动转为七牛的付费模型；工具可能依据池的付费许可另行调用付费候选。

`--mt-paid allow|deny` 控制后续付费候选网络调用，已完成结果和匹配缓存仍可复用。本任务阶段记录优先；跨任务缓存按完整实际请求匹配，对齐／复核／纠正优先选择其他后端的匹配结果，其次免费优先。上下文或术语裁剪后按新输入查缓存。

请求使用 `stream:false`。能力档案决定发送 `json_object`、严格 `json_schema` 或省略格式字段的 `text`；`json_object`／`text` 提示词提供完整输出 Schema。OpenRouter 附带参数要求及 Response Healing，百炼附带 `enable_thinking:false`、`temperature:0`。`openrouter/free` 使用 `reasoning.effort:low`，七牛 `deepseek/deepseek-v4.1-flash` 使用 `reasoning_effort:low`，参数依据见 [OpenRouter](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)及[七牛](https://developer.qiniu.com/aitokenapi/13390/chat-completions)。

网络结果、保存阶段及缓存统一校验结构、编号、非空译文和正常结束。术语编号须属于本次请求，词面须出现在对应目标文本中；复核疑点须指向本组片段。非法网络结果参与候选切换，非法保存结果返回 `cache_mismatch`。

入口在同一阶段连续两次返回不合法结构后，五分钟内优先使用同等级其他就绪候选，到期放行一个恢复探测。其他阶段独立记录；同等级没有其他就绪候选时仍可回退尝试该入口。探测等待计入原有等待预算。

`finish_reason:length` 在所有阶段归为 `output_truncated`，可解析的 JSON 同样视为未完成。截断后五分钟内，同阶段、同档或更大请求优先使用同等级其他就绪候选，到期只放行一个相关恢复探测。较小请求及截断前发出的迟到成功结果保留原截断记录。没有其他候选时按费用许可回退。结束标记依据[百炼兼容接口](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)及 [OpenRouter 响应约定](https://openrouter.ai/docs/api/reference/overview)。

每组依次执行术语提取、草稿、必要的片段对齐及自动复核。草稿目标与片段的编号、顺序和原文（忽略目标首尾空白）一一对应时直接使用草稿，否则执行对齐；预算规划采用同一条件。复核优先选择其他可用入口。完整组立即保存，每 15 秒报告活动阶段及等待时间。

术语按来源顺序累积：人工术语优先，自动术语保留不同译法及对应目标文本的证据窗口；相同词面和译法采用最近证据，当前句候选优先于前序句候选。ASCII 术语按完整词匹配。调度线程依次合并前序及当前组术语后派发翻译，空闲线程可预取后续术语，待翻译窗口最多为 `2 * mt_workers`；全部阶段共用最多 4 个线程。取消或失败停止新派发。

草稿前按各阶段预算选定共同的上下文与术语，保存为 `semantic_input`，供草稿、对齐、复核、纠正和恢复使用。本任务术语阶段按原始目标请求定位，记录实际裁剪后的上下文。候选容量或费用许可变化后沿用这些已保存输入。

术语服务不可用时保存本任务的 `glossary_unavailable` 标记，继续翻译并报告警告；取消和本机异常照常中止。复核或纠正服务不可用、纠正后仍有疑点时，保留完整译文和警告；草稿或必要对齐失败则保存进度并停止发布。自动复核是辅助检查，疑点仍需人工判断。

最多两轮纠正，每轮之后再次复核。译文映射重复时提前结束；若疑点集合（片段编号、类别、证据，忽略顺序及重复项）、输入及所涉完整源句的全部片段译文均未变化，停止新增纠正请求。完整保存阶段和匹配缓存优先复用，停滞时保留最后完整译文并报告警告。

输入按完整请求 JSON 的 UTF-8 字节计量，上限为 32768 与入口 `input_bytes` 的较小值；输出 `max_tokens` 为 4096 与入口 `output_tokens` 的较小值。证据裁剪规则见[上下文与输入预算](#上下文与输入预算)。目标及必需证据超限，或实际译文使后续请求超限时，保存进度并返回 `mt_input_budget`。可配置更大输入能力的候选后 `--reflow`；已达全局上限时需缩短输入后新建任务。

新分组额外估算输出量：目标字符按 1 token、每个编号项加 32、固定结构加 64，术语／复核列表每项另加 64，使用有效输出上限的 75%。规划覆盖术语、草稿、必要对齐、复核及整组纠正；每阶段由同一候选同时满足输入和输出预算。候选须满足凭据、有效期、费用许可和百炼免费保护条件，有普通候选时先按其容量规划，并优先采用免费等级的输出容量。

输出估算用于调整分组，请求仍使用完整 `max_tokens`。优先减少整句数，长句沿已有可靠字幕片段边界拆组，完整保留原文。无法拆分的目标按输入预算准入，实际截断交由响应校验和候选切换处理。新排版统一按输入与输出预算规划分组，恢复沿用已保存方案。

### 翻译池配置

`mt_backends:null` 且 `mt_provider:pool` 时使用默认模板；显式列表完整替换模板，默认 ID 可只提供覆盖项，新增 ID 使用完整配置。例如：

```json
{
  "id": "qiniu_course_paid",
  "provider": "qiniu",
  "base_url": "https://api.qnaigc.com/v1",
  "model": "deepseek/deepseek-v4.1-flash",
  "credential_ref": "qiniu",
  "tier": "paid",
  "weight": 3,
  "rpm": 0,
  "capabilities": {"response_format": "json_object", "input_bytes": 32768, "output_tokens": 4096}
}
```

| 字段 | 规则 |
| --- | --- |
| `id` | 1～64 个字母、数字、下划线、点或连字符；入口 ID 与后端身份分别唯一 |
| `provider` / `credential_ref` | `qiniu/qiniu`、`openai_compatible/openrouter` 或 `bailian/bailian` |
| `base_url` | 七牛固定官方根地址、百炼固定工作空间（可填 `null`）；兼容服务可配置规范化 HTTPS 地址或本机 HTTP 地址 |
| `model` | 非空，最多 500 字符，排除空白及控制字符 |
| `tier` / `weight` | `free` 或 `paid`；权重为 1～100 的整数 |
| `rpm` / `enabled` | RPM 为 0～1000000 的整数，0 表示不主动限流；`enabled:false` 禁用入口 |
| `capabilities.response_format` | `json_object`、`json_schema` 或 `text` |
| `capabilities.input_bytes` / `output_tokens` | 1～1048576／1～32768 的整数，实际请求受上述全局预算限制 |
| `mt_routing.paid_enabled` / `free_wait_seconds` | 布尔值／0～15 的整数 |
| `mt_quality.manual_glossary` | 最多 200 项，原文／译文非空、排除控制字符，分别最多 200／500 字符 |

默认七牛为 `json_object`，OpenRouter 和百炼为 `json_schema`；显式添加的百炼免费模型使用各自能力档案。非空 `mt_backends` 列表会启用池；单模型快捷配置设为 `null`，同样生成池入口。新配置通过新任务或 `--reflow` 生效。

## 数据与恢复

运行时只接受当前执行契约：媒体处理 2、提取配方 2、字幕格式 2、规范化 4、显示时间 2、翻译策略／请求契约 5、提示词 3、响应恢复标识 4、上下文 2、对齐策略 `execution_snapshot.alignment_version:2`、术语调度 `prefix-v2`。版本字段必须是整数且精确匹配，术语调度字段也必须存在。`--resume` 和 `--retime` 均先核对契约，缺少字段或版本不匹配时返回 `cache_version`，须用 `--reflow` 从完整转写重建。线程数与轮询等待使用当前设置，排版、模型、能力和账户身份沿任务快照。

翻译身份保存在 `mt_route_snapshot.entries[].backend`，报告中的默认模型从池中推导。翻译重试和错误按请求、阶段及任务记录。

`data/archives/{任务ID}` 保存 `task.json`、`subtitle.srt`、原始 `raw-*.json` 和逐文件 SHA-256 索引；目录中的任务库备份与结果均属于用户恢复资料。

`--export-archive ID --output-dir DIR` 完整校验归档后导出 `ID.srt`，不读取 Key、不调用云端；已有目标需要 `--overwrite`。归档不执行 `--resume`／`--retime`。`--reflow` 归档任务必须指定输出目录，从已验证的原始转写创建新 ID，使用当前账户、池和排版配置；保留能与来源完整对应的人工决定，不重新上传或提交 ASR，必要时重新翻译。原归档不改写，人工决定无法对应时停止。

当前任务 `--resume` 沿保存身份与版本继续；`--reflow` 使用完整本地转写与当前翻译配置重建，准备前保存私有恢复快照，同事务替换来源、布局和阶段标记。`--retime` 校验完整最终字幕及实际产出入口身份后更新显示时间，不调用 ASR、翻译或额度接口。已保存来源或转写齐全时可不依赖源媒体；原位置被不同文件占用时须选择新的输出目录。

任务库使用进程独占锁与 SQLite 事务。账户、工作空间、源文件指纹、音轨、声道及媒体方案均受校验。未结束任务只在输入、输出目标和翻译身份一致时复用。取消不删除结果；首次 Ctrl+C 停止新翻译请求，所有在途翻译共享五秒收尾窗口，再次 Ctrl+C 立即结束等待。ASR 请求等待返回或超时；本机取消不保证云端停止生成或计费。

翻译阶段保存在私有 `mt_stages`，缓存键包含完整实际请求、后端身份、模型能力、提示词及账户范围。最终记录引用草稿、复核、纠正和实际产出阶段；实际产出可以是草稿、对齐或纠正，片段通过 `mt_stage_key` 和 `mt_origin` 关联完整组。私有草稿按目标顺序保存 `source_text_hashes`，包括从匹配响应缓存恢复的草稿。直接使用草稿时，最终产出引用等于草稿引用，译文映射一致，片段原文摘要匹配对应草稿编号。部分片段重新分片后，其余完整结果仍可校验。译文、阶段证据、缓存和计数原子提交。恢复和校时校验来源、范围、模型及完整映射；人工复核同事务更新来源与片段，清除受影响的翻译与阶段状态。

`mt_group_plan` 在首个阶段请求前事务保存版本 1、排版轮次、分组片段 ID 和来源／布局摘要。恢复核对摘要、完整覆盖、顺序、语种及人工决定边界，异常返回 `cache_mismatch`。完成译文后沿用分组；重分片、重排及人工语种复核与新布局同事务清除方案。提交中断后重新读取已提交 manifest。

发布顺序：渲染校验 → 保存发布意图 → 备份并原子替换 → 提交完成 → 尽力清理音频。`publication_intent` 固定目标、预期哈希、原目标哈希、覆盖许可及预定备份路径。已匹配预期内容时补记完成，不重复备份；外部修改返回 `output_conflict`。改目标或重建内容时清除失效意图；重排、校时快照属于本机私有数据。

## 时间轴与字幕

媒体版本 2 优先复制兼容压缩音轨，并验证包时间戳、编码延迟／填充、音频属性和时长。多声道仅明确选择首声道时可复制；需要处理声道或时间轴时转换为 16kHz 单声道 FLAC。超限按 PCM 样本精确切分后编码，逐段校验容量；单段最长 12 小时，规格见[ASR 音频说明](https://help.aliyun.com/zh/model-studio/asr-model)。MIME 随实际容器使用 `audio/mp4`、`audio/mpeg`、`audio/flac`、`audio/ogg`。

OGG 复制启用 `-fflags +bitexact`，重建核对完整 SHA、分段与时间轴。已知云端任务 ID 和完整本地转写优先复用；版本要求见[数据与恢复](#数据与恢复)。

PCM 中间文件使用 RIFF WAV，数据容量上限为 `UINT32_MAX - 4096` 字节（PCM16／16kHz／单声道约 37.28 小时）。提取前按音轨时间范围预检，缺失时使用容器时长；提取后及切分前核对实际大小与 RIFF／data 声明范围，切分时核对读取样本数。容量超限返回 `media_pcm_limit`，须先分割输入；文件截断或声明不一致返回 `media`。

规范化保留原文、源句索引和原始时间依据。零时长短句仅在句级与完整词级文字、标点和时间点一致，且语种支持、文字不超过 12 字时修复：补最多 1000ms 显示区间，受下一句和音频结尾限制；可用区间不足 400ms 时，可合并距离不超过 400ms 的同语种有效邻句。修复保存来源与警告，词时间不用于继续细分；其他无效时间返回错误。

规范化在补短句显示窗口前处理含长句的连续同点组。每句须非空、同一受支持语种，全部词为该整数时间点且文字和标点完整覆盖句子。组前后时间顺序正常时，依次尝试精确相接、间隔不超过 400ms 的同分段、同语种、非空原始正时长邻句，每级前句优先。距离始终按原始正时长区间计算，显示区间取邻句与 ASR 点的并集；近邻合并记录原邻句区间及距离。保留全部文字、顺序、句索引和原始时间，词时间置空。证据或边界不满足条件时返回 `timeline`，原始响应保持不变。

规范化在叠加全局偏移前校验句时间：起点位于段内，终点允许最多 1ms 取整误差，更大越界返回 `timeline`。可选的 `transcripts[].text` 非空而句子文字全空时返回 `response_schema`。原始结果保留供排查。

完整句的词时间须覆盖清理后的全部文字及标点，为整数、单调不重叠且位于句内。连续零时长词合并到下一个正时长词，句尾合并到前一词组；文字范围和首末时间端点均取自原记录，每组保持正时长。全句零时长、倒序、重叠、越界或文字不匹配时使用句级边界，句级修复或负时间裁剪后同样如此。片段 ID 由源句和词组范围稳定生成；无可靠词时间时使用完整文字范围。

句间识别时间重叠保留原始依据并记录警告。显示时间仅支持版本 2：自动处理同分段、不同来源句且来源递增、满足 `前起点 < 后起点 < 前终点 < 后终点` 的冲突。重叠同时不超过 500ms 与前条原显示时长的 10%，修整后前条和后条原时长均至少 1000ms。只收短前条显示终点；来源、词时间及译文不变。其他冲突返回 `timeline`。

在生成布局后、每轮字幕翻译前及布局重分片提交前执行纯显示预检，失败时不发送该轮字幕 MT 请求，不提前保存调整摘要或发布意图，保留已有成功结果。发布前再次规划并全量校验显示区间为非负整数、正时长、单调且不重叠，相接端点允许相等。

`cues.begin_ms/end_ms` 保留来源及词范围依据。每条 `display_timing_adjustments` 包含 `cue_id`、`old_begin_ms/old_end_ms`、`new_begin_ms/new_end_ms`、`adjustment_ms`（提前终点的正毫秒数）及 `reason`：固定为 `bounded_sentence_overlap`。

原文或最终译文超过各两行，或时长超过 7 秒时尝试分段。断点依次选择句末标点／至少 400ms 停顿、逗号／分号、完整词边界，同级取文字量更均衡的位置。新片段使用真实首末词时间且至少 1000ms，原有更短句保留。最终译文超过两行时，仅重拆超限片段，最多追加两轮翻译；无法继续安全拆分时保留内容和时间并记录警告。

换行依次优化行数、英文短尾行和长度均衡，保留完整词及标点。宽字符计 2、普通字符计 1、组合附加符计 0；中文宽度设置乘 2，其他语言按原值。尾部标点允许最多 4 单位超宽，不可拆长词允许超宽。SRT 为 UTF-8 无 BOM、CRLF、合法毫秒时间，同目录原子提交，覆盖前备份；无语音时完成任务且不生成 SRT。

## 上下文与输入预算

邻句须有一致 ASR 语种、翻译决定、源语言和人工确认状态，遇到不兼容项即停止该方向扩展；未知语种且未人工指定源语言时不扩展。窗口围绕整个目标组的首尾计算，排除组内目标，前后各最多 12 句，与最近目标边界距离最多 30000ms，扩展路径间隔最多 15000ms。按时间距离、句子距离、来源位置确定保留优先级，发送给模型时按原文顺序排列。

目标携带原句标识和字符范围。拆组时，同句其他文字沿完整词边界整理为证据块，每侧最近约 64 字符的块为必需证据，远处块的目标长度依次加倍。请求以 `kind:same_sentence`、目标 `sentence` 编号和字符范围标明证据，模型仅翻译目标。超预算时依次裁剪远处邻句、可选同句证据和术语，均按完整条目裁剪，保留完整目标及必需证据。初始分片和分组预算包含必需同句证据。

## 报告字段

stdout 每行 JSON，stderr 显示进度；报告不包含 Key、签名链接、转写和译文。报告路径在执行前和写入前检查输入、输出、备份、data 及恢复快照，覆盖路径别名和 Windows 备用数据流。归档也位于受保护的 data 内。

| `counts` 字段 | 含义 |
| --- | --- |
| `mt` / `mt_429` / `mt_success` | 已登记尝试／限流响应／HTTP 2xx，均不等同于最终译文成功 |
| `mt_invalid_response` / `mt_output_truncated` | 未通过本地响应校验的次数／其中以 `length` 结束的次数 |
| `mt_subtitle_completed` | 已同事务保存的最终片段数 |
| `mt_v5_{入口ID}_{阶段}_request` | 实际入口的阶段请求登记数 |
| `mt_v5_{阶段}_completed` / `_cache_hit` / `_failure` | 阶段完成、跨任务缓存命中、候选失败数 |
| `mt_quota_query` | 免费额度查询登记数 |
| `mt_route_switch` / `mt_route_wait_ms` | 候选切换数／已完成阶段累计等待毫秒 |
| `mt_input_tokens` / `mt_output_tokens` / `mt_usage_unknown` | 可读取的用量累计／缺失有效用量的响应数 |
| `mt_quality_warning` / `mt_glossary_unavailable` | 复核未通过或不可用的组数／术语不可用次数 |
| `mt_correction_stalled` | 重复疑点及涉及的整句译文均未变化，且无完整保存纠正可复用时停止新增纠正请求的组数 |
| `stage_*_ms` / `run_ms` | 跨恢复累计阶段与运行耗时 |

诊断位于 `.build/mt-diagnostics/{job_id}.jsonl`，只保留白名单中的入口、模型、阶段、HTTP 状态、耗时、尝试次数及安全错误分类，不保存正文或原始异常。`route_attempt.selection` 为 `weighted`（无观测）、`learning`（首次采样）、`performance`（按合法结果耗时）或 `probe`（重新探测）；`size_bucket` 为 0～3 的输出规模档位。`route_pacing` 记录入口的 `concurrency`（1～4）、`interval_ms`（0～15000）及 `reason`（`rate_limit` 或 `recovered`）。`route_failure.dispatched` 区分是否已越过翻译请求发送登记边界，`false` 包括额度准备或派发前校验拒绝，不代表已发出翻译 HTTP 请求。路由尝试可能被发送前的额度校验拒绝，不能把 `route_attempt` 数量直接当作 HTTP 请求数。
