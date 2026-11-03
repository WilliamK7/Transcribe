# 项目工作指南

Windows x64 自用字幕 CLI。用法见 [README.md](README.md)，接口与恢复见 [docs/API_CONTRACT.md](docs/API_CONTRACT.md)，环境维护见 [docs/BUILD.md](docs/BUILD.md)。

## 工作方式

- 先检查现有修改和相关源码，在仓库根目录使用 PowerShell、`.venv\Scripts\python.exe` 和项目 FFmpeg。
- 缺陷先补可复现测试；流水线、持久化、媒体或 Windows 改动运行 `scripts\test.ps1` 和 CLI `--self-test`。纯文档和无行为变化的删除检查引用。
- 云端接口变更查证服务商官方文档。依赖变更同步 `requirements.in` 与完整哈希 `requirements.lock`；媒体资产来源和校验值在 `scripts/release-assets.json`。
- 用中文报告改动与验证，区分模拟接口、本机检查、真实云端及人工效果。文档保留当前用法和必要契约。

## 数据边界

`data` 含凭据、任务、转写和缓存，只读取排查所需内容。普通报告和日志隐藏凭据、签名链接、转写及原始异常；交互语种复核可显示原文。用户数据、字幕、备份及实际恢复所需的历史读取逻辑均须保留。`.venv`、`.build`、`data`、私有媒体和发行产物留在本机。

## 本机记录清理

- 保留当前环境依赖（包括 `.venv/pyvenv.cfg` 指向的 `.build/python`）、用户资料、真实任务恢复快照及未解决问题所需材料。
- 成果进入源码、回归测试或必要文档后，清除阶段计划、临时脚本、合成测试目录、浏览器测试缓存、代码副本和重复验证输出；通常只留最近验证结果与简短清理摘要。
- 删除前核对用途、任务引用和绝对路径；混合目录按文件分类。Windows 文件操作使用 `-LiteralPath`，递归删除／移动限定在已核实的目标范围，目录链接单独处理。

## 源码位置

- `main.py`：CLI；`engine.py`：任务生命周期；`store.py`：持久化事务。
- `review.py`、`policy.py`、`saved_results.py`、`publication.py`：人工决定、执行版本、恢复校验及字幕发布。
- `api.py`、`mt_backend.py`、`mt.py`：云端请求、后端身份及单后端翻译。
- `mt_pool.py`、`mt_routing.py`、`mt_pipeline_v5.py`、`mt_quality.py`：翻译池配置、调度、分组流水线及质量校验。
- `mt_runtime.py`、`mt_transport.py`、`mt_control.py`、`mt_fallback.py`：重试限速、网络子进程、取消及单后端后备。
- `mt_quota_v5.py`、`mt_bailian_quota.py`、`mt_bailian_binding.py`：免费额度与账户绑定。
- `media.py`、`subtitles.py`：媒体和字幕；`common.py`、`config.py`、`instance.py`、`diagnostics.py`、`mt_diagnostics.py`：配置、锁及诊断。
- `tests/`：离线回归；`scripts/`：环境准备、启动和测试入口。
