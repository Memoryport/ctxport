# ctxport companion (browser extension)

一键把你自己的 **ChatGPT / Claude.ai / Gemini** 网页对话同步进本机 ctxport，不用申请官方导出、不用等邮件、不用手动导入。
Chrome / Edge（Manifest V3）。

## 工作方式

```
你已登录的 chatgpt.com / claude.ai / gemini.google.com 页面
   │  content script 用页面自己的登录状态（同源请求）读取你的对话
   ▼
扩展后台 ──► http://127.0.0.1:8765 （ctxport serve-ingest，只监听本机）
   ▼
~/.ctxport/inbox/<platform>/<id>.json  →  立即导入 ~/.ctxport/ctxport.db
```

* **只读你自己的数据**，只发给本机 `127.0.0.1`，不经过任何第三方服务器；不申请 `cookies` 权限，不读取或导出任何登录凭证。
* 增量同步：只下载新增或更新过的对话（ChatGPT/Claude 按 `update_time`；Gemini 没有更新时间，所以除了新对话，每次还会重新读取侧边栏里最近的 10 个对话）。勾选“全量重新下载”可重来。
* 包括已归档对话、ChatGPT Projects 里的对话、ChatGPT 记忆与自定义指令、Claude Projects 的指令（尽力而为，接口不可用时跳过）。
* Gemini 没有可用的数据接口，扩展会在一个后台标签页里逐个打开对话并读取页面内容（**实验性**，页面改版可能失效，也比较慢）。

## 安装（一次）

1. 启动本机接收服务（二选一）：
   ```bash
   ctxport serve-ingest                       # 前台运行
   ctxport serve-ingest --autostart install   # 开机自动运行（macOS launchd / Linux systemd --user / Windows 启动项）
   ```
2. Edge 打开 `edge://extensions`（Chrome 为 `chrome://extensions`）→ 打开“开发人员模式” → “加载解压缩的扩展” → 选择本仓库的 `extension/` 文件夹。
3. 点工具栏上的 ctxport 图标，填入配对码完成配对：`serve-ingest` 首次启动时会打印配对码，也可以随时执行 `ctxport serve-ingest --pair` 获取新的配对码（10 分钟内有效，只能用一次）。配对后只有这个扩展能写入，其他扩展、网页或本机程序都无法写入。
   First time only: enter the pairing code printed by `ctxport serve-ingest --pair` in the popup.
4. 点 **一键同步全部**。

之后默认 **打开这些网站时自动增量同步**（最多每 6 小时一次）；也可以设置后台定时同步。

## 隐私与条款

扩展调用的是这些网站前端自己使用的接口，不是公开 API，网站改版时可能需要更新；请求之间有间隔并在 429 时退避。请只同步你自己的账号数据，并遵守各平台条款。
`ctxport serve-ingest --reset-pairing` 可解除配对（并打印新的配对码）；`--autostart remove` 取消开机自启。

## 开发

```bash
node extension/test/core.test.js   # 同步逻辑单元测试（模拟接口，无需浏览器）
```
