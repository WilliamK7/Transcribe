# 项目工作指南

Windows x64 自用命令行字幕工具。当前用法见 [README.md](README.md)，请求、持久化和恢复规则见 [docs/API_CONTRACT.md](docs/API_CONTRACT.md)，环境维护见 [docs/BUILD.md](docs/BUILD.md)。

## 工作方式

- 先检查现有修改和相关源码；在仓库根目录用 PowerShell，优先使用 `.venv\Scripts\python.exe` 和项目 FFmpeg。
- 根据改动验证实际行为。缺陷先补可复现测试；流水线、持久化、媒体或 Windows 改动运行 `scripts\test.ps1` 和 CLI `--self-test`。纯文档及无行为变化的删除检查引用即可。
- 云端接口变更查证阿里云官方文档。依赖变更同步 `requirements.in` 与完整哈希 `requirements.lock`；媒体资产来源和校验值位于 `scripts/release-assets.json`。
- 用中文简洁说明改动、实际检查和未验证项，分别报告模拟接口、本机检查、真实云端和人工效果。
- 文档记录当前用法与必要契约；开发计划、实验和测试输出放本机 `.build`，按下述用途清理。

## 数据边界

- `data` 含凭据、上传引用、转写、任务和缓存，只读取排查所需内容。日志与普通报告隐藏 Key、Bearer、签名链接、转写和原始异常；终端语种复核可显示原文。
- 保留用户数据、字幕及备份。旧格式代码按实际恢复用途判断，删除前确认数据仍可读取。
- `.venv`、`.build`、`data`、私有媒体和发行产物不提交 Git。“仅存本机”不表示永久保留。

## 本机记录清理

- 保留当前环境依赖（包括 `.venv/pyvenv.cfg` 指向的 `.build/python`）、用户数据／字幕／备份、真实任务恢复快照，以及未解决问题需要的诊断和复现材料。读取任务路径及版本即可判断时，不读取凭据或正文。
- 已完成工作的阶段计划、逐轮测试日志、重复自检报告、临时脚本、合成测试目录、浏览器测试缓存和代码清理副本，在成果已进入源码、回归测试、必要文档或 Git 后可删除。已结束且不再用于排查的实验输出也可删除；代码副本与真实任务／用户备份分别判断。
- 通常保留最近一次验证结果和简短清理摘要即可；当前仍需调查或回滚的材料按用途保留。清理审计使用路径清单及必要校验值即可。
- 删除前核对用途、任务引用和实际路径；证据不足的混合数据目录按文件分类。Windows 文件操作用 `-LiteralPath`，递归删除／移动前确认绝对路径在目标范围内；目录链接单独处理，避免进入其目标。

## 源码位置

`subtitle_tool/` 中，`main.py` 负责 CLI 交互，`engine.py` 协调任务生命周期与恢复，`store.py` 提供持久化事务。`review.py` 表达人工决定，`policy.py` 解析执行版本，`saved_results.py` 校验恢复产物，`publication.py` 发布和恢复 SRT。`api.py` 负责云端请求和重试，`mt_backend.py` 管理翻译后端身份与快照，`mt.py` 负责批量翻译提示词与响应校验，`media.py` 与 `subtitles.py` 负责媒体和字幕规则，`common.py` 与 `config.py` 负责配置及通用操作，`instance.py` 与 `diagnostics.py` 负责独占锁和本机诊断。`tests/` 为离线回归，`scripts/` 为环境准备、启动与测试入口。

翻译运行控制位于 `mt_runtime.py`（重试、冷却及额度）、`mt_transport.py`（有时限的网络子进程）、`mt_control.py`（取消收尾）与 `mt_fallback.py`（按组后备及译文来源）；`mt_diagnostics.py` 保存脱敏诊断。
