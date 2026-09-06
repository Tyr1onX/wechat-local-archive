# WeChat Local Archive

一个只读、本地优先的 Windows 微信 4.x 聊天记录归档工具。

目标非常单一：选择一个会话，把本机已有的聊天记录、附件和语音转写成长期可保存、可检索的本地档案。

## 设计原则

- **只读**：不发送、不撤回、不修改任何微信消息。
- **不接管微信 UI**：导出阶段直接读取本机数据库，不点击聊天窗口。
- **首次引导后可离线读取**：首次需要已经登录并运行中的微信，只用于只读提取并验证本机数据库密钥；密钥使用 Windows DPAPI 加密保存。之后只要密钥没有变化，微信可以完全关闭，工具仍可从本地数据库导出。
- **不长期保存明文解密数据库**：解密缓存放在临时目录，命令结束后清理；持久化密钥只保存为 DPAPI 密文。
- **结构化档案为真源**：`archive.json` 是唯一稳定数据源，Markdown 只是便于阅读的派生文件。
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

## 安装

要求：Windows 10/11、64 位 Python 3.11/3.12、Windows 微信 4.x。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

需要本地语音转写：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[voice]"
```

## 使用

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

第一次转写会下载约 242 MB 的 SenseVoiceSmall INT8 ONNX 模型；之后可离线运行。

## 输出

```text
chat/
├─ archive.json        # 稳定结构化档案
├─ chat.md             # 人类可读版本
└─ assets/
   ├─ image/
   ├─ voice/
   ├─ video/
   └─ file/
```

## 隐私与风险边界

- 不上传聊天内容、附件或语音。
- 不提供发送消息、自动回复、加好友、群控等能力。
- 仓库不会保存任何真实微信数据、密钥或导出产物。
- 首次数据库密钥提取仍属于读取微信进程内存的第三方行为；它不是微信官方导出接口，不能视为零风控风险。

底层数据库兼容依赖 `wechatauto-replica`（Apache-2.0）；SenseVoice（MIT）仅作为可选本地语音识别能力。
