"""Browser Runtime MCP Tools 的严格 JSON Schema（供 tools/list）。"""

from __future__ import annotations

from typing import Any

PLATFORM_ENUM = ["taobao", "pdd", "jingdong"]

TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    # ── Session ──────────────────────────────────────────────
    "session_open": {
        "description": (
            "打开或复用浏览器会话；按 platform 挂载 "
            "~/.browser-runtime/{platform}/chrome_profile。"
            "没有登录 Cookie 时返回 code=NOT_LOGGED_IN，并给出 next_tool：先打开平台首页，再 wait_for_user(reason=login)。"
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["platform"],
            "properties": {
                "platform": {
                    "type": "string",
                    "enum": PLATFORM_ENUM,
                    "description": "平台 profile 分区名：taobao / pdd / jingdong；目录 ~/.browser-runtime/{platform}/chrome_profile",
                },
                "headless": {
                    "type": "boolean",
                    "default": False,
                    "description": "无头模式（多数站点易被拦，默认有头）",
                },
                "reuse": {
                    "type": "boolean",
                    "default": True,
                    "description": "同 platform 已有会话则复用",
                },
            },
        },
    },
    "session_status": {
        "description": "查询会话状态：当前 URL、登录态启发式、是否在监听 Network。",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["session_id"],
            "properties": {
                "session_id": {"type": "string", "minLength": 1},
            },
        },
    },
    # ── Control ──────────────────────────────────────────────
    "browser_navigate": {
        "description": "导航到 URL。未登录时只允许平台首页和登录页，其它 URL 返回 code=NOT_LOGGED_IN；跳到登录页则返回 code=NEEDS_LOGIN。",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["session_id", "url"],
            "properties": {
                "session_id": {"type": "string", "minLength": 1},
                "url": {"type": "string", "minLength": 1},
                "wait_until": {
                    "type": "string",
                    "enum": ["domcontentloaded", "load", "networkidle", "commit"],
                    "default": "domcontentloaded",
                },
                "tab_id": {"type": ["string", "null"], "default": None},
            },
        },
    },
    "browser_interact": {
        "description": (
            "统一交互：click/type/press/select/upload/hover/clear。"
            "优先使用 page_snapshot 返回的 ref。"
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["session_id", "action"],
            "properties": {
                "session_id": {"type": "string", "minLength": 1},
                "action": {
                    "type": "string",
                    "enum": ["click", "type", "press", "select", "upload", "hover", "clear"],
                },
                "ref": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "snapshot 节点 ref，如 e12",
                },
                "selector": {"type": ["string", "null"], "default": None},
                "text": {"type": ["string", "null"], "default": None},
                "key": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "press 时如 Enter",
                },
                "value": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "select 选项值",
                },
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                    "description": "upload 本地路径列表",
                },
                "submit": {
                    "type": "boolean",
                    "default": False,
                    "description": "type 后是否按 Enter",
                },
            },
        },
    },
    "browser_scroll": {
        "description": "滚动页面或滚动到某 ref。",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["session_id"],
            "properties": {
                "session_id": {"type": "string", "minLength": 1},
                "direction": {
                    "type": "string",
                    "enum": ["down", "up", "top", "bottom", "to_ref"],
                    "default": "down",
                },
                "amount": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10000,
                    "default": 800,
                },
                "ref": {"type": ["string", "null"], "default": None},
            },
        },
    },
    "browser_wait": {
        "description": "等待超时、选择器、URL 或网络安静。",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["session_id", "wait_for"],
            "properties": {
                "session_id": {"type": "string", "minLength": 1},
                "wait_for": {
                    "type": "string",
                    "enum": ["timeout", "selector", "url", "network_idle", "network_quiet"],
                    "description": "等待条件",
                },
                "value": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "timeout 用 2s；selector/url 用表达式；network_quiet 可填静默毫秒",
                },
                "timeout_ms": {
                    "type": "integer",
                    "minimum": 100,
                    "maximum": 180000,
                    "default": 30000,
                },
            },
        },
    },
    "wait_for_user": {
        "description": (
            "人机协作：在浏览器页面顶部显示提示条，阻塞等待用户处理登录 / 滑块验证 / 操作确认。"
            "其它工具返回 needs_user_action 时调用。"
            "login 要等整段登录结束（滑块、短信验证码仍算未登录，不会刚发验证码就返回）；"
            "登录成功后会刷新当前页（仍停在登录 URL 则打开平台首页），避免淘宝 SPA 仍显示「请登录」。"
            "captcha 在滑块消失后自动返回；confirm/other 等用户点「继续」或「取消」。"
            "outcome：condition_met / user_continue / "
            "user_cancel（ok=false，停止操作）/ timeout（ok=false，still_pending=true，可再次调用）。"
            "下单、发布、删除、发消息等不可逆操作前必须先 wait_for_user(reason='confirm')。"
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["session_id", "reason"],
            "properties": {
                "session_id": {"type": "string", "minLength": 1},
                "reason": {
                    "type": "string",
                    "enum": ["login", "captcha", "confirm", "other"],
                },
                "message": {
                    "type": ["string", "null"],
                    "default": None,
                    "maxLength": 200,
                    "description": "提示条上给用户看的话，如「即将提交订单：xxx，¥199，请确认」",
                },
                "until": {
                    "type": ["object", "null"],
                    "additionalProperties": False,
                    "default": None,
                    "description": "自定义完成条件（全部满足才算完成）；不填则按 reason 自动检测",
                    "properties": {
                        "selector_gone": {"type": "string", "minLength": 1},
                        "selector_present": {"type": "string", "minLength": 1},
                        "url_contains": {"type": "string", "minLength": 1},
                        "url_not_contains": {"type": "string", "minLength": 1},
                    },
                },
                "timeout_ms": {
                    "type": "integer",
                    "minimum": 5000,
                    "maximum": 1800000,
                    "default": 180000,
                },
                "poll_ms": {
                    "type": "integer",
                    "minimum": 200,
                    "maximum": 10000,
                    "default": 1000,
                },
            },
        },
    },
    "browser_tabs": {
        "description": "标签页 list/new/switch/close。",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["session_id", "action"],
            "properties": {
                "session_id": {"type": "string", "minLength": 1},
                "action": {
                    "type": "string",
                    "enum": ["list", "new", "switch", "close"],
                },
                "tab_id": {"type": ["string", "null"], "default": None},
                "url": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "new 时可附带打开",
                },
            },
        },
    },
    # ── Observe ──────────────────────────────────────────────
    "page_snapshot": {
        "description": "结构化页面树（带 ref），供 browser_interact 使用；默认只要可交互节点。",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["session_id"],
            "properties": {
                "session_id": {"type": "string", "minLength": 1},
                "mode": {
                    "type": "string",
                    "enum": ["interactive", "full", "article"],
                    "default": "interactive",
                },
                "max_nodes": {
                    "type": "integer",
                    "minimum": 10,
                    "maximum": 1000,
                    "default": 200,
                },
                "interesting_only": {"type": "boolean", "default": True},
            },
        },
    },
    "page_screenshot": {
        "description": "截图；登录墙时优先给用户看。返回本地路径。",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["session_id"],
            "properties": {
                "session_id": {"type": "string", "minLength": 1},
                "full_page": {"type": "boolean", "default": False},
                "ref": {"type": ["string", "null"], "default": None},
            },
        },
    },
    "page_query": {
        "description": "按 ref/selector/role/text 查询元素文本或属性。",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["session_id", "by", "query"],
            "properties": {
                "session_id": {"type": "string", "minLength": 1},
                "by": {"type": "string", "enum": ["ref", "selector", "role", "text"]},
                "query": {"type": "string", "minLength": 1},
                "fields": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["text", "html", "attrs", "box", "count"],
                    },
                    "default": ["text", "count"],
                },
            },
        },
    },
    "page_evaluate": {
        "description": "在页面执行轻量 JS；禁止把 token/Cookie 明文返回给模型。",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["session_id", "expression"],
            "properties": {
                "session_id": {"type": "string", "minLength": 1},
                "expression": {"type": "string", "minLength": 1, "maxLength": 8000},
                "return_json": {"type": "boolean", "default": True},
            },
        },
    },
    "devtools_console": {
        "description": "读取近期 console 消息。",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["session_id"],
            "properties": {
                "session_id": {"type": "string", "minLength": 1},
                "level": {
                    "type": "string",
                    "enum": ["error", "warn", "info", "all"],
                    "default": "error",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "default": 50,
                },
                "since_ms": {"type": ["integer", "null"], "default": None},
            },
        },
    },
    # ── Network ──────────────────────────────────────────────
    "network_watch": {
        "description": "开始/停止 Network 监听；可用 action_tag 把请求归因到某次操作。",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["session_id", "action"],
            "properties": {
                "session_id": {"type": "string", "minLength": 1},
                "action": {"type": "string", "enum": ["start", "stop", "status"]},
                "filter": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "resource_types": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": ["fetch", "xhr", "document", "websocket", "other"],
                            },
                            "default": ["fetch", "xhr"],
                        },
                        "url_includes": {
                            "type": "array",
                            "items": {"type": "string"},
                            "default": [],
                        },
                        "url_excludes": {
                            "type": "array",
                            "items": {"type": "string"},
                            "default": ["analytics", "tracking", "wxgamesdk", "sentry"],
                        },
                    },
                },
                "action_tag": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "如 before_search；list 时可 since=action_tag:before_search",
                },
            },
        },
    },
    "network_list": {
        "description": "列出已捕获请求摘要（不含 body）。",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["session_id"],
            "properties": {
                "session_id": {"type": "string", "minLength": 1},
                "since": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "watch_start | action_tag:xxx | request_id:nr_010",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "default": 30,
                },
                "only_json": {"type": "boolean", "default": True},
            },
        },
    },
    "network_search": {
        "description": "按 method/url 关键字/status 过滤已捕获请求。",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["session_id"],
            "properties": {
                "session_id": {"type": "string", "minLength": 1},
                "q": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "匹配 URL 子串",
                },
                "method": {
                    "type": ["string", "null"],
                    "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", None],
                },
                "status": {
                    "type": ["integer", "null"],
                    "minimum": 100,
                    "maximum": 599,
                    "default": None,
                },
                "has_json_body": {"type": ["boolean", "null"], "default": None},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 20,
                },
            },
        },
    },
    "network_get": {
        "description": "按 request_id 取完整 request+response；默认 redact 敏感头。",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["session_id", "request_id"],
            "properties": {
                "session_id": {"type": "string", "minLength": 1},
                "request_id": {"type": "string", "minLength": 1},
                "include": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "request_headers",
                            "query",
                            "post_data",
                            "response_headers",
                            "response_body",
                            "timing",
                        ],
                    },
                    "default": ["query", "post_data", "response_body"],
                },
                "body_max_chars": {
                    "type": "integer",
                    "minimum": 1000,
                    "maximum": 500000,
                    "default": 80000,
                },
                "redact_secrets": {"type": "boolean", "default": True},
            },
        },
    },
    "network_clear": {
        "description": "清空已捕获的 Network 缓冲。",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["session_id"],
            "properties": {
                "session_id": {"type": "string", "minLength": 1},
                "before": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "可选：只清该 request_id 之前",
                },
            },
        },
    },
    # ── Memory ───────────────────────────────────────────────
    "session_api_call": {
        "description": (
            "用当前会话登录态对给定 URL 直接发起 HTTP 请求（无需先存档能力）；"
            "自动携带浏览器 Cookie，gzip 自动解压，可拆 JSONP 包裹。"
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["session_id", "url"],
            "properties": {
                "session_id": {"type": "string", "minLength": 1},
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                    "default": "GET",
                },
                "url": {
                    "type": "string",
                    "minLength": 1,
                    "description": "目标 URL，可含或不含 query",
                },
                "query": {
                    "type": "object",
                    "additionalProperties": True,
                    "default": {},
                    "description": "追加/覆盖 query 参数",
                },
                "headers": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "default": {},
                    "description": "额外请求头；User-Agent/Referer 缺省时自动补",
                },
                "body": {
                    "type": ["object", "string", "null"],
                    "default": None,
                    "description": "POST/PUT 体；dict/list 走 json，str 走 raw",
                },
                "jsonp": {
                    "type": "boolean",
                    "default": True,
                    "description": "JSON 解析失败时尝试拆 JSONP 包裹（淘宝/天猫接口常见）",
                },
                "body_max_chars": {
                    "type": "integer",
                    "minimum": 1000,
                    "maximum": 500000,
                    "default": 80000,
                },
                "redact_secrets": {
                    "type": "boolean",
                    "default": True,
                    "description": "脱敏响应头中的敏感字段",
                },
            },
        },
    },
    "page_api_call": {
        "description": (
            "在当前页面上下文里发请求，让站点自己的 JS 算签名。"
            "mode=mtop：调页面的 window.lib.mtop.request，淘宝/天猫 mtop 的 token/t/sign 由页面现算，"
            "改参数（翻页/rateType 等）也能通过校验；需先导航到淘宝/天猫页面。"
            "mode=fetch：用页面的 fetch 发请求（同源 Cookie、站点对 fetch 的拦截器生效；跨域受 CORS 限制）。"
            "无签名的接口用 session_api_call 即可。"
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["session_id", "mode"],
            "properties": {
                "session_id": {"type": "string", "minLength": 1},
                "mode": {"type": "string", "enum": ["mtop", "fetch"]},
                "url": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "fetch 必填（可相对当前页面）；mtop 可传网关 URL 自动解析 api/v",
                },
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                    "default": "GET",
                    "description": "fetch 的 HTTP 方法；mtop 的 type",
                },
                "query": {
                    "type": "object",
                    "additionalProperties": True,
                    "default": {},
                    "description": "fetch：追加/覆盖 query 参数",
                },
                "headers": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "default": {},
                    "description": "fetch：额外请求头",
                },
                "body": {
                    "type": ["object", "array", "string", "null"],
                    "default": None,
                    "description": "fetch：请求体；object/array 按 JSON 发送",
                },
                "api": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "mtop：接口名，如 mtop.taobao.rate.detaillist.get",
                },
                "v": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "mtop：接口版本，如 6.0（缺省 1.0）",
                },
                "data": {
                    "type": ["object", "null"],
                    "default": None,
                    "description": "mtop：业务参数对象（即抓包里 data 字段解开后的 JSON）",
                },
                "need_login": {
                    "type": "boolean",
                    "default": False,
                    "description": "mtop：ecode=1，需要登录态的接口",
                },
                "mtop_options": {
                    "type": ["object", "null"],
                    "additionalProperties": True,
                    "default": None,
                    "description": "mtop：透传给 lib.mtop.request 的其它参数，如 dataType/timeout",
                },
                "mtop_lib_url": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "mtop：页面缺少 window.lib.mtop 时注入的 mtop.js 地址（从 Network 里找）",
                },
                "jsonp": {
                    "type": "boolean",
                    "default": True,
                    "description": "fetch：JSON 解析失败时尝试拆 JSONP 包裹",
                },
                "timeout_ms": {
                    "type": "integer",
                    "minimum": 1000,
                    "maximum": 180000,
                    "default": 30000,
                },
                "body_max_chars": {
                    "type": "integer",
                    "minimum": 1000,
                    "maximum": 500000,
                    "default": 80000,
                },
                "redact_secrets": {
                    "type": "boolean",
                    "default": True,
                    "description": "fetch：脱敏响应头中的敏感字段",
                },
            },
        },
    },
    "memory_save_api": {
        "description": (
            "把已理解的接口固化为临时能力（含输入 schema 与输出 JSONPath）。"
            "source_request_id 指向 mtop 请求时自动用 page_mtop：只存 api/v 与业务 data 模板，"
            "不存 t/sign，重放时由页面现算签名。"
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["session_id", "name", "method", "url_template"],
            "properties": {
                "session_id": {"type": "string", "minLength": 1},
                "name": {"type": "string", "minLength": 1, "maxLength": 64},
                "source_request_id": {"type": ["string", "null"], "default": None},
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                },
                "url_template": {"type": "string", "minLength": 1},
                "headers_template": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "default": {},
                },
                "query_template": {
                    "type": "object",
                    "additionalProperties": True,
                    "default": {},
                },
                "body_template": {
                    "type": ["object", "null"],
                    "default": None,
                    "description": "可用 {{param}} 占位；page_mtop 时即 mtop 的 data 模板",
                },
                "exec_mode": {
                    "type": ["string", "null"],
                    "enum": ["context", "page_fetch", "page_mtop", None],
                    "default": None,
                    "description": (
                        "重放方式：context=服务端直连（带 Cookie、无 CORS、不跑页面 JS）；"
                        "page_fetch=页面内 fetch；page_mtop=页面 lib.mtop 现算签名。缺省自动识别 mtop"
                    ),
                },
                "input_schema": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "default": {},
                    "description": '如 {"keyword":"string","page":"number"}',
                },
                "output_paths": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "default": {},
                    "description": '如 {"items":"$.items","total":"$.total"}',
                },
                "notes": {"type": "string", "default": ""},
            },
        },
    },
    "memory_list": {
        "description": "列出当前已保存的临时 API 能力。",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "session_id": {"type": ["string", "null"], "default": None},
                "platform": {"type": ["string", "null"], "default": None},
            },
        },
    },
    "memory_run_api": {
        "description": (
            "用当前会话登录态重放已保存能力，按能力的 exec_mode 选择 context.request / 页面 fetch / 页面 lib.mtop。"
            "page_* 模式需要当前标签页停在目标站点页面上。"
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["session_id", "capability_id", "params"],
            "properties": {
                "session_id": {"type": "string", "minLength": 1},
                "capability_id": {"type": "string", "minLength": 1},
                "params": {
                    "type": "object",
                    "additionalProperties": True,
                    "default": {},
                },
                "jsonp": {
                    "type": "boolean",
                    "default": True,
                    "description": "JSON 解析失败时尝试拆 JSONP 包裹",
                },
                "timeout_ms": {
                    "type": "integer",
                    "minimum": 1000,
                    "maximum": 180000,
                    "default": 30000,
                    "description": "page_* 模式的页面内调用超时",
                },
                "body_max_chars": {
                    "type": "integer",
                    "minimum": 1000,
                    "maximum": 500000,
                    "default": 80000,
                },
            },
        },
    },
}


def tool_names() -> list[str]:
    return list(TOOL_SCHEMAS.keys())
