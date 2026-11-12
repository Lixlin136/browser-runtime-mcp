---
name: browser-runtime-shopping-skill
description: 
淘宝/京东/拼多多 购物 skill。结合 MCP **`browser-runtime`** 工作，目标帮助用户在单个电商平台如淘宝、京东、拼多多内完成选品、比价、优惠拆解、计算最优购买方案。
比如当用户要逛淘宝/拼多多/京东、按品类搜商品、抽取商品清单、或点进详情页拿多维数据（参数 / SKU 全价表 / 用户评价含差评 / 问大家 / 券后到手价）供下单决策时使用，是面向购物Agent链路，人机交互完成购物，最终由用户自行登录考虑是否下单
---


# 用户典型使用场景

用户一句话下需求，在**某平台**（一次只开一个 `platform`）完成选品，不要跨店自动下单。常见问法：

- 预算/品类：「某平台搜 200 元以内无线蓝牙耳机，挑评价好的」
- 多条件：「某平台登山冲锋衣，500–800，防水、女款，优先官方旗舰」
- 详情决策：某平台点进某款拿 SKU 全价表、参数、评价/问答采样、券后估算
- 边界：「再对比另一家电商」「直接帮我下单」→ 说明当前只做这一平台、不进支付

**凡是商品相关的输出（清单、对比、推荐、SKU 表），每一件都必须带当前站可点的详情页链接**。方便立刻点开核对。搜索卡若是广告跳转，不要把广告 URL 当商品链接。



# browser-runtime mcp 工具调用说明书
实测里容易翻的点（工具能过、交付不能糊）：

- **登录会中途失效**：大促/风控可能静默踢登录、不一定出验证码。一出现登录墙或「登录后查看」就 `wait_for_user(login)`，不要假装还能取数。
- **同一会话不要并行多个购物任务**：多标签乱跳会导致页面漂移、抽到错页。单次只做一件购物任务。
- **价格要写口径**：列表起售价、详情 SKU、国补公式、券后估算分开写；标注「以下单页为准」。不要把估算说成最终应付。
- **店铺类型看当场页面**：店名带「旗舰」不算数；以当前 URL / 页面证据为准。筛选 tab 可能限流，限流了就换办法，不要空转。
- **不代下单**：只给清单和链接；不可逆操作才 `wait_for_user(confirm)`，用户取消则停。

## 铁律（先读这段）

- **先登录再干活。** 业务工具会在代码里检查该平台登录 Cookie（只判断名字在不在，不读、不返回内容）。`session_open` 或后续工具返回 `code=NOT_LOGGED_IN` 时，严格按结果里的 `next_tool` / `next_args` 做：通常是先 `browser_navigate` 打开平台首页，再 `wait_for_user(reason="login")`。`login_ok` 为真之前不要改调别的工具硬闯。禁止自己读 Cookie。
- **`auth_status` 不能当已登录。** `logged_in_or_public` 同时覆盖「已登录」和「未登录但页面公开可访问」。是否允许继续干活看 `login_ok` 和 `code=NOT_LOGGED_IN`。页面上再出现登录墙、`NEEDS_LOGIN`、「登录后查看」或 `needs_user_action=login`，同样先 `wait_for_user(login)`，不要继续取数。
- **同平台复用。** `session_open(platform=..., reuse=true)`（`platform` 必填，`reuse` 默认 true）。同一 `platform` 共用一份 Chrome profile，目录名等于参数值：

  | 当前站 | `platform` | 登录态目录 |
  |---|---|---|
  | 淘宝/天猫 | `taobao` | `~/.browser-runtime/taobao/chrome_profile` |
  | 拼多多 | `pdd` | `~/.browser-runtime/pdd/chrome_profile` |
  | 京东 | `jingdong` | `~/.browser-runtime/jingdong/chrome_profile` |

  不要用别的字符串。三站登录态互不混用。目录在只说明这个分区曾经登过，不保证当前仍登录。
