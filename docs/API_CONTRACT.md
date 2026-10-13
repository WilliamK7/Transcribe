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

MT 模型为 `qwen-mt-flash`，`stream:false`，每次一条 user 文本消息。顶层 `translation_options` 包含 `source_lang:auto`（或人工确认的 MT 语言名）、`target_lang:Chinese`，有上下文时增加 `tm_list:[{"source":"参考原文","target":"参考译文"}]`。响应须为 `finish_reason:stop` 且内容合法、非空；译文经 OpenCC t2s 后按稳定 `cue_id` 保存。中文／粤语跳过 MT，语种复核在所有 MT 请求前完成。

MT 按 Key／地域／模型共享请求调度，包含重试的请求开始间隔至少 1.05 秒，默认并发 2。HTTP 429 优先采用 `Retry-After` 秒数，否则退避 15／30／60 秒，所有工作线程共享冷却；最多尝试四次。失败保留已成功结果并停止安排后续请求。账户额度见[官方限流说明](https://help.aliyun.com/zh/model-studio/rate-limit)。

## 数据与恢复

任务库使用进程独占锁。任务绑定账号 Key 的 SHA-256 身份、地域、工作空间、接入方式、完整源文件指纹、模型参数、音轨／声道和媒体方案；缓存命中仍校验身份。已提交输入及分段方案保持固定。Ctrl+C 保存状态；取消任务由用户显式恢复。

新任务 manifest 保存以下版本和设置：

| 字段 | 值或内容 |
| --- | --- |
| `region` / `access_mode` | `beijing` / `workspace` |
| `workspace_id` | 1～63 位合法域名标签 |
| `processing_version` | `2` |
| `contract.normalizer` | `2` |
| `subtitle_version` / `translation_strategy_version` | `1` / `1` |
| `subtitle_settings` | 入队时的 `zh_line_width`、`other_line_width`、`chinese_first` |

原始 ASR JSON 与规范化结果分别保存。完整句保存于 `subtitle-sources-v1.json` 和 SQLite `subtitle_sources`；参考译文存于 `mt_references`，最终片段及译文存于 `cues`。`references_planned`、`layout_planned`、`layout_round` 与对应记录在同一事务提交。恢复校验来源及文字／词范围，复用本地结果、成功参考和片段译文，仅补缺失项；必要阶段全部完成后才提交 SRT。

MT 缓存键包含账号、地域、工作空间、接入方式、媒体版本、模型、文字与 MT 源语言。两阶段流程的版本 3 缓存另含阶段、翻译策略版本、ASR 语种及完整实际 `tm_list` 的摘要。

ASR 提交 POST 由持久化 `submitting` 标记覆盖中断窗口。超时、5xx、无法解析或缺少 `task_id` 均保存为 `submission_unknown`，通过 `--bind-task-id` 绑定已知任务，或由用户以 `--allow-resubmit` 接受重复计费风险后重发。明确拒绝才清除标记；查询和下载失败继续沿用原 ID。

Key 原子明文保存于 `data/key.txt`，`DASHSCOPE_API_KEY` 优先。任务库明文保存上传引用；日志和普通报告隐藏凭据、签名链接、原文、译文及原始异常。终端语种复核可显示原文。

兼容读取规则：

- 缺少接入与媒体版本字段的任务按 `legacy_public`、媒体版本 1 读取，北京任务沿用公共端点和 WAV 方案。新加坡任务保留数据，恢复时在联网和缓存复用前返回 `legacy_region`。
- 缺少 `subtitle_version` 的任务沿用原断句、单阶段翻译、版本 2 MT 缓存和排版。已有有效规范化字幕及成功译文继续使用。
- 规范化版本 1 的任务在模型与参数相同时，可从尚未规范化的本地原始结果升级至 2；已有有效 v1 结果保持原样。
- `key.dpapi` 解密并成功写入新文件后移除。旧 `oss_protected` 仍可解密读取；解密失败时，仅尚未提交的分段可重新上传，已知 ID 或提交未知标记优先用于恢复。

## 时间轴与字幕

媒体版本 2 优先复制兼容压缩音轨，并验证包时间戳、编码延迟／填充、音频属性和时长。多声道仅明确选择首声道时可复制；需要处理声道或时间轴时转换为 16kHz 单声道 FLAC。超限按 PCM 样本精确切分后编码，逐段校验容量；单段最长 12 小时，规格见[ASR 音频说明](https://help.aliyun.com/zh/model-studio/asr-model)。MIME 随实际容器使用 `audio/mp4`、`audio/mpeg`、`audio/flac`、`audio/ogg` 或旧 `audio/wav`。FFmpeg 使用参数数组和 `shell=False`。

规范化保留原文、源句索引和原始时间依据。零时长短句仅在句级与完整词级文字、标点和时间点一致，且语种支持、文字不超过 12 字时修复：补最多 1000ms 显示区间，受下一句和音频结尾限制；可用区间不足 400ms 时，可合并距离不超过 400ms 的同语种有效邻句。修复保存来源与警告，词时间不用于继续细分；其他无效时间返回错误。

完整句的词时间须覆盖清理后的全部文字及标点，为整数、正时长、单调不重叠且位于句内。零时长修复或负时间裁剪后的句子使用句级边界。片段 ID 由源句和词范围稳定生成；无可靠词时间时使用完整文字范围。

原文或参考译文预计超过各两行，或时长超过 7 秒时尝试分段。断点依次选择句末标点／至少 400ms 停顿、逗号／分号、完整词边界，同级取文字量更均衡的位置。新片段使用真实首末词时间且至少 1000ms，原有更短句保留。最终译文超过两行时，仅重拆超限片段，最多追加两轮翻译；无法继续安全拆分时保留内容和时间并记录警告。

换行依次优化行数、英文短尾行和长度均衡，保留完整词及标点。宽字符计 2、普通字符计 1、组合附加符计 0；中文宽度设置乘 2，其他语言按原值。尾部标点允许最多 4 单位超宽，不可拆长词允许超宽。SRT 为 UTF-8 无 BOM、CRLF、合法毫秒时间，同目录原子提交，覆盖前备份；无语音时完成任务且不生成 SRT。

## 上下文与输入预算

先为完整 ASR 句生成固定参考，再按片段翻译。拆分片段优先引用所属整句的原译文对；其他上下文按时间距离选择前后各最多两句。未拆分整句排除自身参考。相邻句须具有一致 ASR 语种、翻译决定、MT 源语言及人工确认状态，扩展路径上的间隔不超过 5000ms；未知语种且未指定 MT 语言时仅使用所属句参考。

上下文始终来自固定参考阶段，按完整条目裁剪，优先所属句和最近邻句。无上下文且输入与参考阶段完全一致时直接复用参考译文。

官方[生产使用说明](https://help.aliyun.com/zh/model-studio/machine-translation)的输入上限为 8192 Token，包含翻译记忆。本地按 `messages` 与实际 `translation_options` 的 JSON UTF-8 字节数加 512 估算，预算为 6000。超长参考仅按可靠词边界拆成单元并持久化，按来源顺序组合用于上下文；最终字幕仍独立按词时间翻译。无法安全拆分时保存 `mt_input_budget` 错误及恢复进度。

普通报告中的 `mt` 为包含重试的 HTTP 尝试总数；`mt_reference_request`、`mt_subtitle_request` 为阶段请求条数。阶段缓存命中和参考复用分别计数，报告附版本、调整轮次及只含片段 ID、行数和原因的排版警告。
