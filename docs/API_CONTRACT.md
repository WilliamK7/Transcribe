# 接口与恢复契约

以下记录程序使用的请求与恢复格式；真实工作空间闭环的验证状态见 [README](../README.md)。

| 接口 | 当前应用契约 | 官方来源 |
| --- | --- | --- |
| 新任务 ASR | POST https://{workspace_id}.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/asr/transcription | [Qwen-ASR](https://help.aliyun.com/zh/model-studio/qwen-asr-api-reference) |
| 新任务查询 | GET 同一工作空间 /api/v1/tasks/{task_id} | 同上 |
| 新任务 MT | POST 同一工作空间 /compatible-mode/v1/chat/completions | [Qwen-MT](https://help.aliyun.com/zh/model-studio/qwen-mt-api) |
| 上传策略 | GET https://dashscope.aliyuncs.com/api/v1/uploads，action=getPolicy，model=qwen3-asr-flash-filetrans | [临时上传](https://help.aliyun.com/zh/model-studio/get-temporary-file-url) |
| 文件上传 | 返回的 upload_host，流式整文件 multipart，file 最后；不携带百炼 Authorization | 同上 |
| 音频规格 | 所选模型支持 m4a、mp3、flac、ogg 等；单段不超过 12 小时 | [所选 ASR 音频规格](https://help.aliyun.com/zh/model-studio/asr-model) |

上传使用返回的 upload_host、默认代理和单文件整表单请求。容量取项目 1,000,000,000 字节上限和返回 max_file_size_mb 的较小值，每次新策略重新检查实际大小。

ASR 固定 qwen3-asr-flash-filetrans，单数 input.file_url；X-DashScope-Async 和 X-DashScope-OssResourceResolve 均为 enable。parameters 为 channel_id:[0]、enable_words:true、enable_itn:false，language 省略以自动识别。成功立刻持久化 output.task_id；查询成功地址取 output.result.transcription_url，原始结果按 transcripts[].sentences[] 解析。结果下载省略百炼 Key；链接从普通日志隐藏。

MT 固定 qwen-mt-flash，一条 user 文字消息，stream:false，顶层 translation_options 为 source_lang:auto（或用户确认的 MT 语言名称）、target_lang:Chinese。不传字幕 ID、时间或编号；译文按本地稳定 cue_id 对应。finish_reason:stop 且非空、合法内容才使用，OpenCC t2s 仅作用于译文。中文／粤语分流在发出请求前完成。

当前本地双语 SRT 默认原文在上、简体中文译文在下（chinese_first:false）；中文／粤语只保留原文。显示顺序属于本地排版，不影响 MT 请求或翻译缓存。

程序在同一进程内按 Key／地域／模型共享 MT 调度，每次请求（含重试）开始至少间隔 1.05 秒；并发上限由 mt_workers 控制。MT 遇到 HTTP 429 时优先使用 Retry-After 秒数，否则等待 15／30／60 秒，各工作线程共享冷却时间；最多请求四次，仍失败则保存成功译文并停止安排后续请求。账户实际额度见[官方限流说明](https://help.aliyun.com/zh/model-studio/rate-limit)。

接入身份与旧任务：

- 新任务必须配置合法域名标签 Workspace ID，manifest 固化 region:beijing、workspace_id、access_mode:workspace、processing_version:2 和账号 Key 的 SHA-256 身份。
- 缺少新字段的旧任务按 access_mode:legacy_public、processing_version:1 读取。北京仍用原 dashscope.aliyuncs.com 的 ASR／查询／MT，不自动归属当前配置中的工作空间；原 WAV 媒体方案继续恢复。
- 新加坡任务保留数据，但本版在联网和缓存恢复前阻止该地域；不跨地域发送 Key。
- cache_identity 校验账号、地域、工作空间、接入方式和处理版本；分段上传引用也绑定该身份。完整源文件指纹、模型／规范化契约、音轨／声道和媒体方案必须相容；规范化或翻译缓存命中也检查身份。
- MT 缓存键升级到版本 2，增加账号、工作空间、接入方式和处理版本，旧全局 MT 缓存不命中。旧任务已有成功译文与规范化字幕仍保留。

密钥原子明文保存于 data/key.txt，DASHSCOPE_API_KEY 优先；key.dpapi 仅用于兼容解密，成功写入新文件后移除。oss_url 明文存于个人任务库，从日志／报告隐藏；旧 oss_protected 兼容读取，无法解密且没有 task_id 或提交未知标记时才重新上传。已知 ID 可以独立恢复；未知提交需用户显式处理。

ASR 提交 POST 不自动重试：超时、5xx、无法解析、异常 output 或无 task_id 是提交未知。持久 submitting 标记覆盖 Ctrl+C／崩溃窗口；只支持 --bind-task-id 或明确接受重复计费风险的 --allow-resubmit。明确拒绝才清除提交标记。已知任务的查询／下载失败继续原 ID，轮询／下载／MT 使用有限退避。

媒体处理版本 2：连续时间轴、兼容格式且无需声道转换时复制压缩音轨；多声道仅用户明确选择首声道可复制，ASR [0] 不变。复制前后验证包时间戳、编码延迟／填充、音频属性和时长，保留播放原点、静音和偏移；不可靠则按原解码校正路径转换为 16kHz 单声道 FLAC。超限经 PCM 精确样本切分后编码 FLAC，逐段校验容量；上传 MIME 按实际容器使用 audio/mp4、audio/mpeg、audio/flac、audio/ogg 或旧 audio/wav。恢复必须沿用原方案，已提交后禁止重选输入或重新分段。

原始 ASR JSON 与版本化规范化文件分别保存，有效规范化字幕可在云端结果链接过期后继续使用。

规范化版本 2 增加零时长短句处理：仅当句级时间点与完整 words 文字／标点及其时间点一致、语种支持且文字不超过 12 字时，保留该点并补最多 1000ms 的字幕显示区间，受下一句开始和音频时长限制。显示空间不足 400ms 时，可合并距离不超过 400ms 的相邻同语种有效句，保留两句文字和已有句级边界；无法安全显示或跨语种共享边界仍报错。补充的是显示区间，不声明真实词级语音时长，不改原始响应，不把零时长词用于细分。normalized-v2.json 保存源句索引和修复元数据，警告保存在任务记录中；语种上下文扩展到前后各两句。

旧契约的 ASR／MT 模型及参数完全一致时，尚未规范化的 normalizer:1 任务允许从原始本地结果升级到 2，不重新上传或提交 ASR；已经有效规范化的 v1 字幕及成功译文继续复用。反向时间、非整数时间、缺少一致时间点证据的零时长句仍然拒绝。