- **人机节点只走 `wait_for_user`。** 登录、滑块、不可逆确认（下单 / 发布 / 删除 / 发消息）都走它。其它工具返回 `needs_user_action` 时调它，不要口头让人「自己去登」却不调工具，不要自己点验证码。`login` / `captcha` 在检测到的阻塞消失后自动返回；`confirm` / `other` 等点「继续」或「取消」。
- **同 session 串行 `browser_navigate`。** 并行会互相打断，只有最后一条跑完，其余 `ERR_ABORTED`，结果不可信。
- **不能关最后一个标签页。** `browser_tabs(action=close)` 在只剩一页时返回 `LAST_TAB`；要换页就 `browser_navigate` 复用它。
- **刹车。** 同一目标连续 
2 次没拿到预期结果 → 停下 dump 一个样本（一次 evaluate 探针 / 一张 screenshot / 一条 `network_get`）看清楚，不要继续换写法。连续 3 次无新增信息 → 换路径（Observe → 找接口）或降级交付，明确向用户说明暂时无法完成。
- **Memory 不要为了用而用。** 只在当场已经抓到、并能改参重放的稳定 JSON 接口时才 `memory_save_api`。列表是前端渲染 / 广告跳转、`network_list` 没有业务 JSON，就停在 Observe。
- **商品必带详情链接。** 推荐、对比、清单里每一件都给平台详情 URL，广告链还原为 `id=`。没有链接的商品行不算交付完成。
- **不碰 Cookie / Token 明文。** 不读、不输出、不拼。带登录的请求用 `session_api_call` / `page_api_call` / `memory_run_api`。
- **现场覆盖先验。** 工具返回、当前 DOM、当前 Network 大于任何记忆。层是分类，不是流水线。

登录是否放行看工具返回的 `login_ok`，不要用 profile 目录非空或 `auth_status=logged_in_or_public` 判断。不要解析 Cookie 库。打开后若出现登录墙，仍按页面判据调用 `wait_for_user(login)`。

---

Session 解决「谁在登」；Control 改状态；Observe 看页；Network 看包；API 主动打；Memory 可选固化；过不了的交给 `wait_for_user`。不重叠的工具看名字即可。说明书重点是**重叠区怎么分**。

### 重叠：都像「读页面」

`screenshot` 出图，`snapshot` 出树，名字只差一个词。

| 此刻 | 用 | 不要用 |
|---|---|---|
| 要点东西，需要 `ref` | `page_snapshot` | screenshot（没 `ref`） |
| 已知 `ref` / selector / role / text，取这一处 | `page_query` | 再 snapshot 一遍整页 |
| 未知结构、要批量、要读全局对象 / 内嵌 JSON | `page_evaluate` | 用 query 扫全页 |
| 给人看长什么样 | `page_screenshot` | 当取数 |

### 重叠：都像「判登录」

| 信号 | 含义 |
|---|---|
| `auth_status=logged_in_or_public` | **不能**当已登录；公开页未登录也是这个值 |
| `auth_status=needs_login` / `code=NEEDS_LOGIN` | URL 像登录页（含 `login`/`signin`/`passport` 等），先 `wait_for_user(login)` |
| `needs_user_action=login` / 站点登录墙文案 /「登录后查看」 | 未登录，即使 URL 不像登录页 |
| `needs_user_action=captcha` / `NEEDS_CAPTCHA` | 滑块，`wait_for_user(captcha)` |

### 重叠：都像「发请求」

| 工具 | 执行位置 | 用在 |
|---|---|---|
| `session_api_call` | Playwright `context.request`：带会话 Cookie，无 CORS，**不跑页面 JS** | 无页面签名的接口 |
| `page_api_call(mode=mtop)` | 页面 `window.lib.mtop.request`，`t`/`sign` 由页面现算 | 淘宝/天猫 mtop；改参翻页也走页面现签 |
| `page_api_call(mode=fetch)` | 页面 `fetch` | 同源、要走站点对 fetch 的拦截器；跨域仍受 CORS |

