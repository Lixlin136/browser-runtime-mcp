# Browser Runtime MCP

给 AI 用的**真浏览器** MCP（Python / Playwright）。Agent 可以打开网页、让你扫码登录、点选、看页面结构、抓 Network，再整理成商品清单和跳转链接 进行agent购物。

克隆本仓库，本机建虚拟环境，用 pip 安装依赖后，把 MCP 接到 Cursor、Claude Code、Codex、WorkBuddy、DSH 等客户端即可。不发布 PyPI 包，也不提供远程托管服务：登录和 Chrome 都必须在你自己电脑上。

当前主场景：购物选品。`session_open.platform` 只能是 `taobao` / `pdd` / `jingdong`，登录态按该值分目录。淘宝链路已实测；京东、拼多多工具通用。

协议：[Apache License 2.0](LICENSE)。允许个人和公司使用、修改、再分发（含商用），需保留版权与许可声明。

> 非淘宝/京东/拼多多官方工具。不自动下单、不进支付。站点条款由使用者自行遵守。价格、券、国补以下单页为准。


实测案例见 [docs/cass.md](docs/cass.md)。

## 能做什么（按实测）

- 淘宝 / 京东 / 拼多多选品：预算、品类、多条件（防水女款、轴体等）一句话搜
- 未登录弹出本机 Chrome，`wait_for_user` 扫码；登录态在  
  `~/.browser-runtime/{platform}/chrome_profile`（`platform` 为 `taobao` / `pdd` / `jingdong`）
- 出清单：标题、价格、店铺、理由、可点的商品详情链接
- 同会话可换平台再搜，做三站到手价对比（一次只开一个 `platform`，登录态不混用）
- 可让 Agent 把方案写成 markdown / html
- **不自动下单、不进支付**；价格、券、国补以下单页为准
- 站点把登录踢掉时，再扫一次码就能继续

购物 SOP 在 `skills/browser-runtime-shopping-skill`。接到客户端后，任务里引用该 skill，再跟自然语言需求。页面和工具结果以当场为准。

## 环境

- Python 3.11+
- 本机已装 **Google Chrome**
- 会用 `venv` 和 `pip`

## 安装

本 MCP 用的是你本机已经装好的 **Google Chrome**（`channel=chrome`），不是 Playwright 自带的 Chromium。日常浏览器用的那个即可，**不必再装一份浏览器**。

仓库根目录（已有 `.venv` 可跳过第一行）：

```bash
python -m venv .venv

# Windows
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# macOS / Linux
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -r requirements.txt
```

Windows 默认路径已装 Chrome 时，上面两步就够了，**不用**跑 `playwright install chrome`。

只有启动时报找不到 chrome channel（Linux、非默认安装路径、没装 Chrome）才补下面之一：

```bash
# 优先：让 Playwright 能找到系统 Chrome
python -m playwright install chrome

# 仍不行：再装系统 Chrome 到默认路径
# 最后兜底（淘宝风控更容易认出来，不推荐当默认）：
python -m playwright install chromium
```

登录数据和临时能力都在用户目录

```text
~/.browser-runtime/taobao/chrome_profile
~/.browser-runtime/generic/chrome_profile
~/.browser-runtime/_capabilities/
```


## 推荐接入：Streamable HTTP

在仓库根启动服务（把解释器换成你本机 venv 里的 python）：

```bash
# Windows
.\.venv\Scripts\python.exe -m mcp_server --transport streamable-http

# macOS / Linux
.venv/bin/python -m mcp_server --transport streamable-http
```

默认 `http://127.0.0.1:8766/mcp`，可用 `--host` / `--port` / `--path`。仅建议绑本机。

Cursor / Claude Code / Codex / WorkBuddy 等：先起上面的进程，再把 MCP 指到这个地址。

```json
{
  "mcpServers": {
    "browser-runtime": {
      "url": "http://127.0.0.1:8766/mcp"
    }
  }
}
```

把 `skills/browser-runtime-shopping-skill` 配进客户端。对话里引用该 skill 再跟需求，例如：「淘宝搜 200 元以内无线蓝牙耳机，挑评价好的」。

## 可选：stdio

客户端能直接拉起本地进程时可以用。`command` 和 `cwd` 换成你的克隆目录。`cwd` 必须是仓库根，这样 `python -m mcp_server` 才能找到包。

```json
{
  "mcpServers": {
    "browser-runtime": {
      "command": "<仓库>/.venv/Scripts/python.exe",
      "args": ["-m", "mcp_server"],
      "cwd": "<仓库>"
    }
  }
}
```

macOS / Linux 把 `command` 换成 `<仓库>/.venv/bin/python`。

## 使用约定

1. 第一次会弹出 Chrome，在窗口里登录淘宝，不要关那个窗口直到任务结束。
2. 测的时候尽量不要同时用日常 Chrome 登同一个淘宝号，容易互踢。
3. 滑块、验证码、登录弹窗：看浏览器顶上的提示条，人处理完再继续。
4. 价格、券、国补随时变，下单以页面为准。
5. 不要让 Agent 代付、代下单。

