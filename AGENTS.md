# 项目工作指南

Windows x64 自用命令行字幕工具。当前用法见 [README.md](README.md)，请求、持久化和恢复规则见 [docs/API_CONTRACT.md](docs/API_CONTRACT.md)，环境维护见 [docs/BUILD.md](docs/BUILD.md)。

## 工作方式

- 先检查现有修改和相关源码；在仓库根目录用 PowerShell，优先使用 `.venv\Scripts\python.exe` 和项目 FFmpeg。
- 根据改动验证实际行为。缺陷先补可复现测试；流水线、持久化、媒体或 Windows 改动运行 `scripts\test.ps1` 和 CLI `--self-test`。纯文档及无行为变化的删除检查引用即可。
- 云端接口变更查证阿里云官方文档。依赖变更同步 `requirements.in` 与完整哈希 `requirements.lock`；媒体资产来源和校验值位于 `scripts/release-assets.json`。
- 用中文简洁说明改动、实际检查和未验证项，分别报告模拟接口、本机检查、真实云端和人工效果。
- 文档记录当前用法与必要契约；阶段计划、测试输出和清理快照留在本机 `.build`。

## 数据边界

- `data` 含凭据、上传引用、转写、任务和缓存，只读取排查所需内容。日志与普通报告隐藏 Key、Bearer、签名链接、转写和原始异常；终端语种复核可显示原文。
- 保留用户数据、字幕及备份。旧格式代码按实际恢复用途判断，删除前确认数据仍可读取。
- `.venv`、`.build`、`data`、媒体样片和旧发行包留在本机。Windows 递归删除／移动前验证绝对路径范围，文件操作用 `-LiteralPath`。

## 源码位置

`subtitle_tool/` 中，`main.py` 负责 CLI 与复核，`api.py` 负责云端请求和重试，`media.py` 与 `subtitles.py` 负责媒体和字幕，`store.py` 与 `engine.py` 负责任务持久化和恢复，`common.py` 与 `config.py` 负责配置及通用操作，`instance.py` 与 `diagnostics.py` 负责独占锁和本机诊断。`tests/` 为离线回归，`scripts/` 为环境准备、启动与测试入口。
