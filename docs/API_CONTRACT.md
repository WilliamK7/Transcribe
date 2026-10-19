# 接口与恢复契约

## 云端请求

| 接口 | 请求 | 官方来源 |
| --- | --- | --- |
| ASR | POST `https://{workspace_id}.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/asr/transcription` | [Qwen-ASR](https://help.aliyun.com/zh/model-studio/qwen-asr-api-reference) |
| 查询 | GET 同一工作空间 `/api/v1/tasks/{task_id}` | 同上 |
| MT | POST 同一工作空间 `/compatible-mode/v1/chat/completions` | [Qwen-MT](https://help.aliyun.com/zh/model-studio/qwen-mt-api) |
| 上传策略 | GET `https://dashscope.aliyuncs.com/api/v1/uploads`，`action=getPolicy`，`model=qwen3-asr-flash-filetrans` | [临时上传](https://help.aliyun.com/zh/model-studio/get-temporary-file-url) |
| 文件上传 | 返回的 `upload_host`，流式整文件 multipart，`file` 字段最后，使用上传策略鉴权 | 同上 |

上传使用系统默认代理，容量取 1,000,000,000 字节与策略 `max_file_size_mb` 的较小值，每次获取策略后重新检查。上传与结果下载均省略百炼 Authorization。

ASR 模型为 `qwen3-asr-flash-filetrans`，输入为单数 `input.file_url`。`X-DashScope-Async`、`X-DashScope-OssResourceResolve` 均为 `enable`；`parameters` 为 `channel_id:[0]`、`enable_words:true`、`enable_itn:false`，省略 `language` 以自动识别。提交成功即持久化 `output.task_id`；查询成功后从 `output.result.transcription_url` 下载结果，按 `transcripts[].sentences[]` 解析。

MT 模型为 `qwen-mt-flash`，`stream:false`，每次一条 user 文本消息。中文／粤语跳过 MT，语种复核在所有 MT 请求前完成。

翻译策略 3 使用[官方支持的自定义提示词](https://help.aliyun.com/zh/model-studio/machine-translation)，user 消息依次包含原文上下文、翻译要求和同一来源句的有序片段；提示词指定源语言 `auto`（或人工确认的 MT 语言名）及简体中文目标。请求省略 `translation_options`，设置 `temperature:0`、`seed:0`，采样参数不保证云端严格确定性，含义见[官方 API](https://help.aliyun.com/zh/model-studio/qwen-mt-api)。多片段请求包含固定边界示例，单片请求省略示例。

内部批量接口接收上下文和有序片段，请求内编号从字符串 `"0"` 开始；输入、输出均为 `{"translations":[{"id":"0","text":"文字"}]}`，输出 `text` 为对应片段的译文。本地校验 JSON、编号完整且唯一、无多余项、译文非空且无字幕控制内容，以及 `finish_reason:stop`；原文包含文字或数字时，译文须包含文字或数字。结构异常使用同组输入和修复提示再尝试一次，仍失败返回 `mt_batch_invalid`，整组保留待恢复。译文经 OpenCC t2s 后按编号映射回稳定 `cue_id`。结构校验不代表语义质量验收。

旧策略 1／2 保留单片翻译请求，顶层 `translation_options` 指定源语种及目标语种 `Chinese`，有上下文时增加 `tm_list:[{"source":"参考原文","target":"参考译文"}]`；旧单阶段任务保留原请求形式。响应须正常结束且译文合法、非空。

MT 按 Key／地域／模型在进程内共享调度，并发可设为 1～4，默认 4。北京 `qwen-mt-flash` 额度为 60 RPM／35,000 TPM；本地在 61 秒滚动窗口内最多发送 60 次 HTTP 尝试，包含重试和失败，额外 1 秒用于网络到达时间余量。额度未满时无固定逐条间隔，用尽后逐条等待最早请求过期；窗口等待、共享冷却和自适应间隔取较晚期限，等待可取消。进度区分配额等待与服务端限流，配额等待不计入 `mt_cooldown_ms`。窗口不跨进程保存，账号其他调用及 TPM 限流由 429 退避处理。

每次发送记录调度代次；当前代次首次收到 429 时推进代次，将间隔升至至少 1.05 秒，后续代次再次限流时翻倍，最高 8 秒。同代次其余 429 只更新冷却、计数及重试。当前代次连续成功 4 次后间隔降低 30%，不足 0.1 秒时归零；任何 429 或当前代次其他失败重置成功窗口。旧代次成功不参与恢复，成功以 HTTP 2xx 计，译文仍须校验后才保存。间隔下降立即用于后续调度，并显示提速日志。

HTTP 429 按有效 `Retry-After` 秒数或 HTTP 日期冷却；缺失、非有限或负数值使用 15／30／60 秒退避。共享冷却截止时间取较晚值，等待可取消。每条请求最多尝试四次，最后一次 429 仍更新调度。失败停止安排后续请求，已发出的成功结果继续持久化。实际额度及账号共享规则见[官方限流说明](https://help.aliyun.com/zh/model-studio/rate-limit)。

## 数据与恢复

任务库使用进程独占锁。任务绑定账号 Key 的 SHA-256 身份、地域、工作空间、接入方式、完整源文件指纹、模型参数、音轨／声道和媒体方案；缓存命中仍校验身份。已提交输入及分段方案保持固定。Ctrl+C 保存状态；取消任务由用户显式恢复。

任务提交 `completed` 后清理音频缓存；清理失败保留文件并记录警告，完成状态保持有效。

`output` 保存绝对 SRT 路径，`output_key` 从该路径生成；相对 `--output-dir` 按调用目录解释。入队、恢复改目录及重排均检查目标冲突。

同一源文件与同一目标复用未结束任务，先检查该目标的全部任务冲突及账号／工作空间身份。`--overwrite` 启用覆盖，省略时沿用原设置；路径和覆盖选项在同一事务保存。不同目标新建任务；`--resume --output-dir` 修改未完成任务的目标。

旧相对路径在执行任务时解析：所得冲突键与保存值一致则固定为绝对路径，否则返回 `output_path_required`，由用户用 `--output-dir` 指定。路径确认先于输出目录准备和云端请求；重排的路径随重排事务提交。

新任务 manifest 保存以下版本和设置：

| 字段 | 值或内容 |
| --- | --- |
| `region` / `access_mode` | `beijing` / `workspace` |
| `workspace_id` | 1～63 位合法域名标签 |
| `processing_version` | `2` |
| `contract.normalizer` | `3` |
| `subtitle_version` / `translation_strategy_version` | `2` / `3` |
| `subtitle_settings` | 入队时的 `zh_line_width`、`other_line_width`、`chinese_first` |

原始 ASR JSON 与规范化结果分别保存。完整句保存于 `subtitle-sources-v2.json` 和 SQLite `subtitle_sources`，参考译文存于 `mt_references`，最终片段及译文存于 `cues`。`normalized`、`normalized_count`、`references_planned`、`layout_planned`、`layout_round` 与对应记录同事务提交，恢复以数据库已提交状态为准。恢复校验来源及文字／词范围，复用成功结果，补齐必要阶段后提交 SRT。

空字幕由 `normalized_count=0` 确认。缺少此字段的旧任务使用空规范化文件或按保存版本验证的完整 raw；证据缺失或矛盾返回 `cache_mismatch`。

恢复沿用 manifest 中的翻译策略 1、2 或 3，以及成功译文、参考、排版范围、轮次、设置及人工决定，仅补缺失项。未知版本返回 `cache_version`。显式 `--reflow` 使用当前策略 3 重建。

MT 缓存键包含账号、地域、工作空间、接入方式、媒体版本、模型、文字与 MT 源语言。任务策略 1／2 使用版本 3 缓存，另含阶段、请求兼容策略标识、ASR 语种及完整实际 `tm_list` 的摘要；两者共享策略标识 1，各字段一致时可跨字幕版本 1／2 复用。

任务策略 3 使用独立的版本 4 批量缓存，键包含策略、提示词版本、完整请求（含模型、采样参数、语种、实际上下文和有序片段）、上述账户范围、阶段及 ASR 语种，值保存完整编号到译文的映射。请求内编号独立于来源句及稳定 `cue_id`，上下文或分组变化时重新匹配缓存。

各翻译阶段按完整缓存键合并同批请求。组内每条记录的 `attempts` 随逻辑请求增加一次，策略 3 的修复请求也计入；HTTP 传输重试计入 `mt`。一组译文、完整缓存和计数同事务保存。失败写回全部组员并停止调度新组，在途成功结果继续保存。取消或失败后可恢复；策略 3 同组部分译文缺失时，按完整组缓存或重新请求恢复一致的分配。

`--reflow JOB_ID` 读取全部本地 `raw-{segment_id}.json`，沿用识别输入、偏移和账户范围，按当前排版设置重建来源，仅在需要翻译时联网。源媒体可已移走；转写缺失或无效时保留原任务并报错。人工语种决定按完整来源对应，冲突或无法对应时返回 `reflow_review`。

重排先保存 `.build/reflow/{job_id}/{时间标识}.json` 本机任务快照，含原 manifest、来源、字幕、参考译文及计数。新来源另存 `subtitle-sources-v2-reflow-{时间标识}.json`；来源、片段、参考阶段重置、恢复标记和输出位置在同一 SQLite 事务提交。目标已存在时使用本次 `--overwrite` 参数。准备失败保留原任务；翻译失败保存可恢复进度。SRT 在翻译完成后备份并原子替换。快照和转写均属于本机私有数据。

ASR 提交 POST 由持久化 `submitting` 标记覆盖中断窗口。超时、5xx、无法解析或缺少 `task_id` 均保存为 `submission_unknown`，通过 `--bind-task-id` 绑定已知任务，或由用户以 `--allow-resubmit` 接受重复计费风险后重发。明确拒绝才清除标记；查询和下载失败继续沿用原 ID。

Key 原子明文保存于 `data/key.txt`，`DASHSCOPE_API_KEY` 优先。任务库明文保存上传引用；日志和普通报告隐藏凭据、签名链接、原文、译文及原始异常。终端语种复核可显示原文。

兼容读取规则：

- 缺少接入与媒体版本字段的任务按 `legacy_public`、媒体版本 1 读取，北京任务沿用公共端点和 WAV 方案。新加坡任务保留数据，恢复时在联网和缓存复用前返回 `legacy_region`。
- 缺少 `subtitle_version` 的任务沿用原断句、单阶段翻译、版本 2 MT 缓存和排版。已有有效规范化字幕及成功译文继续使用。
- 字幕版本 1 的任务普通恢复仍使用严格正时长词校验和原排版快照，包括尚未规范化的本地转写；仅 `--reflow` 明确升级为 2。
- 规范化版本 1 的任务在模型与参数相同时，可从尚未规范化的本地原始结果升级至 2；已有有效 v1 结果保持原样。版本 2 的任务沿保存版本恢复，只有新任务及显式 `--reflow` 使用规范化版本 3。
- `key.dpapi` 解密并成功写入新文件后移除。旧 `oss_protected` 仍可解密读取；解密失败时，仅尚未提交的分段可重新上传，已知 ID 或提交未知标记优先用于恢复。

## 时间轴与字幕

媒体版本 2 优先复制兼容压缩音轨，并验证包时间戳、编码延迟／填充、音频属性和时长。多声道仅明确选择首声道时可复制；需要处理声道或时间轴时转换为 16kHz 单声道 FLAC。超限按 PCM 样本精确切分后编码，逐段校验容量；单段最长 12 小时，规格见[ASR 音频说明](https://help.aliyun.com/zh/model-studio/asr-model)。MIME 随实际容器使用 `audio/mp4`、`audio/mpeg`、`audio/flac`、`audio/ogg` 或旧 `audio/wav`。

`media_plan.extract_recipe_version` 新任务为 2，缺失按配方 1 读取，未知版本返回 `cache_version`。配方 2 的 OGG 复制启用 `-fflags +bitexact`，重建校验完整 SHA。旧 OGG 在源指纹不变、尚未规范化且所有分段均无 `task_id`、`submitting` 或 raw 时，可在需要重建时升级字节身份；原音轨、声道、分段和时间轴保持一致。升级原子保存计划及分段身份并清除旧上传引用。有效缓存、上传引用和已知任务 ID 优先复用。

PCM 中间文件使用 RIFF WAV，数据容量上限为 `UINT32_MAX - 4096` 字节（PCM16／16kHz／单声道约 37.28 小时）。提取前按音轨时间范围预检，缺失时使用容器时长；提取后及切分前核对实际大小与 RIFF／data 声明范围，切分时核对读取样本数。容量超限返回 `media_pcm_limit`，须先分割输入；文件截断或声明不一致返回 `media`。

规范化保留原文、源句索引和原始时间依据。零时长短句仅在句级与完整词级文字、标点和时间点一致，且语种支持、文字不超过 12 字时修复：补最多 1000ms 显示区间，受下一句和音频结尾限制；可用区间不足 400ms 时，可合并距离不超过 400ms 的同语种有效邻句。修复保存来源与警告，词时间不用于继续细分；其他无效时间返回错误。

规范化版本 3 在叠加全局偏移前校验句时间：起点位于段内，终点允许最多 1ms 取整误差，更大越界返回 `timeline`。可选的 `transcripts[].text` 非空而句子文字全空时返回 `response_schema`。原始结果保留供排查。

完整句的词时间须覆盖清理后的全部文字及标点，为整数、单调不重叠且位于句内。连续零时长词合并到下一个正时长词，句尾合并到前一词组；文字范围和首末时间端点均取自原记录，每组保持正时长。全句零时长、倒序、重叠、越界或文字不匹配时使用句级边界，句级修复或负时间裁剪后同样如此。片段 ID 由源句和词组范围稳定生成；无可靠词时间时使用完整文字范围。

原文或参考译文预计超过各两行，或时长超过 7 秒时尝试分段。断点依次选择句末标点／至少 400ms 停顿、逗号／分号、完整词边界，同级取文字量更均衡的位置。新片段使用真实首末词时间且至少 1000ms，原有更短句保留。最终译文超过两行时，仅重拆超限片段，最多追加两轮翻译；无法继续安全拆分时保留内容和时间并记录警告。

换行依次优化行数、英文短尾行和长度均衡，保留完整词及标点。宽字符计 2、普通字符计 1、组合附加符计 0；中文宽度设置乘 2，其他语言按原值。尾部标点允许最多 4 单位超宽，不可拆长词允许超宽。SRT 为 UTF-8 无 BOM、CRLF、合法毫秒时间，同目录原子提交，覆盖前备份；无语音时完成任务且不生成 SRT。

## 上下文与输入预算

先为完整 ASR 句生成排版参考。参考和字幕均校验源句、词／文字范围、时间与语种；整句字幕复用对应的单条参考译文，复用结果与 `mt_reference_reused` 计数同事务保存。所有译文参与最终排版检查。

邻句按时间距离选择前后各最多两句。相邻句须具有一致 ASR 语种、翻译决定、MT 源语言及人工确认状态，扩展路径上的间隔不超过 5000ms；未知语种且未人工指定 MT 语言时不扩展邻句。

策略 3 的参考与片段翻译均使用原文上下文。同一来源句的片段联合翻译，超预算时按连续片段分组，同句剩余片段优先于邻句作为上下文。排版追加拆分时，在同一事务清除受影响来源句全部片段译文，重新联合翻译。

策略 1／2 的片段使用固定参考译文构建 `tm_list`，优先所属整句，再取邻句；上下文为空且片段与参考单元范围完全匹配时也可复用参考。

本地输入预算为 6000：策略 3 按完整请求 JSON 的 UTF-8 字节数加 512 模板余量估算，包含修复提示；策略 1／2 按 `messages` 与实际 `translation_options` 加同等余量估算。上下文按完整条目裁剪，策略 3 优先保留连续片段组。单片超限只按可靠词时间及完整词边界拆分，无法安全拆分则保存 `mt_input_budget` 错误及恢复进度。

## 报告字段

报告在执行前及写入前检查路径，保护本次输入和预计输出、任务库全部输入／输出及 SRT 备份、`data` 和 `.build/reflow`。检查覆盖大小写与路径别名；冲突、备用数据流或无法核实的目标返回 `report_conflict`。报告使用原子替换，自检专用路径为 `data/self-test.json`。报告检查只读取任务路径及备份字段。

| `counts` 字段 | 含义 |
| --- | --- |
| `mt` / `mt_429` / `mt_success` | 已登记 HTTP 尝试（含重试及登记后发送前取消）／429／2xx 次数 |
| `mt_reference_request` / `mt_subtitle_request` | 已登记合并后的参考／片段翻译尝试数，可能在发送前取消；HTTP 重试计入 `mt` |
| `mt_reference_completed` / `mt_subtitle_completed` | 校验并保存成功的新参考／字幕译文记录数 |
| `mt_reference_cache_hit` / `mt_subtitle_cache_hit` / `mt_reference_reused` | 参考持久缓存／片段持久缓存命中／直接复用参考译文的记录数 |
| `mt_cooldown_ms` | 共享冷却累计增量，重叠部分只计一次 |
| `stage_*_ms` / `run_ms` | 各阶段／运行累计耗时，含冷却及网络等待 |

进度按记录计数。同批四条同键记录共享新译文时，请求数增加 1、完成数增加 4；缓存命中数只统计读取持久缓存的记录。

HTTP、MT 和上传在发送前检查取消；已登记尝试及占用额度保留，在途成功结果继续保存。ASR 提交未知状态按前述恢复规则处理。

`timings` 将耗时换算为秒；计数及耗时跨恢复累计。报告另含任务状态、版本、排版轮次及超限警告，省略凭据、上传链接和字幕文字。
