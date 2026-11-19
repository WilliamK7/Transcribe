# Magpie 字幕工具

Windows x64 自用字幕 CLI。新任务用北京百炼 `qwen-audio-3.1-asr-flash-filetrans` 识别音频，Magpie 翻译外语，在媒体旁生成同名 UTF-8 SRT。中文和粤语保留原文，外语默认原文在上、简体中文在下。已有字幕默认跳过。

## 开始使用

```powershell
.\scripts\setup.ps1
.\scripts\transcribe.ps1 --configure-key
```

在 Magpie 中配置渠道。在 `data/settings.json` 填入百炼工作空间和 Magpie 数据目录，其余设置可用默认值。

```json
{
  "workspace_id": "your-workspace-id",
  "magpie_data_dir": "D:\\Program Files\\magpie\\data"
}
```

在 Magpie 启动网关，预览候选并用合成文本验收字幕组。`--apply` 会调用模型并备份分组配置。

```powershell
.\.venv\Scripts\python.exe scripts\configure-mt.py --list-candidates
.\.venv\Scripts\python.exe scripts\configure-mt.py --apply
.\scripts\transcribe.ps1 "D:\Videos\课程.mkv"
```

也可在资源管理器选择“发送到 → !百炼字幕”。多个文件顺序处理。音轨、声道或语种有歧义时按提示选择，输入 `q` 可暂停。`--non-interactive` 把歧义留作待复核任务。`--track` 使用 FFprobe 音轨索引，`--channel` 可选 `left`、`right`、`mix`。`--output-dir` 指定输出目录，`--overwrite` 在覆盖前备份原字幕。完整参数见 `--help`。

识别保留方言表达和字词时间戳，按 token 计费；语种标签缺失且文字规则无法确定时进入复核。[官方指南](https://docs.bailian.console.aliyun.com/zh/model-studio/non-realtime-speech-recognition-user-guide)

## 配置 Magpie

| 角色 | 主组 | 商业后备 |
| --- | --- | --- |
| 正文翻译 | `group/transcribe` | `group/transcribe-paid` |
| 术语和复核 | `group/transcribe-review` | `group/transcribe-review-paid` |
| 疑难裁决 | `group/transcribe-judge` | `group/transcribe-judge-paid` |

主组按免费或试用、商业的顺序选择供应商。超时或输出校验失败时，字幕工具可调用该角色的商业后备。三个角色使用互相独立的底层型号。vLLM-SR `auto` 需在 Magpie 渠道的响应头透传中添加 `X-Vsr-Selected-Model`。

```powershell
.\.venv\Scripts\python.exe scripts\manage-mt.py --status
.\.venv\Scripts\python.exe scripts\manage-mt.py --enable
.\.venv\Scripts\python.exe scripts\manage-mt.py --once
.\.venv\Scripts\python.exe scripts\manage-mt.py --add-provider 供应商ID
.\.venv\Scripts\python.exe scripts\manage-mt.py --disable
```

`--status` 从本机记录列出每个角色的渠道、底层型号、费用分类、试用状态和成员归属。`--check-mt` 通过一次真实翻译检查网关。字幕任务会复用运行中的网关，也可自动启动并在退出时关闭本机便携版。默认组支持自动维护，自定义组和远程网关由用户管理。

`data/mt-channels.json` 登记候选与费用规则。`preferred` 调整考察顺序，`price_exceptions` 逐个授权超价或价格未知的商业型号。Vercel 默认单价上限为每百万 token 输入 2 美元、输出 10 美元。`excluded_providers` 可暂停已登记渠道并保留其配置。Magpie 的 `listOff` 仅停止抓取目录，`noModels` 停止供应型号，`off` 停用整个渠道。实际费用按账单核对。[配置模板](subtitle_tool/mt-channels.default.json)

普通渠道维护默认每 15 分钟刷新，单轮最多验收 1 个候选，UTC 当天最多 2 个。`--once` 共用预算。百炼试用从已绑定的查询账户获取额度，`bailian-trial-state.json` 的 `paused` 和 `excluded` 控制暂停与排除。`configure-mt.py --refresh-trial` 可立即刷新库存。

## 项目配置

| 设置 | 用途 |
| --- | --- |
| `mt_base_url` | 默认 `http://127.0.0.1:3425/v1`，端口须与本机 Magpie 设置一致 |
| `mt_model` / `mt_fallback_model` | 正文主组与商业后备。另两个角色使用 `mt_review_*` 和 `mt_judge_*` |
| `mt_policy_version` | 更换策略并希望重译时递增，然后执行 `--reflow` |
| `mt_quality.manual_glossary` | 优先使用的人工术语映射，最多 200 项 |
| `mt_workers` | 同时在途的翻译阶段数，默认 16。供应商的并发和每分钟请求数另行生效 |
| `zh_line_width` / `other_line_width` | 默认 44 / 84，范围 12～72 / 24～160 |
| `chinese_first` | 设为 `true` 时把译文排在原文上方 |

识别凭据优先使用 `DASHSCOPE_API_KEY`，也可通过 `--configure-key` 保存到 `data/key.txt`。翻译使用 `MAGPIE_API_KEY` 或 `data/magpie-key.txt`，本机默认 `magpie-transcribe`。翻译地址和六个组名支持对应的大写环境变量覆盖。

## 任务管理

| 命令 | 用途 |
| --- | --- |
| `--jobs` | 查看保存的任务和下一步可用命令 |
| `--review ID` | 确认音轨、声道或语种后继续 |
| `--resume ID` | 沿原任务的排版、账户与翻译契约继续。已有转写就读本地，已有云端任务 ID 就查询原任务 |
| `--reflow ID` | 从完整原始转写按当前配置重建，保留能对应到原句的人工决定，必要时重新翻译 |
| `--retime ID` | 校验已保存的完整字幕和译文，只更新显示时间，全程在本机执行 |
| `--cancel ID` | 停止本地任务。已提交的云端请求仍可能计费 |
| `--export-archive ID --output-dir DIR` | 校验后按原字节导出归档字幕。归档也可用 `--reflow ID --output-dir DIR` 创建新任务 |

重排和校时中断后都可用 `--resume` 继续，覆盖已有字幕需加 `--overwrite`。原媒体移走后，完整本地结果或已有云端任务 ID 仍可用于恢复。原路径被其他媒体占用时改用 `--output-dir`。提交结果未知时，使用 `--resume ID --bind-task-id 云端ID` 绑定原任务，或用 `--allow-resubmit` 明确接受重复计费后重发。

`Ctrl+C` 保存进度。进度写入 stderr，stdout 每行一个 JSON 结果，`--report 路径` 可另存 JSON 数组。退出码 `0` 表示完成或跳过，`2` 表示任务失败或待复核，`3` 表示启动或本地异常，`130` 表示用户中断。

## 本机检查

`.\scripts\transcribe.ps1 --self-test` 用临时目录和合成音频检查配置、任务锁、FFmpeg 与字幕写入。项目移动后用 `.\scripts\install-sendto.ps1` 重建“发送到”入口，`-Remove` 可移除入口。

[环境维护](docs/BUILD.md) · [接口与恢复契约](docs/API_CONTRACT.md) · [第三方组件](docs/THIRD_PARTY.txt) · [源码许可](LICENSE)
