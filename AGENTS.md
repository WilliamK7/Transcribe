# 项目工作指南

Windows x64 自用字幕 CLI。用法见 [README.md](README.md)，接口与恢复见 [docs/API_CONTRACT.md](docs/API_CONTRACT.md)，环境维护见 [docs/BUILD.md](docs/BUILD.md)。

- 在仓库根目录使用 PowerShell、`.venv\Scripts\python.exe` 和项目 FFmpeg，先检查现有修改及相关源码。
- 缺陷先补可复现测试；流水线、持久化、媒体或 Windows 改动运行 `scripts\test.ps1` 与 CLI `--self-test`，纯文档和无行为变化的删除检查引用。
- 云端接口变更查证官方文档。依赖同步 `requirements.in` 和完整哈希 `requirements.lock`；媒体资产来源与校验值在 `scripts/release-assets.json`。
- 用中文报告改动与验证，区分模拟接口、本机检查、真实云端和人工效果。
- `data` 含凭据、转写、任务和归档，按需读取；日志与报告省略凭据、签名链接、转写及原始异常。交互语种复核可显示原文。
- 保留用户数据、字幕、备份、归档和实际恢复所需资料。`.venv`、`.build`、`data`、私有媒体与发行产物留在本机。
- 运行时仅支持当前执行契约。历史结果保留为归档，重新处理时从原始转写显式重建；当前任务支持断点恢复。
- 清理临时脚本、合成测试目录和重复验证输出前，核对用途、任务引用及绝对路径；混合目录按文件处理，目录链接单独处理。保留 `.venv/pyvenv.cfg` 指向的 `.build/python` 运行依赖。

源码位于 `subtitle_tool/`，离线回归位于 `tests/`，启动和环境维护入口位于 `scripts/`。