带页面现算签名的接口（如 mtop 的 `t`/`sign`）不能用 `session_api_call` 手搓重放。

---

## 0. 你此刻要做什么 → 调哪个工具

读页面 / 判登录 / 发请求以重叠区为准。字段名、分页参数只从当场 evaluate / `network_get` 抄。

| 你此刻要做什么 | 工具 | 一句话 |
|---|---|---|
| 开浏览器，并按平台复用本机登录态 | `session_open(platform=..., reuse=true)` | `platform` 必填：`taobao` / `pdd` / `jingdong`；目录名等于该值 |
| 核对当前 URL、有哪些 tab、Network 是否在听 | `session_status` | 看这些字段即可；`auth_status` 见重叠区，不能当已登录 |
| 工具刚返回 `needs_user_action`（login/captcha），或要等人确认不可逆操作 | `wait_for_user` | `login`/`captcha` 阻塞消失后自动返回；`confirm` 等点按钮 |
| 打开某个 URL | `browser_navigate` | **串行**；跳登录页才给 `NEEDS_LOGIN`；没有直达 URL 就 snapshot 拿 `ref` 再 interact |
| 点、输入、按键、选、上传、悬停、清空 | `browser_interact` | 优先用 `page_snapshot` 给的 `ref` |
| 滚页面，或滚到某个 `ref` | `browser_scroll` | 懒加载列表用这个触发，不是取数工具 |
| 等超时 / 选择器 / URL / 网络安静 | `browser_wait` | `network_quiet` 可能永不静（心跳站），超时后继续 Observe |
| 列表/新建/切换/关掉标签 | `browser_tabs` | 多页时用；只剩一页 `close` 失败（`LAST_TAB`），换页 `navigate` 复用 |
| 读当前页 | 见重叠区「读页面」 | `snapshot` 出树（默认 `interactive`；`article` 正文；`full` 贵）；`screenshot` 出图 |
| 看页面自己打的 console 报错 | `devtools_console` | 页面 JS 挂了再查，不是主取数路径 |
| 开始把后续请求标到某次操作上 | `network_watch(action=start, action_tag=...)` | 先 watch 再交互，list 才能 `since=action_tag:...` |
| 看刚才捕获了哪些请求（摘要、无 body） | `network_list` | 默认 `only_json=true`；空列表只说明这次没发出 JSON 请求 |
| 在已捕获里按 URL/method/status 过滤 | `network_search` | 请求多了再筛，不要每条 `network_get` |
| 打开某一条的 query / post / body | `network_get(request_id)` | 理解接口用；敏感头默认 redact |
| 丢掉旧缓冲，避免和下一次操作混在一起 | `network_clear` | 可选；不是每次必清 |
| 带登录主动发请求 | 见重叠区「发请求」 | 无签 `session_api_call`；有签 `page_api_call`（mtop / fetch） |
| 固化已验证、可改参的稳定 JSON 接口 | `memory_save_api` / `memory_list` / `memory_run_api` | 没有稳定业务 JSON 就不要用；mtop 源自动 `page_mtop`，不存 `t`/`sign` |

---

## 决策梯（按需停，不强制走完）

1. **列表够用** → `page_evaluate` 探源（有内嵌 JSON / 全局对象就用，没有再抽 DOM）；每条只要 id + 展示字段；去广告、过滤放数据层，不赌 URL 参数。
2. **详情** → 同样探源；关键字段缺失或要更全，才进下一步。
3. **要底包 / 全量** → `network_watch` → 触发动作 → `network_list` → `network_get`；无签 `session_api_call`，有签 `page_api_call`（判定见重叠区）。没有稳定 JSON 就停，不要 Memory。
4. **深分页** → 同一路径改参重放，间隔 1–2s；参数名与枚举以真实请求为准。
5. **登录墙 / 滑块** → `wait_for_user(login|captcha)`，用页面判据复核，重跑。

## 其他注意事项：
拼多多平台用手机版https://mobile.pinduoduo.com/。淘宝京东都有网页版
