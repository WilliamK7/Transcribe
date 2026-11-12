# 项目工作指南

Windows x64 自用字幕 CLI。用法见 [README.md](README.md)，接口与恢复见 [docs/API_CONTRACT.md](docs/API_CONTRACT.md)，环境维护见 [docs/BUILD.md](docs/BUILD.md)。

- 在仓库根目录用 PowerShell、`.venv\Scripts\python.exe` 和项目 FFmpeg。改动前检查工作区及相关源码。
- 修复缺陷先补复现测试。改动流水线、持久化、媒体或 Windows 行为时运行 `scripts\test.ps1` 和 CLI `--self-test`。文档和无行为变化的删除检查引用。
- 云端接口以官方文档为准。依赖更新同步 `requirements.in` 与完整哈希 `requirements.lock`，媒体资产校验见 `scripts/release-assets.json`。
- 维护执行契约和断点恢复。历史结果归档，重建从原始转写开始。保留用户数据、字幕、备份、归档、恢复资料及运行依赖。删除前核对绝对路径、用途和任务引用，混合目录按文件处理，链接单独核对。
- 用中文报告改动和验证，区分模拟接口、本机检查、真实云端与人工效果。按需读取私有数据，日志和报告只记录脱敏元数据，交互语种复核可显示原文。
