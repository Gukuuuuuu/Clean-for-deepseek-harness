<div align="center">

# dshClean

**把 DeepSeek Harness 的对话连根拔掉**

正文 · 会话文件夹 · 投影缓存 · 工作区列表 · GUI 草稿 · 附件 · 临时文件 · 系统痕迹

网页点选 · 命令行一键 · 默认只预览 · 删完自动回扫验证

[中文](README.md) · [English](README.en.md)

![Python](https://img.shields.io/badge/Python-3.8%2B-3776ab?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)
![Platform](https://img.shields.io/badge/Platform-macOS%20%E5%B7%B2%E9%AA%8C%E8%AF%81-lightgrey)

</div>
---

> [!NOTE]
> 本项目是第三方工具，与 DeepSeek 官方无关。
## 为什么需要它

DeepSeek Harness（DSH）**没有删除会话的功能**——界面上的「归档」只是把会话从侧边栏隐藏，日志照样留在磁盘上。官方源码里写得很直接：

> 「不删除会话文件——日志在 root 下累积，直到外部移除；seam 无删除接口。」
>
> 「会话删除与文件夹移除是彼此独立且尚未提供的功能。」

而且一次对话的数据**分散在 7 个地方**，只手删 `sessions/` 会留下标题、草稿、索引和引用：

```
$DSH_HOME/                                (默认 ~/.dsh)
├── sessions/<项目目录>/<会话目录>/        ← 对话正文 session.vN.jsonl.zstd + session.lock
├── storages/
│   ├── session_projcache/sessions/*.json  ← 标题 / 待办 / 目标 / 用量 等投影缓存
│   ├── workspace.json (+ .bak-*)          ← 工作区、归档、置顶里的会话引用
│   └── <其它单元>.json                    ← 如 schedule 里的会话引用
├── attachments/ 、cache/                  ← 附件对象与图片缓存
└── (系统临时目录)/dsh-spill-*             ← 超大工具输出的溢出文件

~/Library/Application Support/@deepseek-ai/dsh-desktop/     (macOS)
├── Local Storage/leveldb                  ← GUI 草稿 dsh.conversation.<id>、"当前会话"指针
└── Cache / Code Cache / GPUCache / …      ← 浏览器缓存与运行期残留
```

dshClean 一次性把这些处理干净，并在删除后回扫验证「已删会话 ID 是否还有明文残留」。

## 它到底删了什么

| 位置 | 内容 | 默认 |
|---|---|---|
| `$DSH_HOME/sessions/<项目>/<会话>/` | 对话正文 `session.vN.jsonl.zstd`（含全部历史 generation）、`session.lock` | ✅ 总是 |
| `$DSH_HOME/storages/session_projcache/sessions/<id>.json` | 标题、待办、目标、用量、沙箱模式等投影缓存 | ✅ 总是 |
| `$DSH_HOME/storages/workspace.json` 及 `.bak-*` | `sessionIds` / `archivedSessionIds` / `pinnedSessionIds`（原子写入，保证 JSON 合法） | ✅ 总是 |
| `$DSH_HOME/storages/` 下其它单元 | 指向已删会话的引用与逐记录文件（含 `<id>.json.bak`、`.` 开头的临时文件） | ✅ 总是 |
| `$DSH_HOME/attachments/`、`$DSH_HOME/cache/` | 附件对象、图片请求缓存 | 整体清空时 |
| 桌面端数据目录 12 项 | `Local Storage`（草稿 /「当前会话」指针）、`Session Storage`、各类 Cache、`blob_storage`、`Shared Dictionary`、`Singleton*` | 整体清空时 |
| `$TMPDIR/dsh-spill-XXXXXX`（含 `scoped_dir*/` 内层） | 超大工具输出的 spill 文件 | 整体清空时 |
| `~/Library/Preferences/<bundle-id>.plist` | 只清 `NSOSPLastRootDirectory`（「最近使用目录」痕迹，非对话内容），走 `defaults`/`cfprefsd` | 整体清空时 |
| 应用日志、`DiagnosticReports` 里本应用的崩溃报告 | `~/Library/Logs/DeepSeek Harness/` 等 | 整体清空时 |

**明确不动的**：磁盘上的项目目录本身（如 `~/Projects/my-app`）、账号登录状态（Cookies / Local State，除非加 `--purge-app-dir`）、DSH 的配置与凭据（`profiles/`、`.credentials.yaml`）、废纸篓以外的任何用户文件。
## 它删不掉什么

- **服务端副本**：如果开过「在使用官方模型 API 时上传 Session Log」，DeepSeek 服务端已收到的增量日志本地删不掉，需要在 *设置 → 通用* 关掉该开关。
- **物理层面**：`rm` 只是解除文件链接。在 SSD + APFS 上，`--shred` 的覆盖也**不能保证**物理不可恢复。工具会在删除后检查并提示 FileVault 与 APFS 本地快照状态；要真正防取证，请开启 FileVault（`fdesetup status`），或处置设备前整盘抹除。
- **外部导出副本**：如果你用过「下载 Session Log」，工具会把 `~/Downloads`、`~/Desktop`、`~/Documents` 里的疑似副本**列出来提醒**，但不会替你删。
- **已发出的遥测**：OpenTelemetry 反馈上传属于另一套设置，不在本工具范围内。

## 工作原理

```
扫描(scan) → 生成计划(build_plan) → 预览/确认 → 执行(execute) → 回扫验证(verify)
```



## 免责声明

本工具会**不可逆地删除数据**。请自行确认选择范围并保留必要备份。作者不对任何数据丢失负责。

## License

[MIT](LICENSE)
