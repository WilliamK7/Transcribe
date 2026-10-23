# 接口与恢复契约

## 云端请求

| 接口 | 请求 | 官方来源 |
| --- | --- | --- |
| ASR | POST `https://{workspace_id}.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/asr/transcription` | [Qwen-ASR](https://help.aliyun.com/zh/model-studio/qwen-asr-api-reference) |
| 查询 | GET 同一工作空间 `/api/v1/tasks/{task_id}` | 同上 |
| 默认 MT（百炼） | POST 同一工作空间 `/compatible-mode/v1/chat/completions` | [Qwen-MT](https://help.aliyun.com/zh/model-studio/qwen-mt-api) |
| 兼容 MT | POST 配置根地址 + `/chat/completions` | [OpenAI Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create) |
| OpenRouter MT（请求契约 2） | POST `https://openrouter.ai/api/v1/chat/completions` | [结构化输出](https://openrouter.ai/docs/guides/features/structured-outputs)、[Response Healing](https://openrouter.ai/docs/guides/features/plugins/response-healing) |
| OpenRouter 免费额度（仅请求契约 2 的免费模型） | GET `https://openrouter.ai/api/v1/key` | [免费额度与 UTC 日计数](https://openrouter.ai/docs/api_reference/limits) |
| 上传策略 | GET `https://dashscope.aliyuncs.com/api/v1/uploads`，`action=getPolicy`，`model=qwen3-asr-flash-filetrans` | [临时上传](https://help.aliyun.com/zh/model-studio/get-temporary-file-url) |
| 文件上传 | 返回的 `upload_host`，流式整文件 multipart，`file` 字段最后，使用上传策略鉴权 | 同上 |

上传使用系统默认代理，容量取 1,000,000,000 字节与策略 `max_file_size_mb` 的较小值，每次获取策略后重新检查。上传与结果下载均省略百炼 Authorization。

ASR 模型为 `qwen3-asr-flash-filetrans`，输入为单数 `input.file_url`。`X-DashScope-Async`、`X-DashScope-OssResourceResolve` 均为 `enable`；`parameters` 为 `channel_id:[0]`、`enable_words:true`、`enable_itn:false`，省略 `language` 以自动识别。提交成功即持久化 `output.task_id`；查询成功后从 `output.result.transcription_url` 下载结果，按 `transcripts[].sentences[]` 解析。

默认 MT 模型为 `qwen-mt-flash`，`stream:false`，每次一条 user 文本消息。中文／粤语跳过 MT，语种复核在所有 MT 请求前完成。

翻译配置及环境变量见 [README](../README.md#选择翻译后端)。新任务入队前验证配置与凭据；普通恢复按保存的 MT 身份执行，并使用当前运行 RPM。

兼容根地址规范化主机名、默认端口和末尾斜杠，保留路径前缀，再追加 `/chat/completions`。根地址使用无凭据、查询参数、片段及路径回退的 HTTPS URL；localhost 或回环 IP 支持 HTTP。请求使用独立 Bearer Key；请求契约 1 的字段为 `model`、`messages`、`stream:false`。

新任务和显式重排在规范化根地址恰为 `https://openrouter.ai/api/v1` 时使用请求契约 2；后端仍为 `openai_compatible`，其他兼容地址与百炼使用版本 1。版本 2 在上述基础字段之外发送：

- `response_format: {"type":"json_schema","json_schema":{"name":"subtitle_translations","strict":true,"schema":…}}`。Schema 为必需的 `translations` 数组，成员为必需的字符串 `id`、`text`；两层对象均设置 `additionalProperties:false`，`id.enum` 仅含本次从 `"0"` 开始的字符串编号。条数、编号唯一性、非空译文及字幕控制内容继续由本地校验。
- `provider: {"require_parameters":true}`，仅路由至支持请求参数的端点。
- `plugins: [{"id":"response-healing"}]`，启用非流式格式修复；插件不能保证修复所有响应，仍须本地校验。

普通、修复和递归子组共用请求构造，子组重新生成自己的编号集合；完整 Schema、端点设置及插件字段均计入输入预算。`openrouter/free` 按请求能力筛选免费模型，见[免费路由](https://openrouter.ai/docs/guides/routing/routers/free-router)。暂时无可用端点时有限重试，耗尽后按后备策略处理；明确的参数拒绝直接停止。

两个后端共用策略 3 的提示词、上下文、编号 JSON 校验、繁转简和缩组恢复；请求构造与预算使用同一后端和模型参数。兼容服务的非 2xx 错误映射为 `mt_http_{HTTP状态}`，2xx 中的业务错误映射为 `mt_service_error`；诊断仅记录本地分类和计数，服务端消息、错误码、请求 ID 及响应原文保持私有。

请求契约 2 先识别顶层／全部 choice 的业务错误及 `finish_reason:error`，再检查正常结束和正文。服务错误不进入格式修复或缩组；`content:null` 配合 `finish_reason:length` 归入非正常结束。内部错误仅保留本地分类、HTTP 状态和是否可重试，服务端消息与原始错误码不写入诊断或任务。只按白名单 `error.metadata.error_type` 判断业务类别，见[官方错误分类](https://openrouter.ai/docs/api_reference/errors-and-debugging)。普通 404／503 不被推断为不支持 Schema。

OpenRouter 契约 2 的网络连接／读取失败、请求总时限、HTTP 408／429／5xx，以及明确的 `timeout`、`provider_unavailable`、`provider_overloaded`、`server`、`rate_limit_exceeded` 共用每份正文最多 **4 次 HTTP 尝试，包含首次**的预算，HTTP 与业务错误共用这一预算。参数、凭据、权限、余额、内容政策、输入限制及未知业务错误直接停止；永久类别优先于暂时 HTTP 状态。格式修复和缩组各自生成独立正文。

官方 OpenRouter 根地址、请求契约 2 且模型为 `openrouter/free` 时，HTTP 404 允许有限重试；顶层或 choice 中的业务 `not_found` 在 HTTP 2xx／404／408／429／5xx 下同样可重试，保留原分类并共用上述四次预算。其他永久类型和不可重试的 HTTP 状态优先终止；`not_found` 混合未知、畸形或缺失类型的业务错误归为 `service_unknown`。固定模型（含固定 `:free`）、旧契约和其他根地址的 `not_found` 直接停止。

暂时错误按原后端身份共享恢复：连续失败使用 15／30／60 秒冷却，另加 0～1 秒抖动；同轮并发失败合并。冷却结束仅放行一个翻译探测，成功后恢复并发，旧在途成功不能清除新冷却。保留 429 的自适应间隔；冷却、RPM 窗口及间隔取最晚截止时间。有效 `Retry-After` 按秒数或 HTTP 日期处理，超过 300 秒返回 `mt_retry_after`，不提前请求；截止耗尽使用 `mt_request_deadline`。启用后备时由组级调度处理这些可恢复故障，关闭时停止新调度并收尾。

OpenRouter 契约 2 及百炼后备使用隐藏子进程执行非流式请求，凭据经私有管道传递。翻译联网和完整读取响应合计限时 120 秒，额度查询限时 15 秒，响应上限 4 MiB；排队和限速等待另计。主进程负责重试、持久化、取消及到期终止回收，子进程仅负责联网。

免费额度仅适用于 `openrouter/free` 和以 `:free` 结尾的模型。首次确需免费 MT 时查询 `/key`，不为纯缓存恢复或结果重发创建网络客户端。仅读取 `data.free_model_daily_requests` 的非负整数 `used`、`limit`、`remaining`；共享范围为规范化根地址与 Key 指纹，不按模型拆分。并发查询合并，查询开始间隔至少 30 秒，429 重试前优先复核到期观察；等待 RPM 后再次核对。额度查询期间如有新冷却，发送前重新等待，最终放行时才登记 RPM 时间并原子预占额度。查询覆盖 UTC 日边界时不信任该观察，跨日必须查询新额度，不推定每日 1000 份。

每次 MT HTTP 尝试发送前原子预占一份本地余量，含所有重试；已登记的预占保守保留，已发送失败不退还。刷新扣除查询开始时的在途预占及查询期间新增预占，同一 UTC 日只向下校准可用余量。本轮已有同日可信观察时，查询失败／字段缺失可继续本地余量；首次未知或跨日无法确认时以 `failed/mt_quota_unknown` 停止，不能因此后备。可信额度不足返回 `mt_quota_exhausted`，启用后备时转百炼，关闭时以 failed 结束。由用户 `--resume` 继续，不自动等到次日或续跑。实际限制仍受账号其他调用和上游容量影响，见[官方限额说明](https://openrouter.ai/docs/api_reference/limits)。

翻译策略 3 的 user 消息依次包含原文上下文、翻译要求和同一来源句的有序片段；提示词指定源语言 `auto`（或人工确认的 MT 语言名）及简体中文目标。多片段请求包含固定边界示例，单片请求省略示例。百炼使用[自定义提示词模式](https://help.aliyun.com/zh/model-studio/machine-translation)，省略 `translation_options`，设置 `temperature:0`、`seed:0`；采样参数含义见[官方 API](https://help.aliyun.com/zh/model-studio/qwen-mt-api)。

内部批量接口接收上下文和有序片段，请求内编号从字符串 `"0"` 开始；输入、输出均为 `{"translations":[{"id":"0","text":"文字"}]}`，输出 `text` 为对应片段的译文。本地校验 JSON、编号完整且唯一、无多余项、译文非空且无字幕控制内容，以及 `finish_reason:stop`；原文包含文字或数字时，译文须包含文字或数字。校验异常使用同组输入和修复提示再尝试一次。

两次校验均失败时，按片段数中点拆成连续左右子组，在现有工作线程内先左后右执行；每组仍最多正常及修复各一次，递归至单片。子组编号从 `"0"` 开始，结果映射回原组编号及稳定 `cue_id`，沿用原字幕范围和时间。单片仍失败返回 `mt_batch_invalid`，原组保留待恢复。含 n 片的原组最多 `4n−2` 次逻辑请求；HTTP 错误、重试及取消按原规则处理。译文经 OpenCC t2s 后保存。结构校验不代表语义质量验收。

校验分类为响应外层结构异常、JSON 无效／重复键、编号缺失／重复／越界、译文无效、非正常结束。终止错误包含分类、预期／实际条数和原组片段范围（从 0 开始、右端不含）。校验失败和拆组事件追加到 `.build/mt-diagnostics/{job_id}.jsonl`，包含分类、数量、阶段、原组请求摘要及片段索引／时间范围；实际条数未知时为 null。

能读取响应外层时，校验失败事件另含 `finish_reason`、`content_type`、`json_root_type` 的本地枚举；字符串正文另含 `content_length`（字符数）和 `code_fence`（是否含三个反引号）。未知结束原因映射为 `other`，不保存服务端原值；JSON 无效为 `invalid`，非字符串正文的根类型为 `unavailable`。写入器只接受白名单枚举、非负整数和布尔值。百炼请求 ID 仍须通过格式校验；兼容服务请求 ID 始终为空，不保存正文、凭据或服务端消息。诊断写入失败时继续处理任务。

旧策略 1／2 保留单片翻译请求，顶层 `translation_options` 指定源语种及目标语种 `Chinese`，有上下文时增加 `tm_list:[{"source":"参考原文","target":"参考译文"}]`；旧单阶段任务保留原请求形式。响应须正常结束且译文合法、非空。

MT 按后端／规范化根地址／Key SHA-256／模型在进程内共享调度，并发可设为 1～4，默认 4。北京 `qwen-mt-flash` 额度为 60 RPM／35,000 TPM；本地在 61 秒滚动窗口内最多发送 60 次 HTTP 尝试，包含重试和失败，额外 1 秒用于网络到达时间余量。兼容后端按当前 `mt_rpm` 在 60 秒滚动窗口限制尝试次数，0 不主动限流；RPM 调整不改变共享调度身份。额度未满时无固定逐条间隔，用尽后逐条等待请求过期；窗口等待、共享冷却和自适应间隔取较晚期限，等待可取消。进度区分配额等待与服务端限流，配额等待不计入 `mt_cooldown_ms`。窗口不跨进程保存，账号其他调用及 TPM 限流由 429 退避处理。

每次发送记录调度代次；当前代次首次收到 429 时推进代次，将间隔升至至少 1.05 秒，后续代次再次限流时翻倍，最高 8 秒。同代次其余 429 只更新冷却、计数及重试。当前代次连续成功 4 次后间隔降低 30%，不足 0.1 秒时归零；任何 429 或当前代次其他失败重置成功窗口。旧代次成功不参与恢复，成功以 HTTP 2xx 计，译文仍须校验后才保存。间隔下降立即用于后续调度，并显示提速日志。

HTTP 429 按有效 `Retry-After` 秒数或 HTTP 日期冷却；缺失、非有限或负数值使用 15／30／60 秒退避。共享冷却截止时间取较晚值，等待可取消。每条请求最多尝试四次，最后一次 429 仍更新调度。失败停止安排后续请求，已发出的成功结果继续持久化。实际额度及账号共享规则见[官方限流说明](https://help.aliyun.com/zh/model-studio/rate-limit)。

## 数据与恢复

OpenRouter 契约 2 支持 `--mt-fallback {none,bailian}`，仅用于新任务、`--resume` 与 `--reflow`，新建／恢复省略时默认启用。普通暂时故障（含上述免费路由 `not_found`）每份正文最多 4 次 HTTP 尝试耗尽、格式修复／缩组最终失败或有效 Retry-After 超过 300 秒时，仅当前原组转百炼，后续组继续优先主后端，保留其冷却截止时间。部分子组成功后原组最终失败时，百炼重新翻译完整原组。可信免费额度耗尽才将本轮剩余工作转百炼；首次额度未知、永久错误、内容拒绝及未知业务错误停止。`content_filter` 与拒绝字段在格式恢复前识别。

百炼后备使用保存账户／工作空间的 `qwen-mt-flash`，可能产生费用，按实际使用计费。按自己的请求契约重建单条 user 消息，沿用提示词、`temperature:0`、`seed:0`、`stream:false`，不带 OpenRouter Schema、路由或插件，发送前重新核对百炼输入预算，见 [Qwen-MT API](https://help.aliyun.com/zh/model-studio/qwen-mt-api)。按[官方错误分类](https://help.aliyun.com/en/model-studio/error-code)映射安全类别，临时限流／超时／服务故障有限重试，鉴权、欠费、权限和内容拒绝立即停止。只保留本地类别及次数，不保留上游代码和原始消息。

两个后端分别使用共享限速器和冷却协调器，在同一工作线程内顺序执行，总并发保持配置值。百炼不等待主后端冷却，每个原组最多接管一次，不循环切回主后端。百炼后备使用同一隐藏子进程传输层，保持 120 秒总时限、4 MiB 响应上限、每份正文最多 4 次 HTTP 尝试及两端共享的 5 秒取消窗口；取消后不能启动后备。单后端失败交给组级调度，无法后备或两端均失败才启动全局收尾；两端失败使用 `mt_fallback_failed`，分别提示安全类别与尝试数。

`mt_fallback_policy` 是独立策略对象：`format_version:1`、`enabled:bool`、`backend`（百炼完整后端快照），旧任务首次初始化策略（含显式关闭）另含 `migration_snapshot`（`.build/mt-fallback/{job_id}/{时间}.json` 相对路径）。主 MT／执行快照、请求正文、提示词、分组及主缓存键不变。启用先校验原账号及当前两端凭据；首次初始化先保存包含任务、来源、字幕、参考和计数的本机恢复快照，再事务性写入策略并标记已有成功译文为主后端。重复设置相同策略幂等；关闭保留后备快照和已有来源，只影响后续请求；重排按新任务重新解析该选项。

译文 metadata 用 `mt_origin:primary|fallback` 分别引用主快照或后备快照。译文、来源、实际后端的普通／恢复缓存及成功计数同事务提交；百炼沿原百炼账户／工作空间范围，允许完全匹配的旧缓存，不写入 OpenRouter 键。未触发后备只查主缓存，触发后备才查百炼缓存。参考复用复制来源，复核或切分清空译文时一并清除来源；恢复与发布验证来源能对应保存的后端。每次显式 resume 重新获取所需额度，未完成原组重新优先主后端；已成功结果保留，运行中的尝试预算不跨进程累计。

任务库使用进程独占锁。任务绑定账号 Key 的 SHA-256 身份、地域、工作空间、接入方式、完整源文件指纹、模型参数、音轨／声道和媒体方案；缓存命中仍校验身份。已提交输入及分段方案保持固定。Ctrl+C 保存状态；取消任务由用户显式恢复。

OpenRouter 契约 2 及其百炼后备首次 Ctrl+C 立即停止新请求、重试、额度查询及后备切换，所有在途翻译共享 5 秒收尾窗口；窗口内完整合法的原组仍保存一次，到期终止其余请求，再次 Ctrl+C 跳过剩余窗口。无法继续的错误使用同一有界收尾。网络子进程、管道和工作线程结束后，才提交最终任务状态、关闭数据库并释放锁；迟到或不完整原组不写缓存。用户取消保存 `cancelled`，额度未知或关闭后备后的额度停止保持 `failed`。本地终止不能保证云端停止生成或计费，见 [OpenRouter 取消说明](https://openrouter.ai/docs/api_reference/streaming)。普通百炼、ASR 和旧请求使用原 HTTP 传输，取消等待请求返回或超时。

任务提交 `completed` 后清理音频缓存；清理失败保留文件并记录警告，完成状态保持有效。

发布顺序为：渲染并校验 SRT → 同事务保存发布意图 → 备份与原子写入 → 提交 completed → 尽力清理音频。manifest 的 `publication_intent` 保存 `target`（绝对路径）、`output_key`、`expected_srt_sha256`（本次渲染字节的 SHA-256）、`overwrite`、`original_sha256`（原目标不存在时为 null）和 `backup`（预定绝对备份路径，无原目标时为空）。顶层 `expected_srt_sha256` 与意图中的预期哈希保持一致。

覆盖与非覆盖统一恢复：目标已匹配预期内容时只补记完成，不翻译、不再备份或写入；此操作仍检查原账户及工作空间。未发布时目标须仍匹配原目标哈希或原有的不存在状态。备份写入临时文件并同步后才原子创建预定路径；已有备份匹配原目标哈希则复用，备份不符或目标被外部修改返回 `output_conflict`，保留全部文件。旧任务仅有顶层预期哈希时，只沿保存目标识别已发布内容，不推断历史备份归属。

更换目标、重排、复核或修改输出内容时，同一事务清除过期意图、预期哈希与显示时间调整摘要；同目标补传 `--overwrite` 保留有效意图。仅校时以新意图替换旧意图。预定备份路径在创建前即受普通报告路径保护。

`output` 保存绝对 SRT 路径，`output_key` 从该路径生成；相对 `--output-dir` 按调用目录解释。入队、恢复改目录及重排均检查目标冲突。

同一源文件与同一目标复用未结束任务，先检查该目标的全部任务冲突、账号／工作空间与 MT 身份。MT 后端、地址、模型或 Key 不一致时返回 `mt_config_changed`，须明确使用 `--resume` 沿原配置恢复或 `--reflow` 切换；报错前不修改覆盖选项或任务。`--overwrite` 启用覆盖，省略时沿用原设置；路径和覆盖选项在同一事务保存。不同目标新建任务；`--resume --output-dir` 修改未完成任务的目标。

旧相对路径在执行任务时解析：所得冲突键与保存值一致则固定为绝对路径，否则返回 `output_path_required`，由用户用 `--output-dir` 指定。路径确认先于输出目录准备和云端请求；重排的路径随重排事务提交。

新任务 manifest 保存以下版本和设置：

| 字段 | 值或内容 |
| --- | --- |
| `region` / `access_mode` | `beijing` / `workspace` |
| `workspace_id` | 1～63 位合法域名标签 |
| `processing_version` | `2` |
| `contract.normalizer` | `3` |
| `subtitle_version` / `translation_strategy_version` | `2` / `3` |
| `display_timing_version` | `2`；独立的最终显示时间策略 |
| `subtitle_settings` | 入队时的 `zh_line_width`、`other_line_width`、`chinese_first` |
| `execution_snapshot` | `format_version: 1`、`prompt_version: 9`、`recovery_version: 1`、`context_version: 1` |
| `mt_snapshot` | `format_version: 1`、`provider`、`base_url`、`model`、`key_fingerprint`（SHA-256）、`request_version: 1` 或 `2` |

`contract` 保存 ASR 模型、规范化版本与参数，读取兼容历史含 `mt:qwen-mt-flash` 的混合契约。MT 快照独立于 `cache_identity`（ASR 账户范围）；凭据仅存指纹，并发数和 RPM 使用运行设置。快照格式 1 读取请求契约 1、2；版本 2 仅允许 `openai_compatible` 配合精确的 `https://openrouter.ai/api/v1` 根地址。未知版本或非法版本 2 组合返回 `cache_version`。

普通恢复固定使用 `mt_snapshot` 中的后端、地址、模型和请求契约，不受当前 MT 选择影响；版本 1 的 OpenRouter 任务不会自动升级。在复用现有译文、缓存或重新发布 SRT 前验证对应的当前凭据指纹。不同 MT Key 返回 `mt_account_changed`。没有 MT 快照的旧任务按保存的百炼账户及工作空间处理，不增加快照、不升级翻译策略。已按保存发布意图写入的结果只补记完成时，仍按原规则校验 ASR 账户及工作空间，不要求旧 MT Key。

原始 ASR JSON 与规范化结果分别保存。完整句保存于 `subtitle-sources-v2.json` 和 SQLite `subtitle_sources`，参考译文存于 `mt_references`，最终片段及译文存于 `cues`。`normalized`、`normalized_count`、`references_planned`、`layout_planned`、`layout_round` 与对应记录同事务提交，恢复以数据库已提交状态为准。恢复校验来源及文字／词范围，复用成功结果，补齐必要阶段后提交 SRT。

空字幕由 `normalized_count=0` 确认。缺少此字段的旧任务使用空规范化文件或按保存版本验证的完整 raw；证据缺失或矛盾返回 `cache_mismatch`。

普通恢复按保存事实选择入口：完整有效的规范化记录直接进入复核／翻译／排版；否则使用完整 raw，或查询缺失 raw 对应的原 task_id。上述入口不需要媒体或 FFmpeg，但仍校验原 Key、账户范围、模型／版本、分段身份与时间偏移。规范化校验覆盖数量、唯一来源、全部文字／词范围及参考覆盖；旧任务缺少数量证明时须用规范化文件或完整 raw 核实，缺失或矛盾返回 `cache_mismatch`，不重建已提交结果。规范化完成后无需持续保有 raw；raw 仍保留用于显式重排。未解决的提交未知不会重发；需要新提交时仍要求原媒体和完整指纹一致。

输出位于原媒体目录且源文件仍存在时，首次继续处理和实际发布前均校验完整指纹；原路径媒体变化返回 `source_changed`，须另选目录的 `--output-dir`。源文件已移走或目标在其他目录时不读取源媒体内容。恢复不修改保存的源路径、指纹、音轨或分段身份。

恢复沿用 manifest 中的翻译策略 1、2 或 3，以及成功译文、参考、排版范围、轮次、设置及人工决定，仅补缺失项。未知版本返回 `cache_version`。显式 `--reflow` 使用当前策略 3 重建。

`display_timing_version` 为独立的显示策略整数版本，显式支持 `0`、`1`、`2`：缺失或 `0` 沿用旧渲染，新任务、`--reflow` 及 `--retime` 使用 `2`；未知版本返回 `cache_version`。普通 `--resume` 沿保存版本执行，v0／v1 算法及发布字节保持不变。最终排版完成后，从原始片段生成独立显示区间，保留来源、词时间和缓存身份。非零版本的 `display_timing_adjustments` 与发布意图、预期 SHA 同事务保存，待发布恢复时按已保存版本重新计算并核对摘要及字节，已准备的 v1 校时也可恢复；已匹配保存意图的目标直接补记完成。

新任务及成功准备的 `--reflow` 保存 `execution_snapshot`。带快照的未完成任务在复用翻译缓存、云端请求及重新发布前核对提示词、响应恢复和上下文版本，不受支持时返回 `cache_version`，需要显式 `--reflow`。`context_version` 支持 `0` 与 `1`，窗口规则见下文；缺少此字段或完全无快照时按版本 `0` 恢复，保留原快照形态。无快照的旧任务沿用原翻译策略。已按保存发布意图写入的结果可补记完成；重排准备失败保留原快照。线程数和轮询等待使用当前运行设置。

MT 缓存键包含账号、地域、工作空间、接入方式、媒体版本、模型、文字与 MT 源语言。任务策略 1／2 使用版本 3 缓存，另含阶段、请求兼容策略标识、ASR 语种及完整实际 `tm_list` 的摘要；两者共享策略标识 1，各字段一致时可跨字幕版本 1／2 复用。

任务策略 3 使用独立的版本 4 批量缓存，键包含策略、提示词版本、完整请求（含模型、实际发送的采样参数、语种、实际上下文和有序片段）、上述 ASR 账户范围、阶段及 ASR 语种，值保存完整编号到译文的映射。兼容后端另含完整 MT 身份快照（后端、地址、模型、Key 指纹及请求契约版本），不跨身份共享缓存。百炼固定模型／地址／Key 已受原 ASR 范围约束，保留旧缓存键算法，缺少 MT 快照的历史任务也保持原算法。并发和 RPM 不参与缓存身份。窗口版本变化后按实际请求匹配缓存，相同请求仍可复用。拆组恢复结果使用普通键摘要加恢复策略版本（当前为 1）的独立键，按普通缓存、恢复缓存的顺序查询，缓存内容为完整原组映射。请求内编号独立于来源句及稳定 `cue_id`。

各翻译阶段按完整缓存键合并同批请求。记录的 `attempts` 随实际参与的逻辑请求增加一次，包含策略 3 的修复及子组请求；HTTP 传输重试计入 `mt`。原组全部子组完成后，译文、完整缓存、完成及恢复成功计数同事务保存。失败写回全部组员并停止调度新组，其他在途成功组继续保存。取消时已完整返回的原组仍保存，部分返回的原组保留待恢复。策略 3 同组部分译文缺失时，按完整组缓存或重新请求恢复一致的分配。

`--reflow JOB_ID` 读取全部本地 `raw-{segment_id}.json`，沿用识别输入、偏移和 ASR 账户范围，按当前 MT 后端、模型、请求契约及排版设置重建来源，仅在需要翻译时联网。准备阶段校验当前 MT 配置及新凭据，不要求旧 MT Key；MT 身份变化后使用新身份的译文缓存或重新翻译，不重复 ASR。OpenRouter 从版本 1 重排至版本 2 时，两版完整请求及身份参与缓存键，原参考译文与字幕译文均不复用版本 1 缓存。源媒体可已移走；转写缺失或无效时保留原任务并报错。人工语种决定按完整来源对应，冲突或无法对应时返回 `reflow_review`。

重排先保存 `.build/reflow/{job_id}/{时间标识}.json` 本机任务快照，含原 manifest、来源、字幕、参考译文及计数。新来源另存 `subtitle-sources-v2-reflow-{时间标识}.json`；来源、片段、参考阶段重置、新 MT 快照、恢复标记和输出位置在同一 SQLite 事务提交。目标已存在时使用本次 `--overwrite` 参数。准备失败保留原任务；翻译失败保存可恢复进度，`--resume` 沿新 MT 快照继续。SRT 在翻译完成后备份并原子替换。快照和转写均属于本机私有数据。

`--retime JOB_ID` 从通过来源／词范围校验的最终字幕及译文更新显示时间，沿用原排版设置、ASR／MT 身份、识别输入、处理版本及人工决定，ASR、翻译及额度查询均为零；终端提示使用本地已保存结果，替代付费后备提示。没有字幕版本的历史任务使用 `output_settings`。缺少完整字幕、译文或排版设置时返回 `retime_incomplete`。校时校验原账户及 MT 凭据，已完成的翻译可跨提示词版本复用。旧显示版本因 `timeline` 失败、最终译文完整且当前显示版本试算通过时，错误提示推荐 `--retime`，避免重复执行相同的显示策略。

校时先在内存规划并验证内容，在 `.build/retime/{job_id}/{时间标识}.json` 保存原任务私有快照。新显示版本、调整摘要、发布意图及 SHA、排版设置、恢复标记与输出路径同事务提交；来源、片段、参考和译文保持原样。`retime` 标记保存 `snapshot`（本机相对路径）、`prepared_at`、`from_display_timing_version`。目标存在时使用本次 `--overwrite`，或用 `--output-dir` 另存；沿用备份、原子发布及外部修改冲突检查。准备失败保留原任务，提交后中断用 `--resume` 从保存结果继续。

ASR 提交 POST 由持久化 `submitting` 标记覆盖中断窗口。超时、5xx、无法解析或缺少 `task_id` 均保存为 `submission_unknown`，通过 `--bind-task-id` 绑定已知任务，或由用户以 `--allow-resubmit` 接受重复计费风险后重发。明确拒绝才清除标记；查询和下载失败继续沿用原 ID。旧任务仅有顶层提交标记或状态时，待提交分段唯一才能绑定或重发；取消后仍保留这项事实，须显式恢复。

百炼 Key 原子明文保存于 `data/key.txt`，`DASHSCOPE_API_KEY` 优先；独立兼容 MT Key 读取本机明文 `data/mt-key.txt`，`OPENAI_API_KEY` 优先。任务快照只保存指纹，配置文件不保存 Key。任务库明文保存上传引用；日志和普通报告隐藏凭据、签名链接、原文、译文及原始异常。终端语种复核可显示原文。

兼容读取规则：

- 缺少接入与媒体版本字段的任务按 `legacy_public`、媒体版本 1 读取，北京任务沿用公共端点和 WAV 方案。新加坡任务保留数据，恢复时在联网和缓存复用前返回 `legacy_region`。
- 缺少 `subtitle_version` 的任务沿用原断句、单阶段翻译、版本 2 MT 缓存和排版。已有有效规范化字幕及成功译文继续使用。
- 字幕版本 1 的任务普通恢复仍使用严格正时长词校验和原排版快照，包括尚未规范化的本地转写；仅 `--reflow` 明确升级为 2。
- 规范化版本 1 的任务在模型与参数相同时，可从尚未规范化的本地原始结果升级至 2；已有有效 v1 结果保持原样。版本 2 的任务沿保存版本恢复，只有新任务及显式 `--reflow` 使用规范化版本 3。
- `key.dpapi` 解密并成功写入新文件后移除。旧 `oss_protected` 仍可解密读取；解密失败时，仅尚未提交的分段可重新上传，已知 ID 或提交未知标记优先用于恢复。

来源记录及字幕／参考的 JSON metadata 使用 `decision_origin: automatic | manual` 表示决定来源，`reason` 只用于展示。新规范化结果为 automatic，终端确认同事务更新所属来源与全部片段为 manual，并清除失效译文与阶段标记；每条确认立即保存。没有来源记录的旧任务保留逐片复核。缺失字段仅以旧确认文案兼容恢复，显式未知值返回 `cache_mismatch`。

## 时间轴与字幕

媒体版本 2 优先复制兼容压缩音轨，并验证包时间戳、编码延迟／填充、音频属性和时长。多声道仅明确选择首声道时可复制；需要处理声道或时间轴时转换为 16kHz 单声道 FLAC。超限按 PCM 样本精确切分后编码，逐段校验容量；单段最长 12 小时，规格见[ASR 音频说明](https://help.aliyun.com/zh/model-studio/asr-model)。MIME 随实际容器使用 `audio/mp4`、`audio/mpeg`、`audio/flac`、`audio/ogg` 或旧 `audio/wav`。

`media_plan.extract_recipe_version` 新任务为 2，缺失按配方 1 读取，未知版本返回 `cache_version`。配方 2 的 OGG 复制启用 `-fflags +bitexact`，重建校验完整 SHA。旧 OGG 在源指纹不变、尚未规范化且所有分段均无 `task_id`、`submitting` 或 raw 时，可在需要重建时升级字节身份；原音轨、声道、分段和时间轴保持一致。升级原子保存计划及分段身份并清除旧上传引用。有效缓存、上传引用和已知任务 ID 优先复用。

PCM 中间文件使用 RIFF WAV，数据容量上限为 `UINT32_MAX - 4096` 字节（PCM16／16kHz／单声道约 37.28 小时）。提取前按音轨时间范围预检，缺失时使用容器时长；提取后及切分前核对实际大小与 RIFF／data 声明范围，切分时核对读取样本数。容量超限返回 `media_pcm_limit`，须先分割输入；文件截断或声明不一致返回 `media`。

规范化保留原文、源句索引和原始时间依据。零时长短句仅在句级与完整词级文字、标点和时间点一致，且语种支持、文字不超过 12 字时修复：补最多 1000ms 显示区间，受下一句和音频结尾限制；可用区间不足 400ms 时，可合并距离不超过 400ms 的同语种有效邻句。修复保存来源与警告，词时间不用于继续细分；其他无效时间返回错误。

规范化版本 3 在叠加全局偏移前校验句时间：起点位于段内，终点允许最多 1ms 取整误差，更大越界返回 `timeline`。可选的 `transcripts[].text` 非空而句子文字全空时返回 `response_schema`。原始结果保留供排查。

完整句的词时间须覆盖清理后的全部文字及标点，为整数、单调不重叠且位于句内。连续零时长词合并到下一个正时长词，句尾合并到前一词组；文字范围和首末时间端点均取自原记录，每组保持正时长。全句零时长、倒序、重叠、越界或文字不匹配时使用句级边界，句级修复或负时间裁剪后同样如此。片段 ID 由源句和词组范围稳定生成；无可靠词时间时使用完整文字范围。

句间识别时间重叠保留原始依据并记录警告。显示时间版本 1 自动处理同分段、不同来源句且来源及起止时间依次递增的 `0 < overlap <= 100ms` 冲突：将前条显示终点收至后条起点，保持所有起点不变。前条收尾后的时长和后条原时长均须至少 1000ms。

版本 2 沿用上述来源与区间结构限制，必须满足 `前起点 < 后起点 < 前终点 < 后终点`；重叠上限改为同时不超过 **500ms** 及前条原始显示时长的 **10%**，采用 `overlap * 10 <= 原始时长` 的整数比较。前条修整后与后条原时长均须至少 **1000ms**。连续冲突逐条收短前条终点；原始句、词时间、片段、译文及 `mt_origin` 不变。超限、同起点、包含、跨分段或来源倒序继续返回 `timeline`，提示区分重叠、比例、时长及结构限制，仅含来源 ID、时间范围和限制值。

v2 在生成布局后、每轮字幕翻译前及布局重分片提交前执行纯显示预检，失败时不发送该轮字幕 MT 请求，不提前保存调整摘要或发布意图，保留已有成功结果。发布前再次规划并全量校验显示区间为非负整数、正时长、单调且不重叠，相接端点允许相等。

`cues.begin_ms/end_ms` 保留来源及词范围依据。每条 `display_timing_adjustments` 包含 `cue_id`、`old_begin_ms/old_end_ms`、`new_begin_ms/new_end_ms`、`adjustment_ms`（提前终点的正毫秒数）及 `reason`：v1 为 `small_sentence_overlap`，v2 为 `bounded_sentence_overlap`。

原文或参考译文预计超过各两行，或时长超过 7 秒时尝试分段。断点依次选择句末标点／至少 400ms 停顿、逗号／分号、完整词边界，同级取文字量更均衡的位置。新片段使用真实首末词时间且至少 1000ms，原有更短句保留。最终译文超过两行时，仅重拆超限片段，最多追加两轮翻译；无法继续安全拆分时保留内容和时间并记录警告。

换行依次优化行数、英文短尾行和长度均衡，保留完整词及标点。宽字符计 2、普通字符计 1、组合附加符计 0；中文宽度设置乘 2，其他语言按原值。尾部标点允许最多 4 单位超宽，不可拆长词允许超宽。SRT 为 UTF-8 无 BOM、CRLF、合法毫秒时间，同目录原子提交，覆盖前备份；无语音时完成任务且不生成 SRT。

## 上下文与输入预算

先为完整 ASR 句生成排版参考。参考和字幕均校验源句、词／文字范围、时间与语种；整句字幕复用对应的单条参考译文，复用结果与 `mt_reference_reused` 计数同事务保存。所有译文参与最终排版检查。

邻句须具有一致 ASR 语种、翻译决定、MT 源语言及人工确认状态；遇到不兼容项立即停止该方向扩展。未知语种且未人工指定 MT 语言时不扩展邻句。

策略 3 的 `context_version: 1` 从目标完整句向前、向后各扩展最多 12 句，候选句与目标句最近边界的时间距离最多 30000ms，扩展路径上的相邻句间隔最多 15000ms，阈值均包含等号。候选句按时间距离、句子距离、来源位置排序。`context_version: 0` 及策略 1／2 使用前后各最多两句、路径间隔不超过 5000ms 的窗口。

策略 3 的参考与片段翻译均使用原文上下文。同一来源句的片段联合翻译，超预算时按连续片段分组，同句剩余片段优先于邻句作为上下文。排版追加拆分时，在同一事务清除受影响来源句全部片段译文，重新联合翻译。

失败子组优先以原组内未参与本次请求的片段作为上下文，按片段距离排列，再追加原有上下文；相同位置与文字的条目去重。超预算从末尾低优先级条目起整条裁剪，每次发送前均检查包含修复提示的完整 6000 输入预算。

策略 1／2 的片段使用固定参考译文构建 `tm_list`，优先所属整句，再取邻句；上下文为空且片段与参考单元范围完全匹配时也可复用参考。

本地输入预算为 6000：策略 3 按完整请求 JSON 的 UTF-8 字节数加 512 模板余量估算，包含修复提示；策略 1／2 按 `messages` 与实际 `translation_options` 加同等余量估算。上下文按完整条目裁剪，策略 3 优先保留连续片段组。单片超限只按可靠词时间及完整词边界拆分，无法安全拆分则保存 `mt_input_budget` 错误及恢复进度。

## 报告字段

报告在执行前及写入前检查路径，保护本次输入和预计输出、任务库全部输入／输出及 SRT 备份、`data`、`.build/reflow`、`.build/retime` 和 `.build/mt-fallback`。检查覆盖大小写与路径别名；冲突、备用数据流或无法核实的目标返回 `report_conflict`。报告使用原子替换，自检专用路径为 `data/self-test.json`。报告检查只读取任务路径及备份字段。

| `counts` 字段 | 含义 |
| --- | --- |
| `mt` / `mt_429` / `mt_success` | 已登记 HTTP 尝试（含重试及登记后发送前取消）／429 或 OpenRouter 同类业务限流／HTTP 2xx 次数 |
| `mt_reference_request` / `mt_subtitle_request` | 已登记合并后的参考／片段逻辑请求数，含修复和全部子组，可能在发送前取消；HTTP 重试计入 `mt` |
| `mt_reference_completed` / `mt_subtitle_completed` | 校验并保存成功的新参考／字幕译文记录数 |
| `mt_reference_cache_hit` / `mt_subtitle_cache_hit` / `mt_reference_reused` | 参考持久缓存／片段持久缓存命中／直接复用参考译文的记录数 |
| `mt_invalid_response` / `mt_split` | 校验失败响应数／原组或子组二分事件数 |
| `mt_recovery_success` | 拆组后完整映射成功提交的新请求组数，同键合并组计一次 |
| `mt_recovery_cache_hit` | 恢复缓存命中的记录数，同时计入对应阶段的缓存命中数 |
| `mt_cooldown_ms` | 共享冷却累计增量，重叠部分只计一次 |
| `mt_quota_query` | OpenRouter 免费额度查询登记次数，不占免费 MT 预占 |
| `mt_shared_wait_ms` / `mt_pacing_wait_ms` | OpenRouter 各工作线程等待共享恢复／其余 RPM 和间隔的累计耗时；并发线程分别累计，同一次等待的重叠部分归入共享恢复 |
| `mt_deadline_terminated` / `mt_drain_discarded` | OpenRouter 总时限终止尝试数／收尾中未保存的在途尝试或完整组数 |
| `mt_primary_request` / `mt_fallback_request` | 按实际后端登记的 HTTP 尝试数，包含重试 |
| `mt_primary_http_2xx` / `mt_fallback_http_2xx` | 分后端 HTTP 2xx 次数；`mt_success` 为所有后端的 HTTP 2xx 累计数，译文成功另计 |
| `mt_primary_success` / `mt_fallback_success` | 校验并同事务保存成功的新完整组数 |
| `mt_primary_completed` / `mt_fallback_completed` | 分后端新保存的译文记录数 |
| `mt_primary_cache_hit` / `mt_fallback_cache_hit` | 分后端匹配缓存的记录数；未触发后备不查询百炼缓存 |
| `mt_fallback_switch` | 原组进入百炼后备的次数，包含随后命中百炼缓存或后备失败 |
| `stage_*_ms` / `run_ms` | 各阶段／运行累计耗时，含冷却及网络等待 |

进度按记录计数。同批四条同键记录共享新译文时，请求数增加 1、完成数增加 4；缓存命中数只统计读取持久缓存的记录。

HTTP、MT 和上传在发送前检查取消；已登记尝试及占用额度保留，在途成功结果继续保存。ASR 提交未知状态按前述恢复规则处理。

`timings` 将耗时换算为秒；计数及耗时跨恢复累计。报告的 `mt_provider` 和 `models.mt` 读取任务实际 MT 快照，历史任务为 `bailian`／`qwen-mt-flash`；不使用当前模型选择，也不输出 MT 地址或指纹。报告另含任务状态、版本、排版轮次、超限警告、`display_timing_version` 与上述调整摘要；仅校时另含 `retime.prepared_at/from_display_timing_version`，省略私有路径、凭据、上传链接和字幕文字。

OpenRouter 运行诊断通过独立回调追加至同一 `.build/mt-diagnostics/{job_id}.jsonl`，`kind` 为 `runtime_request`、`runtime_wait`、`runtime_quota` 或 `runtime_drain`；不计入 `mt_invalid_response`／`mt_split`。仅记录安全分类、尝试序号、HTTP 状态、请求耗时、共享／限速等待、是否截止终止及收尾丢弃数；额度事件仅含上述整数、本地可用余量及 UTC 观察时间。不保留账号其他字段、上游消息、原始错误码或正文。报告通过状态和静态错误码区分额度停止与用户取消，`timings` 另含 `mt_shared_wait_seconds` 和 `mt_pacing_wait_seconds`。

后备诊断另有 `runtime_fallback`／`runtime_cache`，附本地 32 位十六进制 `run_id`、64 位原组标识、实际 `backend:primary|fallback`、`provider:openrouter|bailian|openai_compatible` 及白名单切换原因。格式事件也附运行和后端归属；HTTP 接收成功与完整译文提交分开计数。普通报告的 `mt_fallback` 仅含启用状态、后备提供者与模型，不输出后备地址、指纹或私有快照路径。
