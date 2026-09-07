## WeChat Local Archive v0.2.2

首次 Windows 便携版。v0.2.0 和 v0.2.1 的发布验收未通过、未发布；本版修正了 Windows 依赖锁行尾与源码状态校验后重新构建。解压即可运行，不需要预装 Python、uv 或开发工具，也不需要管理员权限。

### 使用

下载 `wechat-local-archive-windows-x64-v0.2.2.zip`，解压后双击 `WeChatLocalArchive.exe`。命令行入口为 `WeChatLocalArchiveCLI.exe`，可使用 `--help`、`--version`。支持 Windows 10/11 x64。

首次使用需要已登录的 Windows 微信 4.x 完成本地初始化。之后可使用缓存密钥离线归档。默认输出到“下载\wechat-local-archive”，设置与 DPAPI 加密密钥保存在 `%LOCALAPPDATA%\WeChatLocalArchive`；删除程序文件夹不会删除这些数据。

### 本版内容

- 增量、幂等归档与完整性验证；保留已有附件和转写缓存。
- 极简单窗口 GUI，支持会话搜索、日期范围、媒体选择和安全取消。
- `archive.json` 真源、`chat.md`、精简 `ai.jsonl` 和离线 `chat.html` 阅读器。
- SenseVoice 本地批量转写与资源档位；浏览器语音播放使用本地 MP3 缓存。
- 可选 Whisper 复核代码及来源记录；基础便携包不包含 Whisper，需使用 Python 可选依赖安装方式启用。

### 隐私与限制

发布包不包含聊天记录、数据库密钥、模型权重或个人缓存。首次 SenseVoice 模型下载需要网络，之后可离线使用。工具不会发送或修改微信消息，不是微信官方产品。数据库密钥提取仍依赖第三方本地实现，不保证兼容所有微信版本。

本版未进行独立代码签名。若 Windows 显示安全提示，请核对 GitHub 发布来源和 SHA256，不要关闭系统安全保护。完整备份请保留整个归档目录，而不只是 HTML 文件。

### 校验

同页提供 ZIP 的 `.sha256` 文件。构建包内的 `build-info.json` 记录版本、Git commit、tag、Python、PyInstaller 和依赖锁哈希；`SHA256SUMS.txt` 记录内部文件校验值。源码与发布包对应同一 Git tag。
