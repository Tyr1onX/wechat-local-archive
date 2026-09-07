# WeChat Local Archive

一个只读、本地优先的 Windows 微信 4.x 聊天记录归档工具。

目标非常单一：选择一个会话，把本机已有的聊天记录、附件和语音转写成长期可保存、可检索的本地档案。

## 设计原则

- **只读**：不发送、不撤回、不修改任何微信消息。
- **不接管微信 UI**：导出阶段直接读取本机数据库，不点击聊天窗口。
- **首次引导后可离线读取**：首次需要已经登录并运行中的微信，只用于只读提取并验证本机数据库密钥；密钥使用 Windows DPAPI 加密保存。之后只要密钥没有变化，微信可以完全关闭，工具仍可从本地数据库导出。
- **不长期保存明文解密数据库**：解密缓存放在临时目录，命令结束后清理；持久化密钥只保存为 DPAPI 密文。
- **结构化档案为真源**：`archive.json` 是唯一稳定数据源，`chat.md` 和 `ai.jsonl` 都是可重建的派生文件。
- **本地语音转写**：可选 SenseVoiceSmall ONNX 批处理，不上传聊天或音频。

## 数据链路

```text
Windows WeChat 4.x local data
        ↓
wechatauto-replica
(SQLCipher read-only decryption + media extraction)
        ↓
normalized archive.json
        ├─ chat.md
        ├─ ai.jsonl
        └─ assets/
              └─ voice/*.silk
                    ↓
              SenseVoiceSmall ONNX
              batch / CPU / int8
                    ↓
              transcript cache
```

## 为什么首次仍需要微信运行

微信 4.x 的聊天数据库是 SQLCipher 4 加密数据库。磁盘上只有加密数据，没有可直接读取的明文密钥。

本项目不实现登录、不重启微信，也不注入/控制聊天界面。首次执行 `bootstrap` 时，底层只读读取已经登录的 `Weixin.exe` 进程内存，取得并验证数据库密钥及图片派生参数，然后使用 Windows DPAPI 加密保存到当前 Windows 用户。

之后：

```text
DPAPI secret → validated DB keys → local SQLCipher decrypt → local DB export
```

所以正常日常使用不需要微信保持打开；只有微信重新登录后密钥发生变化、或首次从未初始化过时，才需要重新 `bootstrap`。

## Windows 便携版

