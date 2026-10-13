# 项目工作指南

Windows x64 自用命令行字幕工具。用法见 README.md，接口与恢复规则见 docs/API_CONTRACT.md，环境准备见 docs/BUILD.md。

## 工作方式

- 先检查现有修改和相关源码；在仓库根目录用 PowerShell，优先使用 `.venv\Scripts\python.exe` 和项目 FFmpeg。
- 根据改动验证实际行为。缺陷先补可复现测试；流水线、持久化、媒体或 Windows 改动运行 `scripts\test.ps1` 和 CLI `--self-test`。纯文档及无行为变化的删除检查引用即可。
- 云端接口变更查证阿里云官方文档。依赖变更同步 requirements.in 与完整哈希 requirements.lock；媒体资产来源和校验值位于 scripts/release-assets.json。
- 用中文简洁说明改动、实际检查和未验证项。模拟接口、本机检查、真实云端和人工效果分别报告。
- 文档记录当前用法与必要契约；阶段性计划、测试输出和清理快照留在本机 `.build`，避免重复维护。

## 产品行为

- 新任务使用北京 Workspace ID，固定 ASR `qwen3-asr-flash-filetrans`、MT `qwen-mt-flash`。ASR 自动检测语言；中文／粤语在 MT 前跳过，歧义进入终端复核。
- 双语默认原文在上、简体中文译文在下；繁简转换只作用于译文。默认输出同目录同名 SRT，覆盖先备份，处理同名不同扩展冲突。
- 默认顺序执行 ASR、MT 并发 2；上传为整文件表单。MT 请求和重试共享间隔及限流冷却，按稳定 cue_id 保存译文。
- 多音轨／声道和语种歧义由用户确认；非交互保存 needs_review、非零退出并给出恢复命令。Ctrl+C 保存状态，取消任务仅显式恢复。
- 保留任务库独占锁、提交未知防重发、缓存身份与完整源文件指纹校验。恢复沿用原端点、输入和媒体方案，优先复用本地结果及成功译文。
- 媒体复制验证时间轴、延迟和音频属性；需要转换时使用 16kHz 单声道 FLAC，超限按 PCM 样本精确分段。FFmpeg 使用参数数组及 shell=False。
- 规范化保留原文和原始时间依据；零时长修复记录警告与来源。SRT 使用 UTF-8 无 BOM、CRLF、合法毫秒时间和同目录原子提交。无语音完成且不生成 SRT。

## 数据边界

- Key 在本机隐藏输入，原子明文保存 data/key.txt；DASHSCOPE_API_KEY 优先。旧 DPAPI 仅解密迁移，新数据直接保存。
- data 含个人凭据、上传引用、转写、任务和缓存。只读取排查所需内容；日志与普通报告隐藏 Key、Bearer、签名链接、转写和原始异常。终端语种复核可显示原文。
- 保留用户数据、字幕及备份。旧任务格式的兼容代码按实际恢复用途判断，删除前确认数据仍可读取。
- .venv、.build、data、媒体样片和旧发行包留在本机。Windows 递归删除／移动先验证绝对路径范围，文件操作用 -LiteralPath。

## 源码位置

- main.py：CLI、配置、任务管理、终端复核、进度和中断。
- common.py、config.py：常量、身份、原子写入、Key 与旧数据解密。
- api.py：上传、ASR、下载、MT 与重试调度。
- media.py、subtitles.py：音轨、时间轴、分段、语言分流和 SRT。
- store.py、engine.py：SQLite、流水线、缓存与恢复。
- instance.py、diagnostics.py：独占锁、本机诊断。
- tests/：离线回归；scripts/：环境准备、启动与测试入口。
