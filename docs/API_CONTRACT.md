# 接口与恢复契约

## magpie 网关契约

翻译通过 Magpie 的 POST `/v1/chat/completions` 发出。分组命令和渠道资格参考官方资料。[Magpie CLI](https://github.com/yetone/magpie/blob/v0.1.3565/internal/cli/groups_cli.go) · [OpenRouter 型号目录](https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties) · [百炼额度](https://help.aliyun.com/zh/model-studio/cli/usage-quota)

路线快照保存六个入口 `magpie`、`magpie_fallback`、`magpie_review`、`magpie_review_fallback`、`magpie_judge`、`magpie_judge_fallback`。每个角色主组按免费 → 商业顺序，后备组使用商业入口。同档供应商等权，供应商内部有序尝试，试用子组占一个供应商位置。分组使用 `stays=off`。三个角色按底层型号去重。连接、限流和上游切换由网关负责。

本机角色池按当前请求 `backend.base_url` 对应的注册表校验。复核／裁决按单次请求排除正文等已用型号，网络响应、阶段恢复和请求缓存共用独立性校验。

商业调用、资源包／代金券消耗和现金支出分别记录，实际抵扣以账单为准。试用准入校验账户、工作空间和模型权限。

请求使用 `MAGPIE_API_KEY` 或 `data/magpie-key.txt`，本机缺省为 `magpie-transcribe`。`X-Magpie-Response-Model: member` 返回实际型号；`X-Magpie-Session: transcribe-<任务ID>-<请求ID>` 对应网关路由记录。

百炼后台全量查询有效额度，每 60 秒刷新，失败退避至最多 300 秒并标记 unknown。自动验收每型号最多 12 个固定合成样例，独立模型再评语义；缓存绑定型号、接口、参数、凭据指纹及测试版本。一次验收一个候选，达到三个合格候选暂停补充。纯缓存恢复不启动刷新。逐次尝试中的额度拒绝优先于最终 200，单模型退出，账户欠费、权限、额度耗尽分别审计；同任务不会由旧额度恢复。原模板保持停用，新型号使用独立 provider，RPM 从共享预算分配。手动暂停和排除优先。详见 [配置](../README.md#配置-magpie)。

`--apply` 先备份并停用共享 `transcribe-bailian-trial` 入口，保留其账户模板，再更新默认角色组的托管成员；执行失败可重新运行。人工停用的成员可保留为不可选占位，无关成员沿用原设置。

型号验收与独立语义验收各请求时限为 120 秒，使用独立 `transcribe-admission-<随机ID>`／`transcribe-admission-judge-<随机ID>` 会话关联路由。临时失败会暂缓整个候选，至少 300 秒后重试；仅独立验收完成的角色取得资格。

### 阶段恢复

| 主组结果 | 项目处理 |
| --- | --- |
| 正文正常结束但格式／编号无效 | 同组修复一次，仍失败转本角色商业后备 |
| 术语／复核／裁决输出无效 | 转本角色商业后备 |
| 截断或阶段请求超时 | 转商业后备，先终止超时子进程 |
| 连接、HTTP、拒绝、内容过滤 | 不另建上游重试，按阶段失败处理 |

每阶段共用 180 秒：主组通常 120 秒，商业后备保留 60 秒，提前切换可用全部剩余时间。正文后备也最多修复一次，总计最多四次请求。辅助阶段失败保留完整译文并标记待检查；正文失败保存进度，停止发布。远程网关的角色隔离由管理员维护，本机检查包括商业后备的实际成员。

## 上游 ASR

| 接口 | 项目直连目标 | 官方来源 |
| --- | --- | --- |
| 提交 | POST `https://{workspace_id}.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/asr/transcription` | [Qwen-ASR](https://help.aliyun.com/zh/model-studio/qwen-asr-api-reference) |
| 查询 | GET 同工作空间 `/api/v1/tasks/{task_id}` | 同上 |
| 上传策略 | GET `https://dashscope.aliyuncs.com/api/v1/uploads` | [临时上传](https://help.aliyun.com/zh/model-studio/get-temporary-file-url) |

项目使用 `DASHSCOPE_API_KEY`／`data/key.txt` 直接签名，按任务保存的工作空间调用，并核验原账户指纹。OSS 上传与签名结果下载不携带百炼认证头。

模型为 `qwen3-asr-flash-filetrans`，输入为单数 `input.file_url`；参数为 `channel_id:[0]`、`enable_words:true`、`enable_itn:false`，语种由 ASR 判定。提交携带 `X-DashScope-Async: enable` 与 `X-DashScope-OssResourceResolve: enable`。POST 单次发送，服务端故障、超时或不确定响应进入 `submission_unknown`，恢复时绑定已知任务 ID 或显式允许重发。

## 字幕翻译与质量

策略 6 使用 `glossary → translate → review → [judge + review]`。所有阶段为非流式 JSON，轻量阶段请求 `reasoning_effort:none`，裁决为 `low`。只有响应实际报告零推理 token 才认定已关闭思考；未知用量不当作零。

术语默认按 8 组窗口增量提取，输入或输出预算不足继续拆分。最多预取两个窗口，按原文顺序提交术语前缀，后组新术语不能影响前组；人工覆盖优先，同词异义保留语境和出处。本任务失败标记及裁剪后上下文复用，取消恢复不重建已经生效的语义输入。

正文一次接收原句、分片边界及编号，直接输出全部目标编号译文，本地检查重复、缺失、越界、空译文和非正常结束。每组由独立型号复核漏译、错译、数字、否定、术语及跨片语义。

裁决同时返回核实后的 issues 和完整 translations。全部误报时必须原样返回译文；有依据的问题在同一次调用修正，最多两轮。文本不变、相同问题未改善或循环即停止。辅助网络失败保留完整译文并告警，损坏的本机保存记录仍报 `cache_mismatch`。

各阶段共同语义输入在正文前保存为 `semantic_input`。输入上限为 32768 字节，输出上限为 4096 token。每 15 秒报告活动阶段。[翻译设置](../README.md#项目配置)

## 数据与恢复

任务状态、操作事件和恢复提示由 `subtitle_tool/lifecycle.py` 定义。Store 同事务校验状态转移与写入，非法转移返回 `invalid_transition`；停止、完成和未知提交通过各自的恢复入口继续。运行阶段是可跳步、可循环的检查点，恢复还需校验保存证据。`--jobs` 只读最后记录，保留状态、错误和请求 ID；状态不表示此刻有进程在执行。

`--state-machine` 直接输出版本化 JSON：`states` 包含名称、中文 `label`、`kind` 和运行阶段的 `auto_recover` 标记，`events` 包含每个操作的 `from`、`to` 和 `guards`。规则从执行代码生成，无需配置、任务库、凭据或网络。

`--resume` 和 `--retime` 先核对执行版本、排版和术语调度快照。版本不匹配时返回 `cache_version`，可通过 `--reflow` 从完整转写重建。线程数与轮询等待使用当前设置，排版、模型、能力和账户身份沿任务快照。[执行版本校验](../subtitle_tool/policy.py)

翻译路线快照写入版本 6，读取版本 5 和 6。`routing.strategy` 为 `free_first`，入口身份和策略修订号 `policy_version` 保存在 `entries[].backend`。版本 6 的 `identities` 保存网关成员对应的底层型号、动态标记、已验收集合及费用档位，并参与阶段缓存隔离。报告记录正文主组及 `mt_policy_version`。

阶段记录的 `actual_model` 仍是网关成员，`resolved_model` 表示经校验的底层型号。vLLM-SR `auto` 只允许翻译，必须由白名单响应头 `x-vsr-selected-model` 给出已验收集合内的型号；缺失、越界或跨角色冲突时不采纳响应，按现有有限等待策略进入商业后备。固定型号的旧记录没有 `resolved_model` 时可按已核实别名恢复；旧阶段与恢复后新生成的最终记录按解析后的底层身份比较，不改写旧记录。旧动态记录保留，但不提供新的独立性证据，无法验证时须显式 `--reflow`。需要重建的路线在列表中标记 `mt_route_status:reflow_required`。

型号验收套件为版本 3；缓存键包含接口能力、插件版本、账户指纹、评判者身份及动态后端集合，凭据不写入验收报告。配置发现合并普通提供者、插件注册表、网关有效目录和官方型号能力；`configure-mt.py --list-candidates` 不写入状态、不发送推理请求。

渠道维护配置 `data/mt-channels.json` 为版本 1，供应商清单是显式授权边界。没有该文件时默认不启动自动维护，`manage-mt.py --enable` 写入模板后启用；新供应商必须显式登记。后台在首次默认本机路线联网请求时启动，与字幕任务一同结束。新任务、恢复任务、独立维护命令共享项目进程锁；后台分组写入与百炼角色分配共用同一个锁，推理不占该锁。远程网关、自定义角色入口和纯缓存恢复不启动自动管理。

可选 `excluded_providers` 是项目级供应商排除列表，缺省为空；它与管理清单、验收评判者冲突时配置无效。自动维护及验收不使用排除项，生产请求在发送前展开本机组检查，不能通过旧验收缓存或嵌套组绕过。历史任务与已保存译文不重建，Magpie 全局配置不受此规则影响。

`gateway-maintenance.json` 版本 1 保存型号身份指纹、已通过结果、失败／冷却状态、自动开放入口的归属及 UTC 日验收预算；不保存凭据、字幕或响应正文。默认每轮 1 个、每天 2 个待验收型号，提前预留名额，异常退出不退回。当日达到上限仍可读目录和复用已有资格；临时失败最少按配置冷却 3600 秒。失败缓存绑定原账户及能力；插件指纹按账户和接口能力计算，目录中其他型号的变化不影响已验收资格。

目录来源为插件注册表、网关有效目录、与当前端点绑定的 Magpie 已抓取目录，以及适配器支持的官方目录。后台使用 Magpie `provider refresh` 刷新已登记、未关闭目录请求的渠道；保留 `listOff`。官方目录中的明确候选可按规则增量开放，人工删除自动开放的入口产生持久 `manual_hold`。目录不可读不等于下架；临时故障不取消同身份资格，凭据／接口／动态集合变化不沿用旧资格。新免费入口、商业后备仍由同一角色独立性验证、子组预演、备份和分阶段写入约束；失败更新保留诊断和恢复记录。维护状态不改变路线快照格式或历史执行契约。

`gateway-apply-pending.json` 版本 2 在每次分组命令前记录该组的前后状态，执行核验后记录完成。中断重试先恢复已确认写入的托管归属；回执丢失时用本机状态核对是否生效，再按当前验收结果重新规划，不重放旧的待执行决策。人工删除和停用继续相对已生效的成员表识别。旧版迁移记录可恢复已确认步骤；若未确认步骤的现状与保存证据冲突，返回 `magpie_group` 并保留记录与备份，不猜测归属。下一次更新的备份同时保存上一份迁移记录。

目录接口依据：[Magpie CLI](https://github.com/yetone/magpie/blob/v0.1.3565/internal/cli/providers_cli.go)、[Magpie 已抓取目录](https://github.com/yetone/magpie/blob/v0.1.3565/internal/catalog/live.go)、[Vercel 型号与每 token 价格](https://vercel.com/docs/ai-gateway/models-and-providers)、[OpenRouter 型号目录](https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties)。

`data/archives/{任务ID}` 保存 `task.json`、`subtitle.srt`、原始 `raw-*.json` 和逐文件 SHA-256 索引；目录中的任务库备份与结果均属于用户恢复资料。

`--export-archive ID --output-dir DIR` 完整校验归档后导出 `ID.srt`，不读取 Key、不调用云端；已有目标需要 `--overwrite`。归档不执行 `--resume`／`--retime`。`--reflow` 归档任务必须指定输出目录，从已验证的原始转写创建新 ID，使用当前账户、网关和排版配置；保留能与来源完整对应的人工决定，不重新上传或提交 ASR，必要时重新翻译。原归档不改写，人工决定无法对应时停止。

`--reflow` 核对账户、识别输入和完整本地转写，按当前设置重新规范化及排版。旧规范化结果、显示时间和翻译策略版本由重建替换。项目保存恢复快照，并在同一事务中替换来源、布局和阶段标记。`--retime` 复用完整最终字幕及已验证的译文身份，更新显示时间，零云端调用。保存的来源或转写齐全时可从本地恢复。原位置被不同媒体占用时须另选输出目录。

任务库使用进程独占锁与 SQLite 事务。账户、工作空间、源文件指纹、音轨、声道及媒体方案均受校验。未结束任务只在输入、输出目标和翻译身份一致时复用。

Ctrl+C 保留已保存结果，停止新翻译请求和配置命令，结束后台额度查询与验收；在途翻译及 CLI 配置命令共享五秒收尾窗口。再次 Ctrl+C 终止并回收子进程。配置更新中断后，恢复时先重新核验保存状态。ASR 请求等待返回或超时，云端已收到的请求仍可能继续执行和计费。

翻译阶段保存在私有 `mt_stages`。缓存键绑定完整请求、后端身份和能力、提示词、账户及策略版本。最终记录引用草稿、复核、裁决和实际产出阶段，片段通过 `mt_stage_key` 和 `mt_origin` 关联完整组。恢复和校时校验原文摘要、范围、模型及译文映射。译文、阶段证据、缓存和完成计数原子提交。请求和响应统计单独累加。人工复核同事务更新来源和片段，清除受影响的翻译与阶段状态。

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

句间识别时间重叠保留原始依据并记录警告。显示时间自动处理同分段、不同来源句且来源递增、满足 `前起点 < 后起点 < 前终点 < 后终点` 的冲突，单次重叠最多 500ms。版本 3 优先收短前条的显示终点，前条无法满足限幅时尝试推迟后条的显示起点。被调整片段的起点和终点累计缩短最多 500ms 且不超过该片段原时长的 10%，调整后至少显示 1000ms。原本短于 1000ms 的片段可保留原时长。其他冲突返回 `timeline`。

`--resume` 沿用保存的显示时间版本。版本 2 只收短前条终点，前条调整后及后条原时长均须至少 1000ms。新任务、`--reflow` 和 `--retime` 使用版本 3。版本 2 任务若在翻译前因显示冲突失败，可通过 `--reflow` 从完整本地转写重建。已有完整最终译文的任务可用 `--retime` 更新显示时间。

在生成布局后、每轮字幕翻译前及布局重分片提交前执行纯显示预检，失败时不发送该轮字幕 MT 请求，不提前保存调整摘要或发布意图，保留已有成功结果。发布前再次规划并全量校验显示区间为非负整数、正时长、单调且不重叠，相接端点允许相等。

`cues.begin_ms/end_ms` 保留来源及词范围依据。每条 `display_timing_adjustments` 记录一次端点变更，包含 `cue_id`、变更前后的 `old_begin_ms/old_end_ms` 与 `new_begin_ms/new_end_ms`、正毫秒数 `adjustment_ms` 以及 `reason`。收短终点使用 `bounded_sentence_overlap`，推迟起点使用 `bounded_sentence_overlap_start_delay`。

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
| `mt_v6_{入口ID}_{阶段}_request` | 实际入口的阶段请求登记数 |
| `mt_v6_{阶段}_completed` / `_cache_hit` / `_failure` | 阶段完成、跨任务缓存命中、候选失败数 |
| `mt_input_tokens` / `mt_output_tokens` / `mt_usage_unknown` | 通过字幕校验的响应：有效用量累计／缺失有效用量的响应数 |
| `mt_response_input_tokens` / `mt_response_output_tokens` / `mt_response_usage_unknown` | 所有已收到响应（含校验失败）的有效用量累计／缺失有效用量的响应数 |
| `mt_response_reasoning_tokens` | 已收到响应中有效的推理 token 累计，属于输出 token 的子集；上游未提供时不假设为 0 |
| `mt_format_repair_request` / `mt_format_repair_success` | 追加格式修复的请求登记数／修复响应通过字幕校验数 |
| `mt_{trial,free,commercial,unknown}_response` | 实际选中入口所属方案的响应数；权益消耗与现金支出保持 unknown，不虚构免费或扣款账单 |
| `mt_paid_fallback_request` / `mt_paid_fallback_success` | 独立付费后备的请求登记数（含其格式修复）／响应通过字幕校验数 |
| `mt_quality_warning` / `mt_glossary_unavailable` | 复核未通过或不可用的组数／术语不可用次数 |
| `mt_correction_stalled` | 重复疑点及涉及的整句译文均未变化，且无完整保存纠正可复用时停止新增纠正请求的组数 |
| `stage_*_ms` / `run_ms` | 跨恢复累计阶段与运行耗时 |

诊断位于 `.build/mt-diagnostics/{job_id}.jsonl`。逐请求的 `request_start`／`request_end` 记录 `request_id`、`gateway_session`、`group_key`、`run_id`、阶段、入口、请求模型与实际成员、尝试次数、策略版本、HTTP 状态、耗时、可读取的 token 用量及安全分类。请求事件的 `attempt` 是同组内的第 1／2 次；`route_complete.attempt` 是阶段总尝试数 0～4，缓存命中为 0。`outcome` 为 `valid`、`invalid`、`service_error`、`transport_error`、`cancelled` 或 `local_error`；格式失败另有 `validation_category`。无效响应的用量也计入全响应统计，缓存命中不虚构新请求。`request_start` 表示登记提交，不能证明上游已收到；进程异常退出可能来不及留下结束事件。正文、凭据和原始异常不进入诊断；本地 token 统计不等同于费用，网关内部尝试及完整费用在 magpie Routing／Usage 查看。