普通用户优先从 [GitHub Releases](https://github.com/Tyr1onX/wechat-local-archive/releases) 下载 Windows x64 ZIP。解压后双击 `WeChatLocalArchive.exe`，无需安装 Python、uv 或管理员权限；命令行入口是 `WeChatLocalArchiveCLI.exe`。基础包包含本地 SenseVoice 运行时，但不包含模型权重或可选 Whisper 复核依赖。首次模型下载后可离线转写。

便携版与 Python 版使用相同的 `%LOCALAPPDATA%\WeChatLocalArchive` 设置、DPAPI 密钥和默认归档目录。删除程序文件夹不会删除用户数据；不要把密钥或聊天归档复制到发布包中。发布包提供 `build-info.json`、`SHA256SUMS.txt` 和第三方许可文本，ZIP 旁提供 SHA256 校验文件。当前没有独立代码签名，不应通过关闭 Windows 安全保护来解决下载警告。

## Python 安装（开发与高级功能）

要求：Windows 10/11、64 位 Python 3.12、Windows 微信 4.x。正式/可复现安装使用仓库中的 `requirements-win.lock`；它包含当前已验证的完整 Windows 依赖图和语音转写依赖。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install "uv==0.12.7"
.\.venv\Scripts\uv.exe pip install --python .\.venv\Scripts\python.exe -r requirements-win.lock
.\.venv\Scripts\uv.exe pip install --python .\.venv\Scripts\python.exe --no-deps --no-build-isolation .
```

这样安装得到的核心版本与 Windows CI 使用的版本一致。SenseVoice 模型文件不进入 lock，也不会提交到 Git；第一次真正执行语音转写时仍会下载到本机 `%LOCALAPPDATA%` 模型缓存。

开发时如果需要重新解析允许范围内的新依赖，可以直接使用 `pyproject.toml`：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[voice,dev]"
```

修改依赖声明后，用固定的 uv 版本重新生成 lock；复核与打包依赖必须受基础 lock 约束，避免升级已验证的微信适配器：

```powershell
uv pip compile pyproject.toml --extra voice --extra dev --python-version 3.12 --python-platform windows --output-file requirements-win.lock
uv pip compile pyproject.toml --extra voice --extra verify --extra dev --python-version 3.12 --python-platform windows --constraint requirements-win.lock --output-file requirements-win-verify.lock
uv pip compile pyproject.toml --extra voice --extra dev --extra build --python-version 3.12 --python-platform windows --constraint requirements-win.lock --output-file requirements-win-build.lock
```

## 图形界面

安装后运行 `wechat-archive-gui`，或使用 `wechat-archive gui`。界面使用 Windows 自带的 tkinter/ttk，不需要 Electron、WebView 或额外 GUI 运行时。若 Python 提示缺少 tkinter，请安装包含 Tcl/Tk 的完整 Python 3.12。

一个窗口即可完成：首次选择本地账号并初始化、搜索会话、填写可选日期范围、选择图片/语音/视频文件、选择语音转写资源档位、导出、验证和打开文件夹。默认输出到“下载\wechat-local-archive”，可自行修改。已有匹配的旧归档会复用；新归档使用稳定的会话 ID 目录，避免同名联系人混淆。

导出默认增量更新。`CANCEL` 会等待当前不可中断的数据库或模型操作结束，然后安全停止；已经完成的语音转写会保留在本地缓存，下次继续使用。关闭正在工作的窗口也会先请求取消，不会直接丢弃后台写入。窗口仅记住输出根目录和 ASR 档位。首次下载模型需要网络，之后可离线转写。

## 使用

每次成功导出都会生成 `archive.json`、`chat.md` 和 `ai.jsonl`。只需要把 `ai.jsonl` 上传给 AI 做文字/语音转写分析时，不必上传整个附件目录；需要让 AI 实际查看图片、视频或文件时，还要提供相应附件。`ai.jsonl` 不会自动识别图片或视频内容。


### AI 专用档案

`ai.jsonl` 每行是一条独立 JSON 消息，按原始顺序保留 `time`、`sender`、`type`、`text`，有附件时再保留相对路径 `asset`。语音使用已有转写，未转写时明确标记；不会复制数据库内部 ID、排序字段或原始 XML。它是可读性和体积优化，不是无损备份，也不包含附件二进制内容。

已存在的旧档案无需重新读取微信、提取媒体或转写，可直接运行：

```powershell
wechat-archive ai D:\Archives\chat
```

该命令只读取已有 `archive.json` 并重建 `ai.jsonl`；内容未变化时不会改写文件。日常增量导出也会自动补齐缺失或过期的 AI 文件。`verify` 会检查已存在的 `ai.jsonl` 是否与真源一致；旧档案尚未生成 AI 文件时仍可正常验证。

### 可选语音复核

默认仍只使用 SenseVoice，不安装或初始化 Whisper。需要复核时，先安装可选依赖；正式环境使用 `requirements-win-verify.lock`，它保留基础 lock 中的微信适配器版本。也可在开发环境安装 `.[voice,verify]`。

```powershell
.\.venv\Scripts\uv.exe pip install --python .\.venv\Scripts\python.exe -r requirements-win-verify.lock
```

已有档案可以完全不打开微信，按消息 ID 复核；默认不会替换原转写：

```powershell
wechat-archive retranscribe D:\Archives\chat --id <message-id>
```

首次使用可通过 `--download-model` 明确允许下载模型，或用 `--model-dir` 指向已有的转换模型目录。默认 `small`、CPU/int8、background 两线程；可选 `large-v3-turbo`，但没有经过本项目的中文准确率验证。模型下载不上传聊天或音频。

复核结果保存在 `archive.json` 的 `transcript_reviews`，包含模型、原文、复核文本和音频哈希；冲突不自动判定谁正确。确认后可显式选用：

```powershell
wechat-archive retranscribe D:\Archives\chat --id <message-id> --apply-review
```

原文会保留在 provenance 中；同一模型有多个版本结果时需要 `--review-key` 指定。也可使用 `--start`、`--end` 或 `--all-suspicious` 限定范围，默认最多处理 20 条，`--limit` 可调整。普通导出可显式增加 `--voice-quality verify`，只对可疑项做二次复核，不自动替换文本；原有 `--asr-preset` 仍是资源档位，与质量模式独立。

复核不是人工校对，也不提供可靠的逐字置信度。参见 [语音复核基准与边界](docs/voice-quality.md)。

### 1. 首次初始化

先正常登录 Windows 微信，然后：

```powershell
wechat-archive bootstrap
```

初始化成功后可以关闭微信。

### 2. 检查离线状态

```powershell
wechat-archive doctor
```

### 3. 查看会话

```powershell
wechat-archive chats
wechat-archive chats --search 示例会话
```

### 4. 导出一个会话

```powershell
wechat-archive export <username> --out D:\Archives\chat
```

限定日期：

```powershell
wechat-archive export <username> --out D:\Archives\chat --start 2026-01-01 --end 2026-09-06
```

导出本机已有媒体：

```powershell
wechat-archive export <username> --out D:\Archives\chat --media all
```

同时批量转写语音：

```powershell
wechat-archive export <username> --out D:\Archives\chat --media all --transcribe
```

默认 ASR 档位是 `balanced`。只需要选择资源档位，不需要直接理解 ONNX 参数：

```text
background  batch=4   threads=2   # 边工作边后台归档
balanced    batch=8   threads=4   # 默认
fast        batch=16  threads=8   # 首次大量归档
```

例如低占用后台转写：

```powershell
wechat-archive export <username> --out D:\Archives\chat --media all --transcribe --asr-preset background
```

`--batch-size` 仍保留为高级 override，但不会改变所选 preset 的线程上限。已有档案没有待转写语音时不会初始化 SenseVoice 模型。

同一输出目录再次执行时默认是**增量、幂等更新**：已有消息不会重复追加，存在的附件不会重新复制/解密，已有语音转写不会重新推理；如果旧消息的附件后来才落到本机，也会只补齐缺失项。需要明确全量重建时使用：

```powershell
wechat-archive export <username> --out D:\Archives\chat --media all --transcribe --refresh
```

`archive.json` 的账号、会话或日期范围与本次请求不匹配时会拒绝合并，避免把两个档案混在一起。

导出完成后可检查档案完整性：

```powershell
wechat-archive verify D:\Archives\chat
```

它会检查 schema、账号/会话元信息、重复消息、时间顺序、附件路径/缺失文件、`chat.md` 一致性，并报告语音转写覆盖率和未引用的 assets。孤儿附件默认不会删除；确认档案本身验证通过后可显式清理：

```powershell
wechat-archive verify D:\Archives\chat --prune-orphans
```

`archive.json` 与 `chat.md` 都通过临时文件 + atomic replace 更新，其中 `archive.json` 作为真源最后替换；长任务中断不会把旧的 canonical archive 覆盖成半文件。

第一次转写会下载约 242 MB 的 SenseVoiceSmall INT8 ONNX 模型；之后可离线运行。

## 输出

```text
chat/
├─ archive.json        # 唯一稳定数据源（schema 4，含可选转写来源/复核记录）
├─ chat.md             # 文本阅读
├─ ai.jsonl            # AI 分析用精简输出
├─ chat.html           # 双击即可打开的离线阅读器
└─ assets/
   ├─ image/
   ├─ voice/           # 原始 SILK
   ├─ reader-audio/    # 浏览器播放用 MP3 派生缓存
   ├─ video/
   └─ file/
```

### 离线 HTML 阅读器

正常导出会同时生成 `chat.html`。直接双击即可在 Chrome/Edge 打开，不需要本地服务器、前端构建工具或网络连接。支持左右消息、日期分隔、全文搜索、月份/日期跳转和每页 80 条的分页；图片、视频、文件仍从本地 `assets/` 读取。

已有档案可单独重建，不读取微信数据库，也不会重新运行 ASR：

```powershell
wechat-archive html D:\Archives\chat
```

原始 SILK 语音会按内容哈希转换成 48 kbps MP3，保存在 `assets/reader-audio/`；再次重建复用缓存，不修改原始语音或 `archive.json`。如果只需要阅读转写、暂时不转换音频：

```powershell
wechat-archive html D:\Archives\chat --no-audio
```

转换失败会保留转写和原始语音链接。生成的 HTML 不加载 CDN、远程字体或脚本；CSP 禁止网络连接，聊天内容按纯文本渲染。可删除 `chat.html` 和 `assets/reader-audio/` 后从 `archive.json` 重建。`verify` 会检查已有 HTML 是否与当前档案一致，孤儿文件清理不会删除阅读器音频缓存。

注意：HTML 是本地阅读器，不是独立的完整备份。移动它时应连同 `assets/` 一起移动；若需保存完整记录，保留整个归档目录。发送给 AI 做文本分析仍优先使用 `ai.jsonl`。

## 构建与发布

Windows x64 构建使用固定的 `requirements-win-build.lock` 和 PyInstaller onedir。GUI、CLI 共用一套运行时，不使用 onefile 或常驻服务器。构建前先安装锁定依赖及本项目，再执行：

```powershell
.\.venv\Scripts\python.exe scripts/build_portable.py
.\.venv\Scripts\python.exe scripts/smoke_portable.py dist/wechat-local-archive-windows-x64-v0.2.2.zip
```

`smoke_portable.py` 会将 ZIP 解压到隔离临时目录，清除 Python 虚拟环境路径并使用临时用户配置，检查版本、隐藏 GUI 初始化、原生 ASR 依赖及合成档案的 verify/AI/HTML/语音播放。它不读取真实微信数据。真实微信 bootstrap 与离线导出仍需在有授权测试账号的环境单独验证。

`.github/workflows/portable.yml` 在 Windows CI 中从锁定依赖构建、测试并上传 ZIP artifact。仅 `v<版本>` tag 触发 Release，发布前检查 tag/commit/版本/锁哈希及包内文件校验。`scripts/verify_release.py` 可独立执行同样的来源校验。发布说明见 `docs/release-v0.2.2.md`。

## 隐私与风险边界

- 不上传聊天内容、附件或语音。
- 不提供发送消息、自动回复、加好友、群控等能力。
- 仓库不会保存任何真实微信数据、密钥或导出产物。
- 首次数据库密钥提取仍属于读取微信进程内存的第三方行为；它不是微信官方导出接口，不能视为零风控风险。

底层数据库兼容依赖 `wechatauto-replica`（Apache-2.0）；SenseVoice（MIT）仅作为可选本地语音识别能力。
