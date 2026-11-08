# 接口与恢复契约

## magpie 网关契约

翻译通过 magpie 字幕组，ASR 通过原生代理调用北京百炼。契约依据 [官方接入说明](https://usemagpie.ai/docs/integrate)、[CLI 参考](https://github.com/yetone/magpie/blob/v0.1.3323/docs/reference.md)及安装版本 **0.1.3323** 的 [native.go](https://github.com/yetone/magpie/blob/v0.1.3323/internal/gateway/native.go)、[providers_cli.go](https://github.com/yetone/magpie/blob/v0.1.3323/internal/cli/providers_cli.go)、[groups_cli.go](https://github.com/yetone/magpie/blob/v0.1.3323/internal/cli/groups_cli.go)。

| 功能 | 本机请求 |
| --- | --- |
| 翻译所有阶段 | POST `http://127.0.0.1:3425/v1/chat/completions`，模型为字幕组 |
| ASR 提交 | POST `/v1/magpie/native/transcribe-bailian/api/v1/services/audio/asr/transcription` |
| ASR 查询 | GET `/v1/magpie/native/transcribe-bailian/api/v1/tasks/{task_id}` |
| 上传策略 | GET `/v1/magpie/native/transcribe-upload/api/v1/uploads` |

原生代理按入口的主机拼接路径，替换调用者认证为上游 Key，保留 `X-DashScope-*`；该版本只执行一次上游请求，ASR 提交仍不自动重试。存储上传和结果下载直连，不带认证。项目读取本机 magpie 注册表核对 ASR 工作空间、单一 Key、无认证覆盖，保持原任务账户指纹。网关故障不会自动改成直连。

`mt_provider:magpie` 生成两个能力相同的入口：`magpie_free` 请求 `mt_free_model`，`magpie` 请求 `mt_model`。均使用 `json_object`，输入／输出预算保持 32768 字节／4096 tokens。网关只读取 `MAGPIE_API_KEY` 或 `data/magpie-key.txt`；本机缺省 `magpie-transcribe`，远程必须有凭据。`User-Agent: Transcribe/1` 用于识别应用。

运行时按费用许可选择一个组：禁止付费选择免费组，允许付费选择总组。每阶段最多向网关发送一次请求，总时限为 180 秒，包含网关排队与成员尝试。429、断连、5xx 或格式校验失败时保存进度，由用户通过 `--resume` 继续。语义纠正作为独立阶段执行。

`--resume --mt-paid allow|deny` 可改变后续网络许可；精确匹配的已完成结果和缓存仍可复用。

免费组与允许付费的组分别保存，`--mt-paid deny` 不发送后者。免费请求前读取本机注册表，递归检查组只含已核实的免费地址和模型，拒绝未知免费来源、模式匹配、分类模型、规则和服务商级回退；`countFree` 只作 magpie 排序标签，不作为费用证明。免费证明只适用于注册表所属本机监听地址（`settings.json` 的 `port`，缺省 3425），并使用任务保存的实际请求地址核对；无注册表或切换到另一网关时免费组不可用。ASR 同样拒绝其他监听地址。通过 `MAGPIE_ADDR` 改端口时，需把实际端口同步到 magpie 保存设置。七牛活动到期时，通过官方 CLI 关闭迁移专属的活动入口；无法关闭时拒绝相关路由。其他自定义活动入口需在网关维护。上游额度查询、RPM 和成员故障切换由 magpie 管理。

迁移保留原设置与凭据备份，显式清除旧池选择。迁移失败时不切换项目设置，已创建的专属入口可通过迁移记录继续；不覆盖无归属记录的同名入口。相同 Key 的免费／付费入口通过 `X-Transcribe-Route` 区分，避免 magpie 的重复入口检测。暂时无法验证免费保护的百炼试用模型导入为关闭状态。

恢复使用保存的组名、地址、网关凭据指纹与能力；组内路由由 magpie 维护。请求携带 `X-Magpie-Response-Model: member`，响应与阶段记录中的 `actual_model` 是 `provider/model`，依据 [member.go](https://github.com/yetone/magpie/blob/v0.1.3323/internal/gateway/member.go)。若上游自身又是路由器，其具体模型详见 magpie 用量记录。历史任务从完整转写通过 `--reflow` 应用当前配置。

总组使用 `routing=free`，免费子组使用 `routing=order`，均 `stays=off`，由 magpie 负责免费优先、入口限流、冷却和回退。该行为核对了安装版本的 [免费优先测试](https://github.com/yetone/magpie/blob/v0.1.3323/internal/gateway/freefirst_group_test.go)。每次翻译携带 `X-Magpie-Session: transcribe-<任务ID>`，本地仅记录该不含文本的关联 ID；成员尝试、上游次数及费用在 magpie Routing／Usage 查看。`GET /v1/magpie/route?session=<gateway_session>` 可读该会话最近一次请求的路由，`--check-mt` 返回合成检查的会话 ID。本地 `mt` 计数代表提交给网关的次数，不代表上游尝试数；本地 token 计数仅统计通过字幕校验的响应。

没有用网关 Key 模型白名单或金额预算代替免费校验：[key_models.go](https://github.com/yetone/magpie/blob/v0.1.3323/internal/gateway/key_models.go) 表明授权组即授权其全部成员；预算依赖价格估算，未知价格不构成免费证明。ASR 原生代理还会拒绝受模型／账户／预算限制的 Key，不能把同一受限 Key 直接套到全部接口。

## 云端请求

| 接口 | 请求 | 官方来源 |
| --- | --- | --- |
| ASR | POST `https://{workspace_id}.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/asr/transcription` | [Qwen-ASR](https://help.aliyun.com/zh/model-studio/qwen-asr-api-reference) |
| 查询 | GET 同工作空间 `/api/v1/tasks/{task_id}` | 同上 |
| 翻译 | POST 所选入口根地址 + `/chat/completions` | [百炼结构化输出](https://help.aliyun.com/zh/model-studio/qwen-structured-output)、[百炼兼容接口](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)、[七牛](https://developer.qiniu.com/aitokenapi/13390/chat-completions)、[OpenRouter](https://openrouter.ai/docs/guides/features/structured-outputs) |
| 上传策略 | GET `https://dashscope.aliyuncs.com/api/v1/uploads` | [临时上传](https://help.aliyun.com/zh/model-studio/get-temporary-file-url) |

ASR 模型为 `qwen3-asr-flash-filetrans`，输入为单数 `input.file_url`；`X-DashScope-Async`、`X-DashScope-OssResourceResolve` 均为 `enable`。参数为 `channel_id:[0]`、`enable_words:true`、`enable_itn:false`，省略语种以自动识别。提交成功即保存 `task_id`，查询成功后下载 `output.result.transcription_url`，解析 `transcripts[].sentences[]`。

上传使用返回策略，流式 multipart 的 `file` 字段最后；上传和结果下载不附带 ASR Authorization。容量取 1,000,000,000 字节和策略限制的较小值。提交 POST 不自动重试；超时、5xx、无有效任务 ID 时保留 `submission_unknown`，须绑定已知 ID 或明确接受重发风险。查询、下载失败沿用原 ID。

## 字幕翻译与质量

请求使用 `stream:false`、`response_format:{"type":"json_object"}`，提示词提供对应阶段的完整输出 Schema。

网络结果、保存阶段及缓存统一校验结构、编号、非空译文和正常结束。术语编号须属于本次请求，词面须出现在对应目标文本中；复核疑点须指向本组片段。`finish_reason:length` 归为 `output_truncated`，可解析的 JSON 同样视为未完成。非法网络结果保存进度，非法保存结果返回 `cache_mismatch`。

本任务阶段记录优先；跨任务缓存按完整实际请求匹配，对齐／复核／纠正优先选择另一组的匹配结果，其次免费优先。上下文或术语裁剪后按新输入查缓存。

每组依次执行术语提取、草稿、必要的片段对齐及自动复核。草稿目标与片段的编号、顺序和原文（忽略目标首尾空白）一一对应时直接使用草稿，否则执行对齐；预算规划采用同一条件。完整组立即保存，每 15 秒报告活动阶段及等待时间。

术语按来源顺序累积：人工术语优先，自动术语保留不同译法及对应目标文本的证据窗口；相同词面和译法采用最近证据，当前句候选优先于前序句候选。ASCII 术语按完整词匹配。调度线程依次合并前序及当前组术语后派发翻译，空闲线程可预取后续术语，待翻译窗口最多为 `2 * mt_workers`；全部阶段共用最多 4 个线程。取消或失败停止新派发。

草稿前按各阶段预算选定共同的上下文与术语，保存为 `semantic_input`，供草稿、对齐、复核、纠正和恢复使用。本任务术语阶段按原始目标请求定位，记录实际裁剪后的上下文。候选容量或费用许可变化后沿用这些已保存输入。

术语服务不可用时保存本任务的 `glossary_unavailable` 标记，继续翻译并报告警告；取消和本机异常照常中止。复核或纠正服务不可用、纠正后仍有疑点时，保留完整译文和警告；草稿或必要对齐失败则保存进度并停止发布。自动复核是辅助检查，疑点仍需人工判断。

最多两轮纠正，每轮之后再次复核。译文映射重复时提前结束；若疑点集合（片段编号、类别、证据，忽略顺序及重复项）、输入及所涉完整源句的全部片段译文均未变化，停止新增纠正请求。完整保存阶段和匹配缓存优先复用，停滞时保留最后完整译文并报告警告。

输入按完整请求 JSON 的 UTF-8 字节计量，上限为 32768 与入口 `input_bytes` 的较小值；输出 `max_tokens` 为 4096 与入口 `output_tokens` 的较小值。证据裁剪规则见[上下文与输入预算](#上下文与输入预算)。目标及必需证据超限，或实际译文使后续请求超限时，保存进度并返回 `mt_input_budget`。无法按可靠边界继续拆分时，需要缩短输入后新建任务。

新分组估算输出量：目标字符按 1 token、每个编号项加 32、固定结构加 64，术语／复核列表每项另加 64，使用有效输出上限的 75%。规划覆盖术语、草稿、必要对齐、复核及整组纠正，按费用许可所选组的能力同时检查输入与输出预算。

输出估算用于调整分组，请求仍使用完整 `max_tokens`。优先减少整句数，长句沿可靠字幕片段边界拆组，保留全部原文。无法拆分的目标按输入预算准入，实际截断保存进度。分组方案随任务保存，恢复继续使用。

### 翻译设置

网关地址与组名见 [项目配置](../README.md#项目配置)。`mt_routing.paid_enabled` 为布尔值，默认 `true`；命令行 `--mt-paid` 可覆盖本任务许可。`mt_quality.manual_glossary` 最多 200 项，原文／译文非空、排除控制字符，分别最多 200／500 字符。配置通过新任务或 `--reflow` 生效。

## 数据与恢复

运行时只接受当前执行契约：媒体处理 2、提取配方 2、字幕格式 2、规范化 4、显示时间 2、翻译策略／请求契约 5、提示词 3、响应恢复标识 4、上下文 2、对齐策略 `execution_snapshot.alignment_version:2`、术语调度 `prefix-v2`。版本字段必须是整数且精确匹配，术语调度字段也必须存在。`--resume` 和 `--retime` 均先核对契约，缺少字段或版本不匹配时返回 `cache_version`，须用 `--reflow` 从完整转写重建。线程数与轮询等待使用当前设置，排版、模型、能力和账户身份沿任务快照。

翻译路线快照为版本 2，身份保存在 `mt_route_snapshot.entries[].backend`，magpie 报告中的模型是费用许可选定的组。项目记录阶段、校验及提交网关的次数；上游尝试、回退及完整费用统计由 magpie 记录。

`data/archives/{任务ID}` 保存 `task.json`、`subtitle.srt`、原始 `raw-*.json` 和逐文件 SHA-256 索引；目录中的任务库备份与结果均属于用户恢复资料。

`--export-archive ID --output-dir DIR` 完整校验归档后导出 `ID.srt`，不读取 Key、不调用云端；已有目标需要 `--overwrite`。归档不执行 `--resume`／`--retime`。`--reflow` 归档任务必须指定输出目录，从已验证的原始转写创建新 ID，使用当前账户、网关和排版配置；保留能与来源完整对应的人工决定，不重新上传或提交 ASR，必要时重新翻译。原归档不改写，人工决定无法对应时停止。

当前任务 `--resume` 沿保存身份与版本继续；`--reflow` 使用完整本地转写与当前翻译配置重建，准备前保存私有恢复快照，同事务替换来源、布局和阶段标记。`--retime` 校验完整最终字幕及实际产出入口身份后更新显示时间，零云端调用。已保存来源或转写齐全时可不依赖源媒体；原位置被不同文件占用时须选择新的输出目录。

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
| `mt_input_tokens` / `mt_output_tokens` / `mt_usage_unknown` | 可读取的用量累计／缺失有效用量的响应数 |
| `mt_quality_warning` / `mt_glossary_unavailable` | 复核未通过或不可用的组数／术语不可用次数 |
| `mt_correction_stalled` | 重复疑点及涉及的整句译文均未变化，且无完整保存纠正可复用时停止新增纠正请求的组数 |
| `stage_*_ms` / `run_ms` | 跨恢复累计阶段与运行耗时 |

诊断位于 `.build/mt-diagnostics/{job_id}.jsonl`，只保存入口、模型、阶段、HTTP 状态、耗时、尝试次数、安全错误分类及 `gateway_session`。正文、凭据和原始异常不进入诊断；网关内部尝试及费用在 magpie Routing／Usage 查看。
