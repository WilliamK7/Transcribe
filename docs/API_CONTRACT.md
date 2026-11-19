# 接口与恢复契约

## 上游 ASR

新任务使用北京百炼 `qwen-audio-3.1-asr-flash-filetrans`，`input.file_urls` 数组传一个 URL。参数为 `channel_id:[0]`、`keep_dialect:true`、`special_word_filter:'{"system_reserved_filter":false}'`，保留方言和原词，固定返回字词时间戳，按 token 计费。模型、参数、工作空间、接入方式和账户指纹在入队时保存。[官方接口](https://docs.bailian.console.aliyun.com/zh/model-studio/qwen-audio-asr-filetrans-http-api)

`qwen3-asr-flash-filetrans` 历史任务沿用 `input.file_url` 及 `channel_id:[0]`、`enable_words:true`、`enable_itn:false`；无识别快照的历史队列也按该契约恢复。上传、提交、恢复和本地重建保留原模型。[历史接口](https://help.aliyun.com/zh/model-studio/qwen-asr-api-reference)

| 操作 | 接口与重试 |
| --- | --- |
| 上传 | `GET https://dashscope.aliyuncs.com/api/v1/uploads` 取临时策略，每次重试构造新表单和对象。OSS 上传仅用返回的表单凭证。[临时上传](https://help.aliyun.com/zh/model-studio/get-temporary-file-url) |
| 提交 | `POST https://{workspace_id}.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/asr/transcription`。发送前保存提交标记，请求携带 `X-DashScope-Async: enable` 和 `X-DashScope-OssResourceResolve: enable`。结果不确定时进入 `submission_unknown`。 |
| 查询与下载 | 在原工作空间 `GET /api/v1/tasks/{task_id}` 轮询已保存的 ID。3.1 校验 `output.results` 恰有一个 `subtask_status:SUCCEEDED` 的结果，再读其 `transcription_url`；旧模型读取 `output.result.transcription_url`。成功后下载原始转写，下载请求省略百炼认证头。 |

结果读取 `transcripts[].sentences[].words`。3.1 的逐句 `language` 可缺失：词时间通过完整性和顺序校验后可用于拆句，语种由文字规则或人工复核决定。零时长句的跨句合并需要同语种证据。

## Magpie 网关契约

翻译走 `POST /v1/chat/completions`。请求使用 `X-Magpie-Response-Model: member` 取得实际成员，通过 `X-Magpie-Session: transcribe-<任务ID>-<请求ID>` 对应网关记录。主组依次选择免费或试用、商业入口。同档供应商等权，供应商内部有序尝试，各组 `stays=off`。六个角色入口见 [README](../README.md#配置-magpie)。[Magpie 分组命令](https://github.com/yetone/magpie/blob/v0.1.4468/internal/cli/groups_cli.go)

| 主组结果 | 本阶段处理 |
| --- | --- |
| 正文正常结束但格式、编号无效 | 从原输入修复一次，仍失败则切换商业后备 |
| 术语、复核或裁决正常结束但输出无效，或实际型号无法验证 | 尝试本角色商业后备 |
| 截断或请求超时 | 结束超时子进程后尝试商业后备 |
| 连接、HTTP、拒绝或内容过滤错误 | 记录阶段失败，上游重试由网关负责 |

阶段总时限 180 秒，通常给主组 120 秒，后备可使用全部剩余时间。正文后备也可修复一次，一阶段最多四次请求。正文失败会保存进度并停止发布。术语失败时使用可用的人工术语，复核或裁决失败时保留完整译文并告警。

本机根据请求地址找到对应的 Magpie 注册表。`actual_model` 表示网关成员，`resolved_model` 表示底层型号。正文、复核和裁决使用独立型号。动态入口 vLLM-SR `auto` 用于正文，`X-Vsr-Selected-Model` 给出已验收的底层型号。阶段恢复和跨任务缓存采用同样的身份与内容校验。

渠道策略存于 `data/mt-channels.json`，格式版本为 2。`include` 默认匹配全部型号，`exclude` 优先。`preferred` 决定发现顺序；`billing`、单价和 `price_exceptions` 决定新验收许可。`excluded_providers` 优先于登记和评判者清单，推理时展开嵌套组核对。目录故障时保留身份仍有效的既有资格。[Magpie 渠道选项](https://github.com/yetone/magpie/blob/v0.1.4468/internal/provider/provider.go) · [Vercel 价格](https://vercel.com/docs/ai-gateway/models-and-providers) · [OpenRouter 目录](https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties)

候选和独立评判者使用合成样例验收，每次请求时限 120 秒。维护预算按候选计数，在发起验收前扣除。百炼试用最多同时接入三个合格型号，绑定原账户、工作空间及可信查询身份，共享 RPM。通常每 60 秒查询额度，失败时标为 `unknown` 并退避。暂停、人工排除、已确认的额度或账户拒绝优先于旧资格。[百炼用量](https://help.aliyun.com/zh/model-studio/cli/usage-quota) · [模型限额](https://help.aliyun.com/zh/model-studio/list-quotas)

分组写入先备份、预演并校验，然后写子组，最后切换主入口。每条命令的前后状态写入 `gateway-apply-pending.json`。若命令成功但回执丢失，下次重新核对实际成员、人工开关和托管归属。无法判断时返回 `magpie_group` 并保留记录。`gateway-managed.json` 保存归属，`gateway-maintenance.json` 保存候选资格、冷却和日预算，`bailian-trial-state.json` 保存试用记录。网关分组写入和试用角色分配共用一把锁，推理在锁外执行。

## 字幕翻译与质量

术语提取、正文、独立复核和疑难裁决使用非流式 JSON。每组输入最多 32768 字节，输出最多 4096 token。术语和正文共用 `mt_workers`。新任务的 `window-independent-v4` 每个术语窗口最多 8 组，最多提前准备两个窗口，按原文顺序合入术语，人工译法优先。旧 `window-prefix-v3` 沿用原任务的串行前缀。

完整原句、片段编号、字符范围和显示时长构成请求依据。相邻上下文在语种和人工决定一致时扩展，每侧最多 12 句。超预算时依次裁剪远处邻句、可选的同句证据和术语，保留完整目标和拆句边界两侧最近的同句证据。首个请求前保存分组，正文前保存共用的语义输入，恢复时按已保存的范围继续。

复核逐片配对原文和译文，并连读跨屏句。相邻不同原文出现大段重复译文时交独立型号裁决。只修改确认出错的原句，最多纠正两轮；无变化、停滞或循环时提前结束。证据不足保留完整译文并告警。

## 数据与恢复

一个进程独占任务库。状态转移、来源、译文和阶段证据通过 SQLite 事务提交。`--jobs` 显示最后保存的状态和可执行的下一步。原始识别响应和各阶段内容属于私有任务资料。

| 操作 | 所用证据及边界 |
| --- | --- |
| `--resume` | 校验原账户、模型、语种决定、排版和执行版本。分段有完整转写就读取本地，有云端 ID 就查询原任务，需要提交时才要求原媒体。未知提交只可绑定已有 ID 或明确同意重发。 |
| `--reflow` | 从完整原始转写重新规范化，按当前配置重排，保存能对应原句的人工决定。重建前写恢复快照，在同一事务中替换来源、布局和阶段标记。对归档执行时指定输出目录并创建新任务。 |
| `--retime` | 从已有的最终字幕和通过校验的译文计算显示时间。准备快照和发布意图后，全程在本机完成。 |
| `--export-archive` | 校验 `data/archives/{任务ID}` 的逐文件 SHA-256 索引、原始转写和 SRT，再导出原字幕字节。 |

执行快照支持提示词 5、7 和断句 3、5。并发和轮询等待取当前设置。`cache_version` 表示需从原始转写重建，`cache_mismatch` 表示来源、片段范围或翻译证据不一致。翻译缓存绑定实际请求、后端身份、能力、账户和策略版本。来源、分组摘要、译文和产出阶段相互校验。人工语种复核或重新分片时，同事务清除失效结果。

发布顺序为渲染校验、保存发布意图、备份并原子替换、提交完成、清理音频。中断恢复时按目标路径、期望哈希、原哈希和预定备份核对。目标已包含期望字节就补记完成，目标或备份被外部改动返回 `output_conflict`。`Ctrl+C` 停止新请求，在途翻译共享最多 5 秒收尾时间，再次中断立即结束等待。

## 时间轴与字幕

优先复制可验证的压缩音轨。声道或时间轴需要处理时转换为 16kHz 单声道 FLAC，保留偏移和编码延迟。单段最长 12 小时，容量超限按 PCM 样本切分。中间 RIFF WAV 数据上限为 `UINT32_MAX - 4096` 字节，超限返回 `media_pcm_limit`，需先分割媒体。[ASR 音频规格](https://help.aliyun.com/zh/model-studio/asr-model)

规范化保留原始文字、句索引及时间。只有完整词级证据支持时才修复同点句或裁剪段末最后一句。可靠词时间应完整覆盖原文、保持单调并落在句内。长句沿真实词边界拆分，优先句末标点或至少 400ms 的停顿。超过两行或 7 秒尝试拆分，译文超限最多追加两轮。缺少可靠断点时保留全文并记录告警。

显示时间与来源时间分别保存。同段相邻来源句的有限交叉可收短前条终点或推迟后条起点。单次重叠最多 500ms，每片累计限缩最多 500ms 且不超过原时长的 10%，调整后至少 1000ms。`display_timing_adjustments` 记录片段、前后端点、调整量和原因。每轮翻译前及发布前校验时间轴，无法消解的冲突返回 `timeline`。SRT 使用 UTF-8 无 BOM、CRLF 和毫秒时间戳。无语音时任务完成且不生成字幕文件。

## 报告字段

任务结果包含 `job_id`、`state`、输入输出路径、`counts`、`error_code`、`request_id` 和产出元数据。`lifecycle.next_actions` 提供操作 `id`、`argv`、`required_options` 和 `requires_confirmation`。报告写入前校验输入、字幕、备份和私有资料，包括 Windows 路径别名与备用数据流。

| 计数字段 | 含义 |
| --- | --- |
| `mt` / `mt_429` / `mt_success` | 已登记请求、限流响应、HTTP 2xx 响应 |
| `mt_subtitle_completed` | 与译文同事务提交的片段数 |
| `mt_v6_{入口ID}_{阶段}_request` | 各入口登记的阶段请求 |
| `mt_v6_{阶段}_completed` / `_cache_hit` / `_failure` | 阶段完成、请求缓存命中、候选失败 |
| `mt_invalid_response` / `mt_output_truncated` | 校验失败响应及其中的截断响应 |
| `mt_input_tokens` / `mt_output_tokens` / `mt_usage_unknown` | 通过字幕校验的响应所报告的用量 |
| `mt_response_*` | 全部已收响应的输入、输出、推理 token 或缺失用量 |
| `mt_format_repair_*` / `mt_paid_fallback_*` | 格式修复及商业后备的请求和成功次数 |
| `mt_{trial,free,commercial,unknown}_response` | 按实际成员的费用分类计数，现金支出和权益抵扣以账单为准 |
| `mt_quality_warning` / `mt_glossary_unavailable` / `mt_correction_stalled` | 复核警告、术语不可用及纠正停滞 |
| `stage_*_ms` / `run_ms` | 跨恢复累计耗时 |

`.build/mt-diagnostics/{job_id}.jsonl` 中的 `request_start` 表示登记尝试，`request_end` 记录结果分类、HTTP 状态、耗时和可读用量。`route_complete.attempt` 是阶段总尝试数，缓存命中为 0。用 `gateway_session` 在 Magpie 的 Routing／Usage 中核对上游实际尝试。诊断和普通报告省略凭据、签名链接及字幕正文。
