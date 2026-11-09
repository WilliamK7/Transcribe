# 接口与恢复契约

## magpie 网关契约

翻译使用字幕组，ASR 使用原生代理。接口依据 magpie **0.1.3390** 的 [CLI 参考](https://github.com/yetone/magpie/blob/v0.1.3390/docs/reference.md)、[原生代理实现](https://github.com/yetone/magpie/blob/v0.1.3390/internal/gateway/native.go)及[官方接入说明](https://usemagpie.ai/docs/integrate)。

| 功能 | 网关请求 |
| --- | --- |
| 翻译 | POST `/v1/chat/completions`，模型为费用许可选定的字幕组 |
| ASR 提交 | POST `/v1/magpie/native/transcribe-bailian/api/v1/services/audio/asr/transcription` |
| ASR 查询 | GET `/v1/magpie/native/transcribe-bailian/api/v1/tasks/{task_id}` |
| 上传策略 | GET `/v1/magpie/native/transcribe-upload/api/v1/uploads` |

原生代理按入口主机拼接路径，用上游 Key 签名并保留 `X-DashScope-*`；ASR POST 单次转发。项目读取本机注册表核对工作空间、单一账户凭据及认证头，保留账户指纹。存储上传和签名结果下载直接访问存储。

路线保存免费组 `magpie_free`、总组 `magpie` 与独立付费后备 `magpie_fallback` 三个完整入口，后两者费用层级均为 `paid`。请求以 `MAGPIE_API_KEY` 或 `data/magpie-key.txt` 认证，本机缺省为 `magpie-transcribe`，应用标识为 `User-Agent: Transcribe/1`。

`--mt-paid deny` 选择免费组，`allow` 选择总组；恢复时可修改后续请求的费用许可，已完成的匹配结果继续复用。免费证明绑定本机注册表所属监听地址（保存的 `port`，缺省 3425）。请求前递归核验组成员和回退路径，按已核实的地址、模型及活动有效期准入。七牛活动到期时尝试通过 CLI 关闭专用入口；免费标签和估算价格仅供网关调度。配置见 [README](../README.md#配置-magpie)。

`X-Magpie-Response-Model: member` 返回实际 `provider/model`。每次请求以独立 UUID 组成 `X-Magpie-Session: transcribe-<任务ID>-<请求ID>`，对应 `GET /v1/magpie/route?session=<gateway_session>` 的最近一次请求。具体成员尝试、用量及费用在 magpie Routing／Usage 查看；项目记录提交网关的次数与校验结果。

### 阶段恢复

| 主组结果 | 项目处理 |
| --- | --- |
| 草稿／对齐正常结束，字符串内容未通过格式或编号校验 | 同组修复一次；再次失败且允许付费时切换后备 |
| 术语／复核／纠正正常结束，字符串内容未通过校验 | 允许付费时直接切换后备 |
| 响应截断或达到本地请求时限 | 允许付费时直接切换后备；超时先结束请求子进程 |
| 连接失败、HTTP 故障、不确定响应、拒绝或内容过滤 | 结束本阶段请求，交由流水线按阶段处理 |

后备是 `mt_fallback_model` 指定的独立付费组，每阶段最多切换一次。草稿／对齐在后备组也可修复一次，因此每阶段最多四次请求。格式修复追加固定指令，修复和切换均沿用冻结的原文、上下文及术语，并在每次发送前核验组配置与费用许可。本地后备组仅允许静态付费成员，递归排除已知免费模型、动态分类规则及服务商级回退；远程网关由其管理员维护成员边界。

缓存查找、准备和全部请求共用一个 180 秒截止时间。允许付费时，主组使用剩余时间的前三分之二，最多预留 60 秒给后备；无准备耗时时分配为 120 秒／60 秒。提前切换时，后备可用全部剩余时间。`--mt-paid deny` 下免费主组使用全部剩余时间，已完成的匹配结果仍可复用。原上游请求是否计费以网关记录为准。

草稿／必要对齐耗尽可用请求后保存进度、停止发布；辅助阶段按[字幕翻译与质量](#字幕翻译与质量)保留完整结果并降级。截断错误码为 `mt_output_truncated`；失败记录保留最后一次本地请求 UUID 和安全原因分类，可据此查询网关。

## 上游 ASR

| 接口 | 网关转发目标 | 官方来源 |
| --- | --- | --- |
| 提交 | POST `https://{workspace_id}.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/asr/transcription` | [Qwen-ASR](https://help.aliyun.com/zh/model-studio/qwen-asr-api-reference) |
| 查询 | GET 同工作空间 `/api/v1/tasks/{task_id}` | 同上 |
| 上传策略 | GET `https://dashscope.aliyuncs.com/api/v1/uploads` | [临时上传](https://help.aliyun.com/zh/model-studio/get-temporary-file-url) |

模型为 `qwen3-asr-flash-filetrans`，输入为单数 `input.file_url`；参数为 `channel_id:[0]`、`enable_words:true`、`enable_itn:false`，语种由 ASR 判定。提交携带 `X-DashScope-Async: enable` 与 `X-DashScope-OssResourceResolve: enable`。网关故障、超时或不确定响应进入 `submission_unknown`，恢复时绑定已知任务 ID 或显式允许重发。

## 字幕翻译与质量

全部阶段及格式修复使用 `stream:false`、`response_format:{"type":"json_object"}`、`reasoning_effort:"none"`，提示词提供对应阶段的完整输出 Schema。magpie 按成员能力和网关规则适配推理级别；支持关闭时为字幕正文保留输出预算，强制推理模型使用其支持的级别。实际 effort 与推理用量见 Routing／Usage。

依据：[magpie 0.1.3390 的能力适配](https://github.com/yetone/magpie/blob/v0.1.3390/internal/gateway/decide.go)、[请求及拒绝处理](https://github.com/yetone/magpie/blob/v0.1.3390/internal/gateway/gateway.go)、[OpenRouter 推理与输出预算](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)。推理 token 会占用输出上限；仅隐藏推理正文并不能节省这部分预算。

网络结果校验正常结束、结构、编号及非空译文；保存阶段及缓存复用同一值校验。术语编号须属于本次请求，词面须出现在对应目标文本中；复核疑点须指向本组片段。`finish_reason:length` 归为 `output_truncated`，可解析的 JSON 同样视为未完成。仅完整通过校验的响应按原目标请求、实际产出入口和策略版本保存，非法保存结果返回 `cache_mismatch`。

本任务阶段记录优先；跨任务缓存按完整目标请求与策略版本匹配，对齐／复核／纠正优先选择另一组的匹配结果，其次免费优先。上下文或术语裁剪后按新输入查缓存。

每组依次执行术语提取、草稿、必要的片段对齐及自动复核。草稿目标与片段的编号、顺序和原文（忽略目标首尾空白）一一对应时直接使用草稿，否则执行对齐；预算规划采用同一条件。完整组立即保存，每 15 秒报告活动阶段及等待时间。

术语按来源顺序累积：人工术语优先，自动术语保留不同译法及对应目标文本的证据窗口；相同词面和译法采用最近证据，当前句候选优先于前序句候选。ASCII 术语按完整词匹配。调度线程依次合并前序及当前组术语后派发翻译，空闲线程可预取后续术语，待翻译窗口最多为 `2 * mt_workers`；全部阶段共用最多 4 个线程。取消或失败停止新派发。

草稿前按各阶段预算选定共同的上下文与术语，保存为 `semantic_input`，供草稿、对齐、复核、纠正和恢复使用。本任务术语阶段按原始目标请求定位，记录实际裁剪后的上下文。候选容量或费用许可变化后沿用这些已保存输入。

术语服务不可用时保存本任务的 `glossary_unavailable` 标记，继续翻译并报告警告；取消和本机异常照常中止。复核或纠正服务不可用、纠正后仍有疑点时，保留完整译文和警告；草稿或必要对齐失败则保存进度并停止发布。自动复核是辅助检查，疑点仍需人工判断。

最多两轮纠正，每轮之后再次复核。译文映射重复时提前结束；若疑点集合（片段编号、类别、证据，忽略顺序及重复项）、输入及所涉完整源句的全部片段译文均未变化，停止新增纠正请求。完整保存阶段和匹配缓存优先复用，停滞时保留最后完整译文并报告警告。

输入按完整请求 JSON 的 UTF-8 字节计量，上限为 32768 与入口 `input_bytes` 的较小值；输出 `max_tokens` 为 4096 与入口 `output_tokens` 的较小值。草稿／对齐在首次发送前预留格式修复指令空间，同时适配已获许可的主组和后备组；修复及切换时不再裁剪已经冻结的证据。证据裁剪规则见[上下文与输入预算](#上下文与输入预算)。目标及必需证据超限，或实际译文使后续请求超限时，保存进度并返回 `mt_input_budget`。无法按可靠边界继续拆分时，需要缩短输入后新建任务。

新分组估算输出量：目标字符按 1 token、每个编号项加 32、固定结构加 64，术语／复核列表每项另加 64，使用有效输出上限的 75%。规划覆盖术语、草稿、必要对齐、复核及整组纠正，按费用许可所选主组及后备组的能力同时检查输入与输出预算。

输出估算用于调整分组，请求仍使用完整 `max_tokens`。优先减少整句数，长句沿可靠字幕片段边界拆组，保留全部原文。无法拆分的目标按输入预算准入，实际截断保存进度。分组方案随任务保存，恢复继续使用。

### 翻译设置

网关地址与组名见 [项目配置](../README.md#项目配置)。`mt_routing.paid_enabled` 为布尔值，默认 `true`；命令行 `--mt-paid` 可覆盖本任务许可。`mt_quality.manual_glossary` 最多 200 项，原文／译文非空、排除控制字符，分别最多 200／500 字符。配置通过新任务或 `--reflow` 生效。

`mt_policy_version` 默认 `1`，取整数 1～2147483647。新任务和 `--reflow` 将其冻结到所有后端的 `policy_version`，同一任务各后端必须一致；该值进入阶段身份与跨任务缓存键。普通恢复沿用保存版本。若组名不变但有意修改成员／策略并要求重译，递增此设置后执行 `--reflow`；普通组内故障切换沿用该版本。

## 数据与恢复

任务状态、操作事件和恢复提示由 `subtitle_tool/lifecycle.py` 定义。Store 同事务校验状态转移与写入，非法转移返回 `invalid_transition`；停止、完成和未知提交通过各自的恢复入口继续。运行阶段是可跳步、可循环的检查点，恢复还需校验保存证据。`--jobs` 只读最后记录，保留状态、错误和请求 ID；状态不表示此刻有进程在执行。

`--state-machine` 直接输出版本化 JSON：`states` 包含名称、中文 `label`、`kind` 和运行阶段的 `auto_recover` 标记，`events` 包含每个操作的 `from`、`to` 和 `guards`。规则从执行代码生成，无需配置、任务库、凭据或网络。

当前执行契约：媒体处理 2、提取配方 2、字幕格式 2、规范化 4、显示时间 2、翻译策略／请求契约 5、提示词及生成参数 4、响应恢复标识 5、上下文 2、对齐策略 `execution_snapshot.alignment_version:2`、术语调度 `prefix-v2`。版本字段为整数且精确匹配，术语调度字段也须存在。`--resume` 和 `--retime` 均先核对契约，缺少字段或版本不匹配时返回 `cache_version`，通过 `--reflow` 从完整转写重建。线程数与轮询等待使用当前设置，排版、模型、能力和账户身份沿任务快照。

翻译路线快照为版本 4；身份保存在版本 2 的 `mt_route_snapshot.entries[].backend`，包含用户管理的策略修订号 `policy_version`。报告中的模型是费用许可选定的主组，并提供 `mt_policy_version`；每个阶段记录实际使用的入口。需要重建的路线在列表中标记 `mt_route_status:reflow_required`。

`data/archives/{任务ID}` 保存 `task.json`、`subtitle.srt`、原始 `raw-*.json` 和逐文件 SHA-256 索引；目录中的任务库备份与结果均属于用户恢复资料。

`--export-archive ID --output-dir DIR` 完整校验归档后导出 `ID.srt`，不读取 Key、不调用云端；已有目标需要 `--overwrite`。归档不执行 `--resume`／`--retime`。`--reflow` 归档任务必须指定输出目录，从已验证的原始转写创建新 ID，使用当前账户、网关和排版配置；保留能与来源完整对应的人工决定，不重新上传或提交 ASR，必要时重新翻译。原归档不改写，人工决定无法对应时停止。

当前任务 `--resume` 沿保存身份与版本继续；`--reflow` 使用完整本地转写与当前翻译配置重建，准备前保存私有恢复快照，同事务替换来源、布局和阶段标记。`--retime` 校验完整最终字幕及实际产出入口身份后更新显示时间，零云端调用。已保存来源或转写齐全时可不依赖源媒体；原位置被不同文件占用时须选择新的输出目录。

任务库使用进程独占锁与 SQLite 事务。账户、工作空间、源文件指纹、音轨、声道及媒体方案均受校验。未结束任务只在输入、输出目标和翻译身份一致时复用。取消不删除结果；首次 Ctrl+C 停止新翻译请求，所有在途翻译共享五秒收尾窗口，再次 Ctrl+C 立即结束等待。ASR 请求等待返回或超时；本机取消不保证云端停止生成或计费。

翻译阶段保存在私有 `mt_stages`，缓存键包含完整目标请求、后端身份及策略版本、模型能力、提示词及账户范围。最终记录引用草稿、复核、纠正和实际产出阶段；实际产出可以是草稿、对齐或纠正，片段通过 `mt_stage_key` 和 `mt_origin` 关联完整组。私有草稿按目标顺序保存 `source_text_hashes`，包括从匹配响应缓存恢复的草稿。直接使用草稿时，最终产出引用等于草稿引用，译文映射一致，片段原文摘要匹配对应草稿编号。部分片段重新分片后，其余完整结果仍可校验。译文、阶段证据、缓存和阶段完成计数原子提交；请求及响应统计单独累加，包含未能保存为阶段结果的响应。恢复和校时校验来源、范围、模型及完整映射；人工复核同事务更新来源与片段，清除受影响的翻译与阶段状态。

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

任务结果中的 `lifecycle` 包含 `version`、中文 `label`、`kind` 和 `next_actions`。每项操作的 `id` 用于程序分支，`label` 用于展示，`argv` 是固定参数数组；`required_options` 将待填选项映射到输入说明，调用方填写真实值。`requires_confirmation:true` 表示重发未知提交前须明确接受可能重复计费。操作建议来自保存记录，执行时校验文件、凭据和产出；覆盖已有字幕须显式添加 `--overwrite`。

| `counts` 字段 | 含义 |
| --- | --- |
| `mt` / `mt_429` / `mt_success` | 已登记尝试／限流响应／HTTP 2xx，均不等同于最终译文成功 |
| `mt_invalid_response` / `mt_output_truncated` | 未通过本地响应校验的次数／其中以 `length` 结束的次数 |
| `mt_subtitle_completed` | 已同事务保存的最终片段数 |
| `mt_v5_{入口ID}_{阶段}_request` | 实际入口的阶段请求登记数 |
| `mt_v5_{阶段}_completed` / `_cache_hit` / `_failure` | 阶段完成、跨任务缓存命中、候选失败数 |
| `mt_input_tokens` / `mt_output_tokens` / `mt_usage_unknown` | 通过字幕校验的响应：有效用量累计／缺失有效用量的响应数 |
| `mt_response_input_tokens` / `mt_response_output_tokens` / `mt_response_usage_unknown` | 所有已收到响应（含校验失败）的有效用量累计／缺失有效用量的响应数 |
| `mt_response_reasoning_tokens` | 已收到响应中有效的推理 token 累计，属于输出 token 的子集；上游未提供时不假设为 0 |
| `mt_format_repair_request` / `mt_format_repair_success` | 追加格式修复的请求登记数／修复响应通过字幕校验数 |
| `mt_paid_fallback_request` / `mt_paid_fallback_success` | 独立付费后备的请求登记数（含其格式修复）／响应通过字幕校验数 |
| `mt_quality_warning` / `mt_glossary_unavailable` | 复核未通过或不可用的组数／术语不可用次数 |
| `mt_correction_stalled` | 重复疑点及涉及的整句译文均未变化，且无完整保存纠正可复用时停止新增纠正请求的组数 |
| `stage_*_ms` / `run_ms` | 跨恢复累计阶段与运行耗时 |

诊断位于 `.build/mt-diagnostics/{job_id}.jsonl`。逐请求的 `request_start`／`request_end` 记录 `request_id`、`gateway_session`、`group_key`、`run_id`、阶段、入口、请求模型与实际成员、尝试次数、策略版本、HTTP 状态、耗时、可读取的 token 用量及安全分类。请求事件的 `attempt` 是同组内的第 1／2 次；`route_complete.attempt` 是阶段总尝试数 0～4，缓存命中为 0。`outcome` 为 `valid`、`invalid`、`service_error`、`transport_error`、`cancelled` 或 `local_error`；格式失败另有 `validation_category`。无效响应的用量也计入全响应统计，缓存命中不虚构新请求。`request_start` 表示登记提交，不能证明上游已收到；进程异常退出可能来不及留下结束事件。正文、凭据和原始异常不进入诊断；本地 token 统计不等同于费用，网关内部尝试及完整费用在 magpie Routing／Usage 查看。
