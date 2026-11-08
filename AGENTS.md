# 项目工作指南

Windows x64 自用字幕 CLI。用法见 [README.md](README.md)，接口与恢复见 [docs/API_CONTRACT.md](docs/API_CONTRACT.md)，环境维护见 [docs/BUILD.md](docs/BUILD.md)。

- 在仓库根目录使用 PowerShell、`.venv\Scripts\python.exe` 和项目 FFmpeg，先检查现有修改及相关源码。
- 缺陷先补可复现测试；流水线、持久化、媒体或 Windows 改动运行 `scripts\test.ps1` 与 CLI `--self-test`，纯文档和无行为变化的删除检查引用。
- 云端接口变更查证官方文档；依赖同步 `requirements.in` 和完整哈希 `requirements.lock`；媒体资产来源与校验值在 `scripts/release-assets.json`。
- 用中文报告改动与验证，区分模拟接口、本机检查、真实云端和人工效果。按需读取私有数据；日志与报告仅记录脱敏元数据，交互语种复核可显示原文。
- 维护当前执行契约和断点恢复；历史结果归档，重新处理时从原始转写显式重建。
- 保留用户数据、字幕、备份、归档和恢复资料。清理前核对绝对路径、用途及任务引用，混合目录按文件处理、目录链接单独处理。`.venv/pyvenv.cfg` 指向的 `.build/python` 属于运行依赖；本地环境、私有数据和发行产物由 `.gitignore` 管理。

源码位于 `subtitle_tool/`，离线回归位于 `tests/`，启动和环境维护入口位于 `scripts/`。