## 工具分层

| 层 | 工具 |
|---|---|
| Session | `session_open` `session_status` `wait_for_user` |
| Control | `browser_navigate` `browser_interact` `browser_scroll` `browser_wait` `browser_tabs` |
| Observe | `page_snapshot` `page_screenshot` `page_query` `page_evaluate` `devtools_console` |
| Network | `network_watch` `network_list` `network_search` `network_get` `network_clear` |
| API | `session_api_call` `page_api_call` |
| Memory | `memory_save_api` `memory_list` `memory_run_api` |

`session_open(platform="taobao"|"pdd"|"jingdong")`。登录态在 `~/.browser-runtime/{platform}/chrome_profile`。

## 目录

```text
mcp_server/     Python 包（模块名 mcp_server，启动：python -m mcp_server）
skills/         browser-runtime-shopping-skill
docs/cass.md    WorkBuddy 实测案例（问题 + 截图）
docs/assets/    案例截图
docs/产物文档/  当时导出的完整 md/html
```

## 数据安全与边界

核心原则是*能自动化的全交给 Agent（开页、读现场、抓包、出清单和链接），登录、滑块、确认这几道闸原样交还给人*。MCP、Chrome、登录态都在自己电脑上，没有远程托管，也不会把账号交到别人的服务器。

- 本机登录态 + 开源自用。扫码登录、验证码、滑块都在本机弹出的浏览器里由你自己完成。登录态复用，大部分情况下只需要登录一次即可
- Cookie / 登录态写在本机 `~/.browser-runtime/{platform}/chrome_profile`不回传 Cookie 明文。
- 对话里的商品清单、截图、导出的 md/html 也是你本机 Agent 的产物。

平台有反爬和反自动化。不要短时间高频反复搜、狂翻页、多开并行任务。正常选品一般没大问题；用得太猛可能触发滑块、踢登录，极端情况会风控账号。这是站点策略，不是本工具能关掉的。

本项目只帮你在各购物站**选和比**，给出清单、链接和估算。加购、下单、付款必须你自己在官方页面完成。价格、券、国补以下单页为准。站点使用条款由使用者自行遵守。

## 一键安装（给 Cursor / Claude Code / Codex / WorkBuddy / DSH）

本仓库用 **Streamable HTTP**（也可以用stdio 可让ai改为stdio 不需要手动起个http服务 ）。用户把本 README 或仓库地址丢给 Agent，说一句「帮我把 browser-runtime-mcp 装好」即可。克隆下来就知道项目在哪个目录；装环境、起 HTTP、写 MCP JSON、拷 skill，全由 Agent 做。用户不用手改配置。

对 Agent：按顺序执行，路径用你刚才 clone 出来的真实目录，不要编盘符。

1. **克隆**到用户本机一个固定目录（已有则 `git pull`，不要再 clone 一份）。
2. **环境**：本机已有 Python 3.11+ 和 Google Chrome。在仓库根：

```bash
python -m venv .venv
# Windows: .\.venv\Scripts\python.exe
# macOS / Linux: .venv/bin/python
<venv的python> -m pip install -U pip
<venv的python> -m pip install -r requirements.txt
```

3. **自检再起服务**（后台，不要关掉这个进程）。先验证 SDK 能 import，失败就停，不要写 mcp.json：

```bash
<venv的python> -c "import mcp_server.server"
<venv的python> -m mcp_server --transport streamable-http
```

默认 `http://127.0.0.1:8766/mcp`。8766 已被本服务占用就跳过，不要再起一份。`import mcp_server.server` 
4. **写 MCP 配置**：合并进当前产品自己的 MCP 配置文件（Cursor 一般是 `~/.cursor/mcp.json` 或项目 `.cursor/mcp.json`；其它产品写它的连接器 / mcp 配置处）。只加这一段，不要把用户别的 MCP 覆盖掉：

```json
{
  "mcpServers": {
    "browser-runtime": {
      "url": "http://127.0.0.1:8766/mcp"
    }
  }
}
```

5. **装 skill**：把仓库里的 `skills/browser-runtime-shopping-skill` 整个目录拷到当前产品的 skills 目录（Cursor / Claude Code / Codex / WorkBuddy / DSH 各用各的 skills 位置，按该产品习惯放）。
6. 必要时提醒用户重载 MCP。装完用一句话需求冒烟，例如：「淘宝搜 200 元以内无线蓝牙耳机，挑评价好的」。

以后开机服务不会自己起来。用户跟 Agent 说「先把 browser-runtime 的 MCP 服务启动起来」，Agent 进**已克隆的那个仓库根**，用该目录 `.venv` 再跑第 3 步即可。用户也可以自己在仓库根手动起。
