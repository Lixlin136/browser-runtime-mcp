"""Playwright 会话运行时：Control / Observe / Network。"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    Response,
    async_playwright,
)

from mcp_server import memory_store

HARNESS_ROOT = Path(__file__).resolve().parent.parent
LOGIN_DATA_ROOT = Path.home() / ".browser-runtime"
OUT_DIR = HARNESS_ROOT / "out"
SCREENSHOT_DIR = OUT_DIR / "screenshots"

SECRET_HEADER_KEYS = {
    "authorization",
    "cookie",
    "set-cookie",
    "x-csrf-token",
    "x-xsrf-token",
    "x-auth-token",
}

LOGIN_URL_HINTS = ("signin", "login", "passport", "sso", "accounts/login", "havana")
# 只放淘宝风控页的确定特征；泛化的 "captcha" 会误伤普通 URL，交给 DOM 检测
CAPTCHA_URL_HINTS = ("/punish", "x5secdata", "_____tmd_____")
# login wait：滑块/短信也算未完成，连续确认次数要比 captcha 更严
LOGIN_WAIT_STREAK = 4
CAPTCHA_WAIT_STREAK = 2
LOGIN_REFRESH_SETTLE_MS = 500
PLATFORM_HOME_URLS = {
    "taobao": "https://www.taobao.com/",
    "pdd": "https://www.pinduoduo.com/",
    "jingdong": "https://www.jd.com/",
}
# 未登录时允许打开的入口；拼多多购物走移动站
PLATFORM_LOGIN_START_URLS = {
    "taobao": "https://www.taobao.com/",
    "pdd": "https://mobile.pinduoduo.com/",
    "jingdong": "https://www.jd.com/",
}
# 只认 Cookie 名是否存在，不读取、不回传值。京东要两个都在。
AUTH_COOKIES: dict[str, dict[str, tuple[str, ...]]] = {
    "taobao": {"any": ("cookie2", "unb")},
    "jingdong": {"all": ("pt_key", "pt_pin")},
    "pdd": {"any": ("PDDAccessToken",)},
}
AUTH_COOKIE_DOMAINS = {
    "taobao": ("taobao.com", "tmall.com"),
    "jingdong": ("jd.com",),
    "pdd": ("pinduoduo.com", "yangkeduo.com"),
}
# 未登录时只放行首页（path 为 /）和登录域，拦住搜索页、商品页
HOME_HOSTS = {
    "taobao": frozenset({"www.taobao.com", "www.tmall.com"}),
    "jingdong": frozenset({"www.jd.com"}),
    "pdd": frozenset({"www.pinduoduo.com", "mobile.pinduoduo.com", "mobile.yangkeduo.com"}),
}
LOGIN_HOSTS = {
    "taobao": frozenset({"login.taobao.com", "login.tmall.com", "passport.taobao.com"}),
    "jingdong": frozenset({"passport.jd.com", "plogin.m.jd.com"}),
    "pdd": frozenset({"login.pinduoduo.com"}),
}

# 需要人处理的页面状态（按顺序匹配，先具体后泛化）；只认可见元素。
USER_ACTION_RULES: list[dict[str, Any]] = [
    {
        "kind": "captcha",
        "selectors": [
            "#nc_1_wrapper", ".nc_wrapper", ".nc-container",  # 阿里滑块
            "#baxia-dialog-content", ".baxia-dialog",  # 淘宝 baxia 风控弹窗
            "iframe[src*='punish']", "iframe[src*='captcha']",
            ".geetest_panel", ".geetest_box",  # 极验
            ".yidun_popup", "#tcaptcha_iframe", "#tcaptcha_transform",  # 网易易盾 / 腾讯
        ],
    },
    {
        "kind": "login",
        "selectors": [
            "iframe[src*='login.taobao.com']", "iframe[src*='login.tmall.com']",
            "iframe[src*='passport']",
            "[class*='login-modal' i]", "[class*='loginModal']", "[class*='login-container' i]",
        ],
    },
    {
        "kind": "captcha",
        "selectors": ["[class*='captcha' i]:not(input)", "[id*='captcha' i]:not(input)"],
    },
    {
        "kind": "login",
        "selectors": [
            "input[type='password']",
            "input[autocomplete='one-time-code']",
            "input[placeholder*='验证码']",
            "input[placeholder*='短信']",
            "input[name*='sms' i]",
            "input[id*='sms' i]",
        ],
    },
]

_DETECT_USER_ACTION_JS = """({ rules }) => {
  const visible = (el) => {
    const s = window.getComputedStyle(el);
    if (!s || s.display === 'none' || s.visibility === 'hidden' || Number(s.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 4 && r.height > 4;
  };
  for (const rule of rules) {
    for (const sel of rule.selectors) {
      let els;
      try { els = document.querySelectorAll(sel); } catch (e) { continue; }
      for (const el of els) {
        if (el.closest('#__br_wait_host')) continue;
        if (visible(el)) return { kind: rule.kind, evidence: sel };
      }
    }
  }
  return null;
}"""

_SELECTOR_VISIBLE_JS = """(sel) => {
  let els;
  try { els = document.querySelectorAll(sel); } catch (e) { return false; }
  for (const el of els) {
    const s = window.getComputedStyle(el);
    if (!s || s.display === 'none' || s.visibility === 'hidden') continue;
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) return true;
  }
  return false;
}"""

# 浏览器内提示条：shadow DOM 隔离样式，不进入 page_snapshot 的 querySelectorAll。
_WAIT_BANNER_JS = """({ id, message, continueLabel, cancelLabel }) => {
  const existing = document.getElementById('__br_wait_host');
  if (window.__brWait && window.__brWait.id === id && existing) return 'exists';
  if (existing) existing.remove();
  window.__brWait = { id, decision: null };
  const host = document.createElement('div');
  host.id = '__br_wait_host';
  host.style.cssText = 'position:fixed;top:12px;left:50%;transform:translateX(-50%);z-index:2147483647;';
  const root = host.attachShadow({ mode: 'open' });
  root.innerHTML = `
    <style>
      .box{all:initial;display:flex;gap:10px;align-items:center;padding:10px 14px;border-radius:8px;
           background:#1f2937;color:#fff;font:14px/1.4 -apple-system,"Microsoft YaHei",sans-serif;
           box-shadow:0 4px 16px rgba(0,0,0,.3);max-width:680px}
      .msg{flex:1}
      button{all:initial;cursor:pointer;padding:6px 12px;border-radius:6px;font:13px "Microsoft YaHei",sans-serif}
      .go{background:#10b981;color:#fff}.no{background:#4b5563;color:#fff}
    </style>
    <div class="box"><span class="msg"></span>
      <button class="go" data-d="continue"></button><button class="no" data-d="cancel"></button></div>`;
  root.querySelector('.msg').textContent = message;
  root.querySelector('.go').textContent = continueLabel;
  root.querySelector('.no').textContent = cancelLabel;
  root.querySelectorAll('button').forEach((b) => b.addEventListener('click', () => {
    window.__brWait.decision = b.dataset.d;
    host.remove();
  }));
  (document.body || document.documentElement).appendChild(host);
  return 'injected';
}"""

_WAIT_POLL_JS = """({ id }) => {
  const w = window.__brWait;
  if (!w || w.id !== id) return { present: false, decision: null };
  return { present: !!document.getElementById('__br_wait_host') || !!w.decision, decision: w.decision };
}"""

_WAIT_CLEANUP_JS = """() => {
  const el = document.getElementById('__br_wait_host');
  if (el) el.remove();
  window.__brWait = null;
}"""

# reason -> (继续按钮, 取消按钮, 默认提示语)
WAIT_REASONS: dict[str, tuple[str, str, str]] = {
    "login": ("我已登录，继续", "取消", "AI 助手需要你在此页面登录（含验证码/滑块），全部完成后再自动继续"),
    "captcha": ("我已完成，继续", "取消", "AI 助手遇到了安全验证，请手动完成，完成后会自动继续"),
    "confirm": ("确认继续", "取消", "AI 助手即将执行操作，请确认"),
    "other": ("我已完成，继续", "取消", "AI 助手需要你在浏览器里处理一下，完成后点继续"),
}

USER_ACTION_HINTS = {
    "login": "需要用户登录：调用 wait_for_user(reason='login') 等用户在浏览器里登录",
    "captcha": "出现滑块/人机验证：调用 wait_for_user(reason='captcha') 等用户手动完成，不要自动破解",
}

_JSONP_RE = re.compile(r"^\s*([A-Za-z_$][\w$]*)\s*\((.*)\)\s*;?\s*$", re.S)

# 淘宝 mtop 网关路径：/h5/{api}/{v}/
_MTOP_PATH_RE = re.compile(r"/h5/(mtop\.[\w.\-]+)/(\d+(?:\.\d+)*)/?", re.I)

EXEC_MODES = ("context", "page_fetch", "page_mtop")

# 在页面上下文发 fetch：同源 Cookie、站点对 window.fetch 的拦截器都会生效；跨域受 CORS 约束。
_PAGE_FETCH_JS = """async ({ url, method, headers, body, bodyIsJson }) => {
  const init = { method, headers: { ...(headers || {}) }, credentials: 'include' };
  if (body !== null && body !== undefined && method !== 'GET' && method !== 'HEAD') {
    if (bodyIsJson) {
      init.body = JSON.stringify(body);
      if (!Object.keys(init.headers).some(k => k.toLowerCase() === 'content-type')) {
        init.headers['Content-Type'] = 'application/json';
      }
    } else {
      init.body = String(body);
    }
  }
  try {
    const resp = await fetch(url, init);
    const text = await resp.text();
    const respHeaders = {};
    resp.headers.forEach((v, k) => { respHeaders[k] = v; });
    return { ok: true, status: resp.status, url: resp.url, headers: respHeaders, text };
  } catch (e) {
    return { ok: false, code: 'PAGE_FETCH_FAILED', error: String((e && e.message) || e) };
  }
}"""

# 调页面已加载的 window.lib.mtop.request：token/t/sign 由站点自己的库现算。
_PAGE_MTOP_JS = """async ({ params, libUrl }) => {
  const pick = () => {
    const m = window.lib && window.lib.mtop;
    return m && typeof m.request === 'function' ? m : null;
  };
  let mtop = pick();
  if (!mtop && libUrl) {
    try {
      await new Promise((resolve, reject) => {
        const s = document.createElement('script');
        s.src = libUrl;
        s.onload = resolve;
        s.onerror = () => reject(new Error('load failed: ' + libUrl));
        document.head.appendChild(s);
      });
    } catch (e) {
      return { ok: false, code: 'MTOP_LIB_LOAD_FAILED', error: String((e && e.message) || e) };
    }
    mtop = pick();
  }
  if (!mtop) return { ok: false, code: 'MTOP_LIB_MISSING' };
  let res;
  try {
    res = await mtop.request(params);
  } catch (e) {
    // lib.mtop 业务失败时 reject 的也是 {api, ret, data, v} 响应对象
    if (!e || typeof e !== 'object' || !('ret' in e)) {
      return { ok: false, code: 'MTOP_ERROR', error: String((e && e.message) || e) };
    }
    res = e;
  }
  try {
    return { ok: true, text: JSON.stringify(res) };
  } catch (e) {
    return { ok: false, code: 'MTOP_ERROR', error: 'response not serializable: ' + String(e) };
  }
}"""


def profile_dir(platform: str) -> Path:
    return LOGIN_DATA_ROOT / platform / "chrome_profile"


def err(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "code": code, "message": message, **extra}


def ok(**extra: Any) -> dict[str, Any]:
    return {"ok": True, **extra}


def _query_value(v: Any) -> str:
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return ""
    return str(v)


def _merge_query(url: str, query: dict[str, Any] | None) -> str:
    """把 query 合并进 URL；同名参数以 query 为准，list 值展开为重复参数。"""
    if not query:
        return url
    parsed = urlparse(url)
    pairs = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k not in query]
    for k, v in query.items():
        if isinstance(v, list):
            pairs.extend((k, _query_value(x)) for x in v)
        else:
            pairs.append((k, _query_value(v)))
    return urlunparse(parsed._replace(query=urlencode(pairs)))


def _body_payload(text: str, data: Any, body_max_chars: int) -> dict[str, Any]:
    """统一响应体返回：JSON 未超限给 body_json；超限给截断文本；非 JSON 给 2000 字预览。"""
    too_long = len(text) > body_max_chars
    if data is not None and not too_long:
        return {"body_json": data, "body_preview": None, "raw_truncated": False}
    if data is not None:
        return {"body_json": None, "body_preview": text[:body_max_chars], "raw_truncated": True}
    return {"body_json": None, "body_preview": text[:2000], "raw_truncated": too_long}


def _mtop_from_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """从抓到的 mtop 请求里拆出 api / v / 业务 data，丢弃 t / sign 等一次性参数。"""
    m = _MTOP_PATH_RE.search(urlparse(item.get("url") or "").path)
    if not m:
        return None
    query = item.get("query") if isinstance(item.get("query"), dict) else {}
    post = item.get("post_data")
    raw_data: Any = query.get("data")
    if raw_data is None:
        if isinstance(post, dict):
            raw_data = post.get("data")
        elif isinstance(post, str):
            form = parse_qs(post, keep_blank_values=True)
            if form.get("data"):
                raw_data = form["data"][0]
    data: dict[str, Any] = {}
    if isinstance(raw_data, dict):
        data = raw_data
    elif isinstance(raw_data, str):
        try:
            parsed = json.loads(raw_data)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            data = parsed
    return {
        "api": m.group(1),
        "v": m.group(2),
        "type": (item.get("method") or "GET").upper(),
        "need_login": str(query.get("ecode", "0")) == "1",
        "data": data,
    }


def _mtop_outcome(result: Any) -> tuple[bool, list[str], str | None]:
    """解析 mtop 响应的 ret 字段，给出是否成功与可执行的提示。"""
    ret_raw = result.get("ret") if isinstance(result, dict) else None
    ret = [str(x) for x in ret_raw] if isinstance(ret_raw, list) else []
    success = any(x.startswith("SUCCESS") for x in ret)
    joined = " ".join(ret).upper()
    hint = None
    if not success:
        if "SESSION_EXPIRED" in joined or "FAIL_SYS_SESSION" in joined:
            hint = "登录态失效：wait_for_user(reason='login') 等用户重新登录后重试"
        elif "RGV587" in joined or "USER_VALIDATE" in joined:
            hint = "触发人机验证/滑块：wait_for_user(reason='captcha') 等用户手动完成后重试"
        elif "TOKEN" in joined:
            hint = "token 过期或为空：lib.mtop 一般会自动刷新 _m_h5_tk，重试一次即可"
        elif "ILLEGAL_ACCESS" in joined or "SIGN" in joined:
            hint = "签名/参数校验失败：核对 api、v、data 是否与页面真实请求一致"
    return success, ret, hint


@dataclass
class NetworkFilter:
    resource_types: list[str] = field(default_factory=lambda: ["fetch", "xhr"])
    url_includes: list[str] = field(default_factory=list)
    url_excludes: list[str] = field(
        default_factory=lambda: ["analytics", "tracking", "wxgamesdk", "sentry"]
    )


@dataclass
class Session:
    session_id: str
    platform: str
    playwright: Playwright
    context: BrowserContext
    headless: bool
    pages: dict[str, Page] = field(default_factory=dict)
    active_tab_id: str = ""
    ref_map: dict[str, str] = field(default_factory=dict)  # ref -> selector
    console: list[dict[str, Any]] = field(default_factory=list)
    watching: bool = False
    network_filter: NetworkFilter = field(default_factory=NetworkFilter)
    action_tag: str | None = None
    watch_started_at: float | None = None
    tag_started_at: dict[str, float] = field(default_factory=dict)
    network_buffer: list[dict[str, Any]] = field(default_factory=list)
    # login_ok：允许做搜索/点选/取数。login_revoked：Cookie 还在但页面或接口已判定失效，须重新 wait_for_user
    login_ok: bool = False
    login_revoked: bool = False
    _req_seq: int = 0
    _last_network_at: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def active_page(self) -> Page:
        if not self.active_tab_id or self.active_tab_id not in self.pages:
            raise RuntimeError("NO_ACTIVE_TAB")
        return self.pages[self.active_tab_id]


class Runtime:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._platform_index: dict[str, str] = {}  # platform -> session_id
        self._lock = asyncio.Lock()

    def _get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    @staticmethod
    def _domain_matches(domain: str, suffixes: tuple[str, ...]) -> bool:
        host = (domain or "").lstrip(".").lower()
        return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)

    async def _has_auth_cookie(self, sess: Session) -> bool:
        """只看关键登录 Cookie 的名字在不在，不返回 Cookie 值。"""
        spec = AUTH_COOKIES.get(sess.platform)
        suffixes = AUTH_COOKIE_DOMAINS.get(sess.platform)
        if not spec or not suffixes:
            return False
        try:
            cookies = await sess.context.cookies()
        except Exception:  # noqa: BLE001
            return False
        names = {
            c.get("name")
            for c in cookies
            if c.get("name") and self._domain_matches(str(c.get("domain") or ""), suffixes)
        }
        required_all = spec.get("all") or ()
        if required_all:
            return all(name in names for name in required_all)
        return any(name in names for name in (spec.get("any") or ()))

    async def _refresh_login_flag(self, sess: Session) -> bool:
        """Cookie 消失，或已被登录墙/会话失效打回时，不允许继续干活。"""
        if not await self._has_auth_cookie(sess) or sess.login_revoked:
            sess.login_ok = False
            return False
        sess.login_ok = True
        return True

    def _revoke_login(self, sess: Session) -> None:
        sess.login_ok = False
        sess.login_revoked = True

    def _confirm_login(self, sess: Session) -> None:
        sess.login_revoked = False
        sess.login_ok = True

    def _is_prelogin_url(self, platform: str, url: str) -> bool:
        """未登录时只允许打开首页或登录页，避免直接进搜索/商品。"""
        raw = (url or "").strip()
        if raw in ("", "about:blank", "chrome://newtab/"):
            return True
        parsed = urlparse(raw)
        if parsed.scheme not in ("http", "https"):
            return False
        host = (parsed.hostname or "").lower()
        path = parsed.path or "/"
        if host in HOME_HOSTS.get(platform, frozenset()) and path in ("", "/"):
            return True
        if host in LOGIN_HOSTS.get(platform, frozenset()):
            return True
        suffixes = AUTH_COOKIE_DOMAINS.get(platform, ())
        on_platform = self._domain_matches(host, suffixes)
        return bool(on_platform and any(hint in raw.lower() for hint in LOGIN_URL_HINTS))

    def _login_block(self, sess: Session, *, page_url: str | None = None) -> dict[str, Any]:
        home = PLATFORM_LOGIN_START_URLS.get(sess.platform) or PLATFORM_LOGIN_START_URLS["taobao"]
        url = (page_url or "").strip()
        on_entry = bool(url) and url not in ("about:blank", "chrome://newtab/") and self._is_prelogin_url(
            sess.platform, url
        )
        if on_entry:
            message = (
                "当前没有可用登录态。请调用 wait_for_user(reason='login')，"
                "等用户在浏览器里登录完成后再搜索、点选、取数或发业务请求。"
            )
            next_tool = "wait_for_user"
            next_args: dict[str, Any] = {"session_id": sess.session_id, "reason": "login"}
        else:
            message = (
                f"当前没有可用登录态。请先 browser_navigate 打开 {home}，"
                "再 wait_for_user(reason='login')。登录完成前不要访问商品页、搜索页或发业务请求。"
            )
            next_tool = "browser_navigate"
            next_args = {"session_id": sess.session_id, "url": home}
        return err(
            "NOT_LOGGED_IN",
            message,
            needs_user_action="login",
            platform=sess.platform,
            login_ok=False,
            next_tool=next_tool,
            next_args=next_args,
        )

    async def _require_login(self, sess: Session) -> dict[str, Any] | None:
        if await self._refresh_login_flag(sess):
            return None
        try:
            page_url = sess.active_page().url
        except Exception:  # noqa: BLE001
            page_url = ""
        return self._login_block(sess, page_url=page_url)

    def _overlay_login_block(self, payload: dict[str, Any], block: dict[str, Any]) -> None:
        payload["ok"] = False
        payload["code"] = "NOT_LOGGED_IN"
        payload["message"] = block["message"]
        payload["needs_user_action"] = "login"
        payload["login_ok"] = False
        payload["next_tool"] = block["next_tool"]
        payload["next_args"] = block["next_args"]
        payload["user_action"] = {"kind": "login", "hint": USER_ACTION_HINTS["login"]}

    @staticmethod
    def _ret_means_logged_out(ret: list[str] | None) -> bool:
        joined = " ".join(ret or []).upper()
        return "SESSION_EXPIRED" in joined or "FAIL_SYS_SESSION" in joined

    async def session_open(
        self,
        platform: str,
        headless: bool = False,
        reuse: bool = True,
    ) -> dict[str, Any]:
        async with self._lock:
            if reuse and platform in self._platform_index:
                sid = self._platform_index[platform]
                sess = self._sessions.get(sid)
                if sess is not None:
                    await self._ensure_login_entry(sess)
                    return await self._status_payload(sess)

            pw = await async_playwright().start()
            pdir = profile_dir(platform)
            pdir.mkdir(parents=True, exist_ok=True)
            try:
                context = await pw.chromium.launch_persistent_context(
                    user_data_dir=str(pdir),
                    channel="chrome",
                    headless=headless,
                    viewport={"width": 1400, "height": 900},
                    locale="zh-CN",
                    args=["--disable-blink-features=AutomationControlled"],
                    ignore_default_args=["--enable-automation"],
                )
            except Exception as e:  # noqa: BLE001
                await pw.stop()
                return err("LAUNCH_FAILED", f"启动浏览器失败: {e}")

            sid = f"s_{uuid.uuid4().hex[:10]}"
            sess = Session(
                session_id=sid,
                platform=platform,
                playwright=pw,
                context=context,
                headless=headless,
            )
            # 绑定已有页或新建
            page = context.pages[0] if context.pages else await context.new_page()
            tab_id = "t1"
            sess.pages[tab_id] = page
            sess.active_tab_id = tab_id
            self._bind_page_listeners(sess, page)

            self._sessions[sid] = sess
            self._platform_index[platform] = sid
            await self._ensure_login_entry(sess)
            return await self._status_payload(sess)

    async def _ensure_login_entry(self, sess: Session) -> None:
        """没有登录态时停在首页或登录页，避免恢复出来的商品页被直接拿去取数。"""
        if await self._has_auth_cookie(sess) and not sess.login_revoked:
            return
        try:
            page = sess.active_page()
        except Exception:  # noqa: BLE001
            return
        url = page.url or ""
        if url not in ("", "about:blank", "chrome://newtab/") and self._is_prelogin_url(sess.platform, url):
            return
        dest = PLATFORM_LOGIN_START_URLS.get(sess.platform) or PLATFORM_LOGIN_START_URLS["taobao"]
        try:
            await page.goto(dest, wait_until="domcontentloaded", timeout=60_000)
        except Exception:  # noqa: BLE001
            return

    def _bind_page_listeners(self, sess: Session, page: Page) -> None:
        def on_console(msg: Any) -> None:
            sess.console.append(
                {
                    "level": msg.type,
                    "text": msg.text,
                    "url": page.url,
                    "ts": time.time(),
                }
            )
            if len(sess.console) > 500:
                sess.console = sess.console[-300:]

        async def on_response(resp: Response) -> None:
            if not sess.watching:
                return
            await self._capture_response(sess, resp)

        page.on("console", on_console)
        page.on("response", lambda r: asyncio.create_task(on_response(r)))

    async def _capture_response(self, sess: Session, resp: Response) -> None:
        try:
            req = resp.request
            rtype = req.resource_type
            # playwright: fetch/xhr/document/...
            mapped = rtype if rtype in ("fetch", "xhr", "document", "websocket") else "other"
            filt = sess.network_filter
            if filt.resource_types and mapped not in filt.resource_types:
                return
            url = resp.url
            low = url.lower()
            if any(x.lower() in low for x in filt.url_excludes):
                return
            if filt.url_includes and not any(x.lower() in low for x in filt.url_includes):
                return

            sess._req_seq += 1
            rid = f"nr_{sess._req_seq:04d}"
            mime = (resp.headers.get("content-type") or "").split(";")[0].strip()
            post_data = req.post_data
            body_text: str | None = None
            body_json: Any = None
            is_json = "json" in mime or "json" in (resp.headers.get("content-type") or "").lower()
            try:
                body_text = await resp.text()
                if body_text and (is_json or body_text[:1] in "{["):
                    try:
                        body_json = json.loads(body_text)
                        is_json = True
                    except json.JSONDecodeError:
                        pass
                if body_json is None and body_text and len(body_text) <= 5_000_000:
                    body_json = self._unwrap_jsonp(body_text)
                    if body_json is not None:
                        is_json = True
            except Exception:  # noqa: BLE001
                body_text = None

            parsed = urlparse(url)
            query = {k: (v[0] if len(v) == 1 else v) for k, v in parse_qs(parsed.query).items()}
            post_parsed: Any = post_data
            if post_data:
                try:
                    post_parsed = json.loads(post_data)
                except json.JSONDecodeError:
                    post_parsed = post_data

            entry = {
                "request_id": rid,
                "timestamp": time.time(),
                "method": req.method,
                "url": url,
                "resource_type": mapped,
                "status": resp.status,
                "mime": mime,
                "is_json": is_json,
                "from_action": sess.action_tag,
                "request_headers": dict(req.headers),
                "response_headers": dict(resp.headers),
                "query": query,
                "post_data": post_parsed,
                "response_body_text": body_text,
                "response_body_json": body_json,
            }
            sess.network_buffer.append(entry)
            sess._last_network_at = time.time()
            if len(sess.network_buffer) > 1000:
                sess.network_buffer = sess.network_buffer[-800:]
        except Exception:  # noqa: BLE001
            return

    async def session_status(self, session_id: str) -> dict[str, Any]:
        sess = self._get(session_id)
        if not sess:
            return err("NO_SESSION", f"未知 session_id: {session_id}")
        return await self._status_payload(sess)

    async def _status_payload(self, sess: Session) -> dict[str, Any]:
        page = sess.active_page()
        auth = self._guess_auth(page.url)
        tabs = []
        for tid, p in sess.pages.items():
            tabs.append({"tab_id": tid, "url": p.url, "title": await p.title()})
        payload = ok(
            session_id=sess.session_id,
            platform=sess.platform,
            profile_path=str(profile_dir(sess.platform)),
            auth_status=auth,
            network_watching=sess.watching,
            active_tab={"tab_id": sess.active_tab_id, "url": page.url, "title": await page.title()},
            tabs=tabs,
            network_buffer_size=len(sess.network_buffer),
            needs_user_action=None,
            login_ok=sess.login_ok,
        )
        action = await self._detect_user_action(page)
        self._apply_user_action(payload, action, flag_error=False, sess=sess)
        if payload.get("needs_user_action") != "captcha" and not await self._refresh_login_flag(sess):
            self._overlay_login_block(payload, self._login_block(sess, page_url=page.url))
        else:
            payload["login_ok"] = sess.login_ok
        return payload

    async def _detect_user_action(self, page: Page, *, strict: bool = False) -> dict[str, Any] | None:
        """URL + 可见 DOM 启发式：是否停在登录 / 滑块验证上，需要人来处理。

        strict=True 时页面不可读（导航中）会抛异常，而不是当作「没有阻塞」。
        """
        url = (page.url or "").lower()
        if any(h in url for h in CAPTCHA_URL_HINTS):
            return {"kind": "captcha", "evidence": "url"}
        if self._guess_auth(page.url) == "needs_login":
            return {"kind": "login", "evidence": "url"}
        try:
            found = await page.evaluate(_DETECT_USER_ACTION_JS, {"rules": USER_ACTION_RULES})
        except Exception:  # noqa: BLE001
            if strict:
                raise
            return None
        return found or None

    def _apply_user_action(
        self,
        payload: dict[str, Any],
        action: dict[str, Any] | None,
        *,
        flag_error: bool = True,
        sess: Session | None = None,
    ) -> None:
        if not action:
            return
        kind = action["kind"]
        if kind == "login" and sess is not None:
            self._revoke_login(sess)
        payload["needs_user_action"] = kind
        payload["user_action"] = {**action, "hint": USER_ACTION_HINTS.get(kind)}
        # 登录墙和滑块都会挡住后续取数，调用方要停下来等人处理
        if flag_error and kind in ("captcha", "login") and payload.get("ok"):
            payload["ok"] = False
            payload["code"] = "NEEDS_CAPTCHA" if kind == "captcha" else "NEEDS_LOGIN"
            payload["message"] = USER_ACTION_HINTS[kind]

    def _guess_auth(self, url: str) -> str:
        low = (url or "").lower()
        if any(h in low for h in LOGIN_URL_HINTS):
            return "needs_login"
        if not url or url in ("about:blank", "chrome://newtab/"):
            return "unknown"
        return "logged_in_or_public"

    async def browser_navigate(
        self,
        session_id: str,
        url: str,
        wait_until: str = "domcontentloaded",
        tab_id: str | None = None,
    ) -> dict[str, Any]:
        sess = self._get(session_id)
        if not sess:
            return err("NO_SESSION", f"未知 session_id: {session_id}")
        if not self._is_prelogin_url(sess.platform, url):
            blocked = await self._require_login(sess)
            if blocked:
                return blocked
        if tab_id and tab_id in sess.pages:
            sess.active_tab_id = tab_id
        page = sess.active_page()
        try:
            resp = await page.goto(url, wait_until=wait_until, timeout=60_000)
            http_status = resp.status if resp else None
        except Exception as e:  # noqa: BLE001
            return err("NAVIGATE_FAILED", str(e), session_id=session_id)

        auth = self._guess_auth(page.url)
        payload = ok(
            session_id=session_id,
            final_url=page.url,
            title=await page.title(),
            http_status=http_status,
            auth_status=auth,
        )
        if auth == "needs_login":
            payload["ok"] = False
            payload["code"] = "NEEDS_LOGIN"
            payload["login_url"] = page.url
            payload["message"] = "检测到登录页：调用 wait_for_user(reason='login') 等用户在浏览器中完成登录"
        self._apply_user_action(payload, await self._detect_user_action(page), sess=sess)
        if not await self._refresh_login_flag(sess):
            self._overlay_login_block(payload, self._login_block(sess, page_url=page.url))
            payload["final_url"] = page.url
        else:
            payload["login_ok"] = True
        return payload

    def _resolve_locator(self, sess: Session, page: Page, ref: str | None, selector: str | None):
        if ref:
            sel = sess.ref_map.get(ref)
            if not sel:
                raise ValueError(f"INVALID_REF: {ref}")
            return page.locator(sel).first
        if selector:
            return page.locator(selector).first
        raise ValueError("需要 ref 或 selector")

    async def browser_interact(
        self,
        session_id: str,
        action: str,
        ref: str | None = None,
        selector: str | None = None,
        text: str | None = None,
        key: str | None = None,
        value: str | None = None,
        files: list[str] | None = None,
        submit: bool = False,
    ) -> dict[str, Any]:
        sess = self._get(session_id)
        if not sess:
            return err("NO_SESSION", f"未知 session_id: {session_id}")
        blocked = await self._require_login(sess)
        if blocked:
            return blocked
        page = sess.active_page()
        url_before = page.url
        try:
            if action == "press":
                await page.keyboard.press(key or "Enter")
            else:
                loc = self._resolve_locator(sess, page, ref, selector)
                if action == "click":
                    await loc.click(timeout=15_000)
                elif action == "type":
                    await loc.fill(text or "")
                    if submit:
                        await page.keyboard.press("Enter")
                elif action == "clear":
                    await loc.fill("")
                elif action == "hover":
                    await loc.hover(timeout=15_000)
                elif action == "select":
                    await loc.select_option(value or text or "")
                elif action == "upload":
                    await loc.set_input_files(files or [])
                else:
                    return err("INVALID_ACTION", f"未知 action: {action}")
        except Exception as e:  # noqa: BLE001
            return err("INTERACT_FAILED", str(e), session_id=session_id)

        await page.wait_for_timeout(200)
        payload = ok(
            session_id=session_id,
            action=action,
            navigation_happened=page.url != url_before,
            final_url=page.url,
            hint="建议紧接着 network_list 或 page_snapshot",
        )
        self._apply_user_action(payload, await self._detect_user_action(page), sess=sess)
        return payload

    async def browser_scroll(
        self,
        session_id: str,
        direction: str = "down",
        amount: int = 800,
        ref: str | None = None,
    ) -> dict[str, Any]:
        sess = self._get(session_id)
        if not sess:
            return err("NO_SESSION", f"未知 session_id: {session_id}")
        blocked = await self._require_login(sess)
        if blocked:
            return blocked
        page = sess.active_page()
        try:
            if direction == "to_ref" and ref:
                loc = self._resolve_locator(sess, page, ref, None)
                await loc.scroll_into_view_if_needed()
            elif direction == "top":
                await page.evaluate("window.scrollTo(0, 0)")
            elif direction == "bottom":
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            elif direction == "up":
                await page.evaluate(f"window.scrollBy(0, -{int(amount)})")
            else:
                await page.evaluate(f"window.scrollBy(0, {int(amount)})")
        except Exception as e:  # noqa: BLE001
            return err("SCROLL_FAILED", str(e))
        return ok(session_id=session_id, direction=direction)

    async def browser_wait(
        self,
        session_id: str,
        wait_for: str,
        value: str | None = None,
        timeout_ms: int = 30000,
    ) -> dict[str, Any]:
        sess = self._get(session_id)
        if not sess:
            return err("NO_SESSION", f"未知 session_id: {session_id}")
        blocked = await self._require_login(sess)
        if blocked:
            return blocked
        page = sess.active_page()
        try:
            if wait_for == "timeout":
                ms = self._parse_timeout(value, timeout_ms)
                await page.wait_for_timeout(ms)
            elif wait_for == "selector":
                await page.wait_for_selector(value or "body", timeout=timeout_ms)
            elif wait_for == "url":
                await page.wait_for_url(value or "**", timeout=timeout_ms)
            elif wait_for == "network_idle":
                await page.wait_for_load_state("networkidle", timeout=timeout_ms)
            elif wait_for == "network_quiet":
                quiet_ms = int(value) if value and value.isdigit() else 800
                await self._wait_network_quiet(sess, quiet_ms, timeout_ms)
            else:
                return err("INVALID_WAIT", f"未知 wait_for: {wait_for}")
        except Exception as e:  # noqa: BLE001
            return err("WAIT_TIMEOUT", str(e), session_id=session_id)
        return ok(session_id=session_id, wait_for=wait_for, url=page.url)

    async def wait_for_user(
        self,
        session_id: str,
        reason: str,
        message: str | None = None,
        until: dict[str, Any] | None = None,
        timeout_ms: int = 180000,
        poll_ms: int = 1000,
    ) -> dict[str, Any]:
        sess = self._get(session_id)
        if not sess:
            return err("NO_SESSION", f"未知 session_id: {session_id}")
        if reason not in WAIT_REASONS:
            return err("INVALID_REASON", f"reason 须为 {list(WAIT_REASONS)} 之一")
        if sess.headless:
            return err("HEADLESS_SESSION", "无头会话里用户看不到浏览器：请 session_open(headless=false) 后再试")
        page = sess.active_page()
        until = {k: v for k, v in (until or {}).items() if v not in (None, "")}
        initial = await self._detect_user_action(page)
        # 没给 until 时，只有开始时确实检测到同类阻塞，才在它消失后自动结束；否则只等用户点按钮
        auto_detect = bool(until) or bool(
            initial and initial["kind"] == reason and reason in ("login", "captcha")
        )
        continue_label, cancel_label, default_message = WAIT_REASONS[reason]
        banner_args = {
            "id": uuid.uuid4().hex,
            "message": message or default_message,
            "continueLabel": continue_label,
            "cancelLabel": cancel_label,
        }
        try:
            await page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass

        started = time.time()
        deadline = started + timeout_ms / 1000
        outcome = "timeout"
        met_streak = 0
        while True:
            state = None
            try:
                state = await page.evaluate(_WAIT_POLL_JS, {"id": banner_args["id"]})
            except Exception:  # noqa: BLE001
                pass  # 页面导航中
            if state and state.get("decision") in ("continue", "cancel"):
                outcome = f"user_{state['decision']}"
                break
            if auto_detect:
                # 连续多次满足才算完成，避免验证码刷新、登录跳转中途的瞬时状态
                met_streak = met_streak + 1 if await self._wait_condition_met(page, reason, until) else 0
                need = LOGIN_WAIT_STREAK if reason == "login" and not until else CAPTCHA_WAIT_STREAK
                if met_streak >= need:
                    outcome = "condition_met"
                    break
            if not state or not state.get("present"):
                try:
                    await page.evaluate(_WAIT_BANNER_JS, banner_args)
                except Exception:  # noqa: BLE001
                    pass
            if time.time() >= deadline:
                break
            await asyncio.sleep(poll_ms / 1000)

        try:
            await page.evaluate(_WAIT_CLEANUP_JS)
        except Exception:  # noqa: BLE001
            pass
        login_refresh = None
        if reason == "login" and outcome in ("condition_met", "user_continue"):
            login_refresh = await self._refresh_after_login(sess, page)
        try:
            title = await page.title()
        except Exception:  # noqa: BLE001
            title = ""
        payload = ok(
            session_id=session_id,
            reason=reason,
            outcome=outcome,
            auto_detect=auto_detect,
            waited_ms=int((time.time() - started) * 1000),
            url=page.url,
            title=title,
            needs_user_action=None,
            login_refresh=login_refresh,
        )
        self._apply_user_action(payload, await self._detect_user_action(page), flag_error=False, sess=sess)
        if reason == "login" and outcome in ("condition_met", "user_continue"):
            still_blocked = payload.get("needs_user_action") in ("login", "captcha")
            if await self._has_auth_cookie(sess) and not still_blocked:
                self._confirm_login(sess)
            else:
                sess.login_ok = False
        payload["login_ok"] = sess.login_ok
        if outcome == "user_cancel":
            payload["ok"] = False
            payload["code"] = "USER_CANCELLED"
            payload["message"] = "用户取消：停止当前操作，向用户确认下一步"
        elif outcome == "timeout":
            payload["ok"] = False
            payload["code"] = "USER_WAIT_TIMEOUT"
            payload["still_pending"] = True
            payload["message"] = "用户还没处理完：可再次调用 wait_for_user 继续等待"
        elif reason == "login" and outcome in ("condition_met", "user_continue") and not sess.login_ok:
            self._overlay_login_block(payload, self._login_block(sess, page_url=page.url))
            payload["outcome"] = outcome
        return payload

    async def _wait_condition_met(self, page: Page, reason: str, until: dict[str, Any]) -> bool:
        if until:
            url = page.url or ""
            if "url_contains" in until and str(until["url_contains"]) not in url:
                return False
            if "url_not_contains" in until and str(until["url_not_contains"]) in url:
                return False
            if "selector_gone" in until and await self._selector_visible(page, until["selector_gone"]) is not False:
                return False
            if "selector_present" in until and not await self._selector_visible(page, until["selector_present"]):
                return False
            return True
        try:
            action = await self._detect_user_action(page, strict=True)
        except Exception:  # noqa: BLE001
            return False
        if reason == "login":
            return self._login_flow_finished(page, action)
        return not action or action["kind"] != reason

    def _login_flow_finished(self, page: Page, action: dict[str, Any] | None) -> bool:
        """整段登录结束：滑块、短信、登录墙都算未完成，避免刚发验证码就返回。"""
        if action and action.get("kind") in ("login", "captcha"):
            return False
        if self._guess_auth(page.url) == "needs_login":
            return False
        url = (page.url or "").strip()
        if not url or url in ("about:blank", "chrome://newtab/"):
            return False
        return True

    async def _refresh_after_login(self, sess: Session, page: Page) -> str:
        """弹窗登录后 SPA 不会用新 Cookie 重渲染；还在登录 URL 则进平台首页。"""
        try:
            await page.wait_for_timeout(LOGIN_REFRESH_SETTLE_MS)
        except Exception:  # noqa: BLE001
            pass
        url = (page.url or "").strip()
        try:
            if self._guess_auth(url) == "needs_login" or not url or url in ("about:blank", "chrome://newtab/"):
                dest = PLATFORM_HOME_URLS.get(sess.platform) or PLATFORM_HOME_URLS["taobao"]
                await page.goto(dest, wait_until="domcontentloaded", timeout=60_000)
                return "navigate_home"
            await page.reload(wait_until="domcontentloaded", timeout=60_000)
            return "reload"
        except Exception:  # noqa: BLE001
            return "failed"

    async def _selector_visible(self, page: Page, selector: str) -> bool | None:
        """None 表示页面正在导航、无法判断。"""
        try:
            return bool(await page.evaluate(_SELECTOR_VISIBLE_JS, selector))
        except Exception:  # noqa: BLE001
            return None

    def _parse_timeout(self, value: str | None, default_ms: int) -> int:
        if not value:
            return default_ms
        m = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s)?", value.strip())
        if not m:
            return default_ms
        n = float(m.group(1))
        unit = m.group(2) or "ms"
        return int(n * 1000) if unit == "s" else int(n)

    async def _wait_network_quiet(self, sess: Session, quiet_ms: int, timeout_ms: int) -> None:
        deadline = time.time() + timeout_ms / 1000
        # 至少先等一小会儿让请求发出
        await asyncio.sleep(0.3)
        while time.time() < deadline:
            last = sess._last_network_at or 0
            if last and (time.time() - last) * 1000 >= quiet_ms:
                return
            if not sess.watching:
                await asyncio.sleep(quiet_ms / 1000)
                return
            await asyncio.sleep(0.15)
        raise TimeoutError(f"network_quiet 超时 ({timeout_ms}ms)")

    async def browser_tabs(
        self,
        session_id: str,
        action: str,
        tab_id: str | None = None,
        url: str | None = None,
    ) -> dict[str, Any]:
        sess = self._get(session_id)
        if not sess:
            return err("NO_SESSION", f"未知 session_id: {session_id}")

        if action == "list":
            tabs = []
            for tid, p in sess.pages.items():
                tabs.append({"tab_id": tid, "url": p.url, "title": await p.title()})
            return ok(session_id=session_id, tabs=tabs, active_tab_id=sess.active_tab_id)

        if action == "new":
            if not url or not self._is_prelogin_url(sess.platform, url):
                blocked = await self._require_login(sess)
                if blocked:
                    return blocked
            page = await sess.context.new_page()
            new_id = f"t{len(sess.pages) + 1}"
            while new_id in sess.pages:
                new_id = f"t{uuid.uuid4().hex[:4]}"
            sess.pages[new_id] = page
            sess.active_tab_id = new_id
            self._bind_page_listeners(sess, page)
            if url:
                await page.goto(url, wait_until="domcontentloaded")
            return ok(session_id=session_id, tab_id=new_id, url=page.url)

        if action == "switch":
            if not tab_id or tab_id not in sess.pages:
                return err("INVALID_TAB", f"未知 tab_id: {tab_id}")
            sess.active_tab_id = tab_id
            await sess.pages[tab_id].bring_to_front()
            return ok(session_id=session_id, tab_id=tab_id, url=sess.pages[tab_id].url)

        if action == "close":
            tid = tab_id or sess.active_tab_id
            if tid not in sess.pages:
                return err("INVALID_TAB", f"未知 tab_id: {tid}")
            if len(sess.pages) == 1:
                return err("LAST_TAB", "不能关闭最后一个标签页")
            await sess.pages[tid].close()
            del sess.pages[tid]
            if sess.active_tab_id == tid:
                sess.active_tab_id = next(iter(sess.pages))
            return ok(session_id=session_id, closed=tid, active_tab_id=sess.active_tab_id)

        return err("INVALID_ACTION", f"未知 action: {action}")

    async def page_snapshot(
        self,
        session_id: str,
        mode: str = "interactive",
        max_nodes: int = 200,
        interesting_only: bool = True,
    ) -> dict[str, Any]:
        sess = self._get(session_id)
        if not sess:
            return err("NO_SESSION", f"未知 session_id: {session_id}")
        page = sess.active_page()
        auth = self._guess_auth(page.url)
        nodes = await page.evaluate(
            """({ mode, maxNodes, interestingOnly }) => {
              const out = [];
              const push = (el, role, name) => {
                if (out.length >= maxNodes) return;
                const ref = 'e' + (out.length + 1);
                el.setAttribute('data-br-ref', ref);
                let selector = '[data-br-ref=\"' + ref + '\"]';
                out.push({
                  ref,
                  role: role || el.getAttribute('role') || el.tagName.toLowerCase(),
                  name: (name || el.getAttribute('aria-label') || el.innerText || el.getAttribute('placeholder') || '').trim().slice(0, 120),
                  value: (el.value !== undefined ? String(el.value) : '').slice(0, 200),
                  tag: el.tagName.toLowerCase(),
                  href: el.href || null,
                });
              };

              document.querySelectorAll('[data-br-ref]').forEach(el => el.removeAttribute('data-br-ref'));

              if (mode === 'article') {
                const article = document.querySelector('article, .Post-RichText, .RichText.ztext, main');
                if (article) {
                  push(article, 'article', (document.querySelector('h1')||{}).innerText || document.title);
                  const paras = article.querySelectorAll('h1,h2,p');
                  paras.forEach(p => {
                    if (out.length < maxNodes) push(p, p.tagName.toLowerCase(), p.innerText);
                  });
                }
                return out;
              }

              const sel = interestingOnly
                ? 'a[href], button, input, textarea, select, [role=\"button\"], [role=\"link\"], [role=\"textbox\"], [contenteditable=\"true\"]'
                : 'a, button, input, textarea, select, h1, h2, h3, p, [role]';
              document.querySelectorAll(sel).forEach(el => {
                if (out.length >= maxNodes) return;
                const style = window.getComputedStyle(el);
                if (style && (style.display === 'none' || style.visibility === 'hidden')) return;
                const rect = el.getBoundingClientRect();
                if (rect.width < 2 || rect.height < 2) return;
                push(el, null, null);
              });
              return out;
            }""",
            {"mode": mode, "maxNodes": max_nodes, "interestingOnly": interesting_only},
        )
        sess.ref_map = {n["ref"]: f'[data-br-ref="{n["ref"]}"]' for n in nodes}
        payload = ok(
            session_id=session_id,
            url=page.url,
            title=await page.title(),
            auth_status=auth,
            mode=mode,
            nodes=nodes,
        )
        if auth == "needs_login":
            payload["code"] = "NEEDS_LOGIN"
            payload["login_url"] = page.url
            payload["message"] = "当前可能在登录页"
        self._apply_user_action(payload, await self._detect_user_action(page), flag_error=False, sess=sess)
        return payload

    async def page_screenshot(
        self,
        session_id: str,
        full_page: bool = False,
        ref: str | None = None,
    ) -> dict[str, Any]:
        sess = self._get(session_id)
        if not sess:
            return err("NO_SESSION", f"未知 session_id: {session_id}")
        page = sess.active_page()
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SCREENSHOT_DIR / f"{sess.session_id}_{int(time.time())}.png"
        try:
            if ref:
                loc = self._resolve_locator(sess, page, ref, None)
                await loc.screenshot(path=str(path))
            else:
                await page.screenshot(path=str(path), full_page=full_page)
        except Exception as e:  # noqa: BLE001
            return err("SCREENSHOT_FAILED", str(e))
        return ok(session_id=session_id, path=str(path))

    async def page_query(
        self,
        session_id: str,
        by: str,
        query: str,
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        sess = self._get(session_id)
        if not sess:
            return err("NO_SESSION", f"未知 session_id: {session_id}")
        blocked = await self._require_login(sess)
        if blocked:
            return blocked
        page = sess.active_page()
        fields = fields or ["text", "count"]
        try:
            if by == "ref":
                loc = self._resolve_locator(sess, page, query, None)
            elif by == "selector":
                loc = page.locator(query)
            elif by == "role":
                loc = page.get_by_role(query)  # type: ignore[arg-type]
            elif by == "text":
                loc = page.get_by_text(query)
            else:
                return err("INVALID_BY", f"未知 by: {by}")

            count = await loc.count()
            result: dict[str, Any] = {"count": count}
            if count == 0:
                return ok(session_id=session_id, by=by, query=query, result=result)
            first = loc.first
            if "text" in fields:
                result["text"] = (await first.inner_text())[:4000]
            if "html" in fields:
                result["html"] = (await first.inner_html())[:8000]
            if "attrs" in fields:
                result["attrs"] = await first.evaluate(
                    """el => {
                      const o = {};
                      for (const a of el.attributes) o[a.name] = a.value;
                      return o;
                    }"""
                )
            if "box" in fields:
                result["box"] = await first.bounding_box()
            if "count" not in fields:
                result.pop("count", None)
                result["count"] = count
            return ok(session_id=session_id, by=by, query=query, result=result)
        except Exception as e:  # noqa: BLE001
            return err("QUERY_FAILED", str(e))

    async def page_evaluate(
        self,
        session_id: str,
        expression: str,
        return_json: bool = True,
    ) -> dict[str, Any]:
        sess = self._get(session_id)
        if not sess:
            return err("NO_SESSION", f"未知 session_id: {session_id}")
        blocked = await self._require_login(sess)
        if blocked:
            return blocked
        page = sess.active_page()
        # 粗防：禁止直接读 cookie
        lowered = expression.lower()
        if "document.cookie" in lowered or "localstorage" in lowered:
            return err("FORBIDDEN", "禁止通过 evaluate 读取 cookie/localStorage")
        try:
            # 允许表达式或函数体
            if expression.strip().startswith("()") or "=>" in expression or expression.strip().startswith("function"):
                result = await page.evaluate(expression)
            else:
                result = await page.evaluate(f"() => ({expression})")
        except Exception as e:  # noqa: BLE001
            return err("EVAL_FAILED", str(e))
        if return_json:
            try:
                json.dumps(result)
            except TypeError:
                result = str(result)
        return ok(session_id=session_id, result=result)

    async def devtools_console(
        self,
        session_id: str,
        level: str = "error",
        limit: int = 50,
        since_ms: int | None = None,
    ) -> dict[str, Any]:
        sess = self._get(session_id)
        if not sess:
            return err("NO_SESSION", f"未知 session_id: {session_id}")
        level_map = {
            "error": {"error"},
            "warn": {"error", "warning", "warn"},
            "info": {"error", "warning", "warn", "info", "log"},
            "all": None,
        }
        allow = level_map.get(level, {"error"})
        cutoff = (since_ms / 1000) if since_ms else None
        msgs = []
        for m in sess.console:
            if cutoff and m["ts"] < cutoff:
                continue
            if allow is not None and m["level"] not in allow:
                continue
            msgs.append(m)
        return ok(session_id=session_id, messages=msgs[-limit:])

    async def network_watch(
        self,
        session_id: str,
        action: str,
        filter: dict[str, Any] | None = None,
        action_tag: str | None = None,
    ) -> dict[str, Any]:
        sess = self._get(session_id)
        if not sess:
            return err("NO_SESSION", f"未知 session_id: {session_id}")
        if action == "status":
            return ok(
                session_id=session_id,
                watching=sess.watching,
                action_tag=sess.action_tag,
                buffer_size=len(sess.network_buffer),
                filter={
                    "resource_types": sess.network_filter.resource_types,
                    "url_includes": sess.network_filter.url_includes,
                    "url_excludes": sess.network_filter.url_excludes,
                },
            )
        if action == "stop":
            sess.watching = False
            return ok(session_id=session_id, watching=False, buffer_size=len(sess.network_buffer))
        if action == "start":
            blocked = await self._require_login(sess)
            if blocked:
                return blocked
            f = filter or {}
            sess.network_filter = NetworkFilter(
                resource_types=list(f.get("resource_types") or ["fetch", "xhr"]),
                url_includes=list(f.get("url_includes") or []),
                url_excludes=list(
                    f.get("url_excludes")
                    if f.get("url_excludes") is not None
                    else ["analytics", "tracking", "wxgamesdk", "sentry"]
                ),
            )
            sess.watching = True
            sess.watch_started_at = time.time()
            sess.action_tag = action_tag
            if action_tag:
                sess.tag_started_at[action_tag] = time.time()
            return ok(
                session_id=session_id,
                watching=True,
                action_tag=action_tag,
                buffer_size=len(sess.network_buffer),
            )
        return err("INVALID_ACTION", f"未知 action: {action}")

    def _filter_since(self, sess: Session, since: str | None) -> list[dict[str, Any]]:
        items = sess.network_buffer
        if not since:
            return items
        if since == "watch_start":
            t0 = sess.watch_started_at or 0
            return [x for x in items if x["timestamp"] >= t0]
        if since.startswith("action_tag:"):
            tag = since.split(":", 1)[1]
            t0 = sess.tag_started_at.get(tag)
            if t0 is None:
                return [x for x in items if x.get("from_action") == tag]
            return [x for x in items if x["timestamp"] >= t0]
        if since.startswith("request_id:"):
            rid = since.split(":", 1)[1]
            idx = next((i for i, x in enumerate(items) if x["request_id"] == rid), None)
            if idx is None:
                return []
            return items[idx + 1 :]
        return items

    def _summary(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "request_id": item["request_id"],
            "timestamp": item["timestamp"],
            "method": item["method"],
            "url": item["url"],
            "resource_type": item["resource_type"],
            "status": item["status"],
            "mime": item["mime"],
            "is_json": item.get("is_json"),
            "from_action": item.get("from_action"),
        }

    async def network_list(
        self,
        session_id: str,
        since: str | None = None,
        limit: int = 30,
        only_json: bool = True,
    ) -> dict[str, Any]:
        sess = self._get(session_id)
        if not sess:
            return err("NO_SESSION", f"未知 session_id: {session_id}")
        blocked = await self._require_login(sess)
        if blocked:
            return blocked
        items = self._filter_since(sess, since)
        if only_json:
            items = [x for x in items if x.get("is_json")]
        items = items[-limit:]
        return ok(
            session_id=session_id,
            count=len(items),
            items=[self._summary(x) for x in items],
        )

    async def network_search(
        self,
        session_id: str,
        q: str | None = None,
        method: str | None = None,
        status: int | None = None,
        has_json_body: bool | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        sess = self._get(session_id)
        if not sess:
            return err("NO_SESSION", f"未知 session_id: {session_id}")
        blocked = await self._require_login(sess)
        if blocked:
            return blocked
        items = sess.network_buffer
        if q:
            ql = q.lower()
            items = [x for x in items if ql in x["url"].lower()]
        if method:
            items = [x for x in items if x["method"].upper() == method.upper()]
        if status is not None:
            items = [x for x in items if x["status"] == status]
        if has_json_body is not None:
            items = [x for x in items if bool(x.get("is_json")) == has_json_body]
        items = items[-limit:]
        return ok(session_id=session_id, count=len(items), items=[self._summary(x) for x in items])

    async def network_get(
        self,
        session_id: str,
        request_id: str,
        include: list[str] | None = None,
        body_max_chars: int = 80000,
        redact_secrets: bool = True,
    ) -> dict[str, Any]:
        sess = self._get(session_id)
        if not sess:
            return err("NO_SESSION", f"未知 session_id: {session_id}")
        blocked = await self._require_login(sess)
        if blocked:
            return blocked
        item = next((x for x in sess.network_buffer if x["request_id"] == request_id), None)
        if not item:
            return err("NOT_FOUND", f"未知 request_id: {request_id}")
        include = include or ["query", "post_data", "response_body"]
        req: dict[str, Any] = {"method": item["method"], "url": item["url"]}
        resp: dict[str, Any] = {"status": item["status"], "mime": item["mime"]}

        if "request_headers" in include:
            hdrs = dict(item.get("request_headers") or {})
            if redact_secrets:
                hdrs = self._redact_headers(hdrs)
            req["headers"] = hdrs
        if "query" in include:
            req["query"] = item.get("query")
        if "post_data" in include:
            req["post_data"] = item.get("post_data")
        if "response_headers" in include:
            hdrs = dict(item.get("response_headers") or {})
            if redact_secrets:
                hdrs = self._redact_headers(hdrs)
            resp["headers"] = hdrs
        if "response_body" in include:
            truncated = False
            if item.get("response_body_json") is not None:
                text = json.dumps(item["response_body_json"], ensure_ascii=False)
                if len(text) > body_max_chars:
                    resp["body_json"] = None
                    resp["body_text"] = text[:body_max_chars]
                    truncated = True
                else:
                    resp["body_json"] = item["response_body_json"]
                    resp["body_text"] = None
            else:
                text = item.get("response_body_text") or ""
                if len(text) > body_max_chars:
                    text = text[:body_max_chars]
                    truncated = True
                resp["body_text"] = text
                resp["body_json"] = None
            resp["truncated"] = truncated

        hints = self._analysis_hints(item)
        return ok(
            session_id=session_id,
            request_id=request_id,
            request=req,
            response=resp,
            analysis_hints=hints,
        )

    def _redact_headers(self, headers: dict[str, str]) -> dict[str, str]:
        out = {}
        for k, v in headers.items():
            if k.lower() in SECRET_HEADER_KEYS:
                out[k] = "***REDACTED***"
            else:
                out[k] = v
        return out

    def _analysis_hints(self, item: dict[str, Any]) -> dict[str, Any]:
        body = item.get("response_body_json")
        post = item.get("post_data")
        input_fields: list[str] = []
        if isinstance(post, dict):
            input_fields = list(post.keys())
        elif isinstance(item.get("query"), dict):
            input_fields = list(item["query"].keys())
        list_paths: list[str] = []
        id_paths: list[str] = []
        scopes: list[tuple[str, Any]] = [("", body)]
        # mtop 等网关把业务数据包在 data 下
        if isinstance(body, dict) and isinstance(body.get("data"), dict):
            scopes.append(("data.", body["data"]))
        for prefix, scope in scopes:
            if not isinstance(scope, dict):
                continue
            for k, v in scope.items():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    list_paths.append(f"{prefix}{k}")
                    if "id" in v[0]:
                        id_paths.append(f"{prefix}{k}[].id")
        mtop = _mtop_from_item(item)
        return {
            "likely_api": bool(item.get("is_json") and item.get("status") == 200),
            "input_fields": list(mtop["data"].keys()) if mtop else input_fields,
            "list_paths": list_paths,
            "id_paths": id_paths,
            "mtop": (
                {"api": mtop["api"], "v": mtop["v"], "replay": "page_api_call(mode=mtop) / memory exec_mode=page_mtop"}
                if mtop
                else None
            ),
        }

    async def network_clear(self, session_id: str, before: str | None = None) -> dict[str, Any]:
        sess = self._get(session_id)
        if not sess:
            return err("NO_SESSION", f"未知 session_id: {session_id}")
        if not before:
            n = len(sess.network_buffer)
            sess.network_buffer.clear()
            return ok(session_id=session_id, cleared=n)
        idx = next((i for i, x in enumerate(sess.network_buffer) if x["request_id"] == before), None)
        if idx is None:
            return err("NOT_FOUND", f"未知 request_id: {before}")
        cleared = idx
        sess.network_buffer = sess.network_buffer[idx:]
        return ok(session_id=session_id, cleared=cleared)

    async def memory_save_api(
        self,
        session_id: str,
        name: str,
        method: str,
        url_template: str,
        source_request_id: str | None = None,
        headers_template: dict[str, str] | None = None,
        query_template: dict[str, Any] | None = None,
        body_template: dict[str, Any] | None = None,
        input_schema: dict[str, str] | None = None,
        output_paths: dict[str, str] | None = None,
        notes: str = "",
        exec_mode: str | None = None,
    ) -> dict[str, Any]:
        sess = self._get(session_id)
        if not sess:
            return err("NO_SESSION", f"未知 session_id: {session_id}")
        blocked = await self._require_login(sess)
        if blocked:
            return blocked
        if exec_mode is not None and exec_mode not in EXEC_MODES:
            return err("INVALID_EXEC_MODE", f"exec_mode 须为 {EXEC_MODES} 之一")

        item = None
        if source_request_id:
            item = next((x for x in sess.network_buffer if x["request_id"] == source_request_id), None)

        mtop: dict[str, Any] | None = None
        if exec_mode in (None, "page_mtop"):
            if item:
                mtop = _mtop_from_item(item)
            if mtop is None:
                m = _MTOP_PATH_RE.search(urlparse(url_template).path)
                if m:
                    mtop = {
                        "api": m.group(1),
                        "v": m.group(2),
                        "type": method.upper(),
                        "need_login": False,
                        "data": {},
                    }
            if mtop is None and exec_mode == "page_mtop":
                return err(
                    "INVALID_MTOP",
                    "page_mtop 需要 mtop 源请求，或 url_template 形如 https://h5api.m.taobao.com/h5/{api}/{v}/",
                )
        if exec_mode is None:
            exec_mode = "page_mtop" if mtop else "context"

        if exec_mode == "page_mtop" and mtop:
            # 只存业务 data；t / sign / token 由页面里的 lib.mtop 每次现算
            if body_template is None:
                body_template = mtop["data"]
            query_template = {}
        elif item:
            if body_template is None and isinstance(item.get("post_data"), dict):
                body_template = item["post_data"]
            if not query_template and item.get("query"):
                query_template = item["query"]
        if item:
            if not input_schema and isinstance(body_template, dict):
                input_schema = {k: "string" for k in body_template}
            if not output_paths:
                hints = self._analysis_hints(item)
                output_paths = {p.rsplit(".", 1)[-1]: f"$.{p}" for p in hints.get("list_paths") or []}

        record: dict[str, Any] = {
            "name": name,
            "exec_mode": exec_mode,
            "method": method,
            "url_template": url_template,
            "source_request_id": source_request_id,
            "headers_template": headers_template or {},
            "query_template": query_template or {},
            "body_template": body_template,
            "input_schema": input_schema or {},
            "output_paths": output_paths or {},
            "notes": notes,
        }
        if exec_mode == "page_mtop" and mtop:
            record["mtop"] = {
                "api": mtop["api"],
                "v": mtop["v"],
                "type": mtop["type"],
                "need_login": mtop["need_login"],
            }
        result = memory_store.save_capability(sess.platform, record)
        return ok(session_id=session_id, exec_mode=exec_mode, **result)

    async def memory_list(
        self,
        session_id: str | None = None,
        platform: str | None = None,
    ) -> dict[str, Any]:
        if platform is None and session_id:
            sess = self._get(session_id)
            if sess:
                platform = sess.platform
        caps = memory_store.list_capabilities(platform)
        return ok(capabilities=caps, platform=platform)

    @staticmethod
    def _unwrap_jsonp(text: str) -> Any:
        """先按 JSON 解析；失败则尝试拆 JSONP 包裹 name({...}) / name([...])。"""
        text = (text or "").strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        m = _JSONP_RE.match(text)
        if not m:
            return None
        inner = m.group(2).strip()
        if inner and inner[0] in "{([":
            try:
                return json.loads(inner)
            except json.JSONDecodeError:
                return None
        return None

    def _parse_body(self, text: str, jsonp: bool = True) -> Any:
        if jsonp:
            return self._unwrap_jsonp(text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    async def session_api_call(
        self,
        session_id: str,
        url: str,
        method: str = "GET",
        query: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        body: Any = None,
        jsonp: bool = True,
        body_max_chars: int = 80000,
        redact_secrets: bool = True,
    ) -> dict[str, Any]:
        sess = self._get(session_id)
        if not sess:
            return err("NO_SESSION", f"未知 session_id: {session_id}")
        blocked = await self._require_login(sess)
        if blocked:
            return blocked
        method = method.upper()
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            return err("INVALID_METHOD", f"不支持 method: {method}")

        headers = dict(headers or {})
        header_keys_lower = {k.lower() for k in headers}
        # 缺省补 UA / Referer，降低淘宝等接口 403 概率
        if "user-agent" not in header_keys_lower and sess.pages:
            try:
                ua = await sess.active_page().evaluate("navigator.userAgent")
                if ua:
                    headers["User-Agent"] = ua
            except Exception:  # noqa: BLE001
                pass
        if "referer" not in header_keys_lower:
            try:
                headers["Referer"] = urlparse(url)._replace(path="/", params="", query="", fragment="").geturl()
            except Exception:  # noqa: BLE001
                pass

        try:
            status, resp_headers, text = await self._context_request(
                sess, method=method, url=url, query=query, headers=headers, body=body
            )
        except Exception as e:  # noqa: BLE001
            return err("REQUEST_FAILED", str(e))
        data = self._parse_body(text, jsonp=jsonp)
        if redact_secrets:
            resp_headers = self._redact_headers(resp_headers)
        return ok(
            session_id=session_id,
            status=status,
            headers=resp_headers,
            **_body_payload(text, data, body_max_chars),
        )

    async def _context_request(
        self,
        sess: Session,
        *,
        method: str,
        url: str,
        query: dict[str, Any] | None,
        headers: dict[str, str] | None,
        body: Any,
    ) -> tuple[int, dict[str, str], str]:
        """Playwright APIRequestContext 直连：带会话 Cookie、不受 CORS 限制、不执行页面 JS。"""
        method = method.upper()
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            raise ValueError(f"不支持 method: {method}")
        kwargs: dict[str, Any] = {"headers": headers or None}
        if method in ("POST", "PUT", "PATCH", "DELETE") and body is not None:
            # dict/list 由 Playwright 序列化为 JSON 并补 content-type
            kwargs["data"] = body
        resp = await sess.context.request.fetch(_merge_query(url, query), method=method, **kwargs)
        return resp.status, dict(resp.headers), await resp.text()

    async def _page_fetch(
        self,
        page: Page,
        *,
        method: str,
        url: str,
        query: dict[str, Any] | None,
        headers: dict[str, str] | None,
        body: Any,
        timeout_ms: int,
    ) -> dict[str, Any]:
        raw = await asyncio.wait_for(
            page.evaluate(
                _PAGE_FETCH_JS,
                {
                    "url": _merge_query(url, query),
                    "method": method.upper(),
                    "headers": headers or {},
                    "body": body,
                    "bodyIsJson": isinstance(body, (dict, list)),
                },
            ),
            timeout=timeout_ms / 1000,
        )
        if not raw.get("ok"):
            return err(raw.get("code") or "PAGE_FETCH_FAILED", raw.get("error") or "页面内 fetch 失败")
        return raw

    async def _page_mtop(
        self,
        page: Page,
        *,
        api: str,
        v: str,
        data: Any,
        method: str,
        need_login: bool,
        options: dict[str, Any] | None,
        lib_url: str | None,
        timeout_ms: int,
    ) -> dict[str, Any]:
        params = {
            **(options or {}),
            "api": api,
            "v": v,
            "data": data if data is not None else {},
            "type": method.upper(),
            "ecode": 1 if need_login else 0,
        }
        raw = await asyncio.wait_for(
            page.evaluate(_PAGE_MTOP_JS, {"params": params, "libUrl": lib_url}),
            timeout=timeout_ms / 1000,
        )
        if not raw.get("ok"):
            code = raw.get("code") or "MTOP_ERROR"
            message = raw.get("error") or "lib.mtop 调用失败"
            if code == "MTOP_LIB_MISSING":
                message = (
                    "当前页面没有 window.lib.mtop：先 browser_navigate 到会发 mtop 请求的淘宝/天猫页面，"
                    "或传 mtop_lib_url（从 Network 里找页面加载的 mtop.js 地址）"
                )
            return err(code, message, page_url=page.url)
        text = raw.get("text") or ""
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            result = None
        success, ret, hint = _mtop_outcome(result)
        return {"ok": True, "text": text, "result": result, "success": success, "ret": ret, "hint": hint}

    async def page_api_call(
        self,
        session_id: str,
        mode: str,
        url: str | None = None,
        method: str = "GET",
        query: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        body: Any = None,
        api: str | None = None,
        v: str | None = None,
        data: Any = None,
        need_login: bool = False,
        mtop_options: dict[str, Any] | None = None,
        mtop_lib_url: str | None = None,
        jsonp: bool = True,
        timeout_ms: int = 30000,
        body_max_chars: int = 80000,
        redact_secrets: bool = True,
    ) -> dict[str, Any]:
        sess = self._get(session_id)
        if not sess:
            return err("NO_SESSION", f"未知 session_id: {session_id}")
        blocked = await self._require_login(sess)
        if blocked:
            return blocked
        page = sess.active_page()

        if mode == "fetch":
            if not url:
                return err("INVALID_ARGS", "mode=fetch 需要 url（可相对当前页面）")
            try:
                raw = await self._page_fetch(
                    page,
                    method=method,
                    url=url,
                    query=query,
                    headers=headers,
                    body=body,
                    timeout_ms=timeout_ms,
                )
            except asyncio.TimeoutError:
                return err("PAGE_CALL_TIMEOUT", f"页面内 fetch 超时 ({timeout_ms}ms)")
            except Exception as e:  # noqa: BLE001
                return err("PAGE_FETCH_FAILED", str(e))
            if not raw.get("ok"):
                return raw
            text = raw.get("text") or ""
            resp_headers = raw.get("headers") or {}
            if redact_secrets:
                resp_headers = self._redact_headers(resp_headers)
            return ok(
                session_id=session_id,
                mode=mode,
                status=raw.get("status"),
                final_url=raw.get("url"),
                headers=resp_headers,
                **_body_payload(text, self._parse_body(text, jsonp=jsonp), body_max_chars),
            )

        if mode == "mtop":
            if not api and url:
                m = _MTOP_PATH_RE.search(urlparse(url).path)
                if m:
                    api, v = m.group(1), v or m.group(2)
            if not api:
                return err("INVALID_ARGS", "mode=mtop 需要 api（如 mtop.taobao.rate.detaillist.get），或可解析的 mtop url")
            try:
                outcome = await self._page_mtop(
                    page,
                    api=api,
                    v=v or "1.0",
                    data=data,
                    method=method,
                    need_login=need_login,
                    options=mtop_options,
                    lib_url=mtop_lib_url,
                    timeout_ms=timeout_ms,
                )
            except asyncio.TimeoutError:
                return err("PAGE_CALL_TIMEOUT", f"lib.mtop 调用超时 ({timeout_ms}ms)")
            except Exception as e:  # noqa: BLE001
                return err("MTOP_ERROR", str(e))
            if not outcome.get("ok"):
                return outcome
            payload = ok(
                session_id=session_id,
                mode=mode,
                api=api,
                v=v or "1.0",
                mtop_success=outcome["success"],
                mtop_ret=outcome["ret"],
                hint=outcome["hint"],
                **_body_payload(outcome["text"], outcome["result"], body_max_chars),
            )
            if not outcome["success"]:
                payload["ok"] = False
                payload["code"] = "MTOP_FAILED"
            if self._ret_means_logged_out(outcome.get("ret")):
                self._revoke_login(sess)
                self._overlay_login_block(payload, self._login_block(sess, page_url=page.url))
            return payload

        return err("INVALID_MODE", f"未知 mode: {mode}")

    async def memory_run_api(
        self,
        session_id: str,
        capability_id: str,
        params: dict[str, Any] | None = None,
        jsonp: bool = True,
        timeout_ms: int = 30000,
        body_max_chars: int = 80000,
    ) -> dict[str, Any]:
        sess = self._get(session_id)
        if not sess:
            return err("NO_SESSION", f"未知 session_id: {session_id}")
        blocked = await self._require_login(sess)
        if blocked:
            return blocked
        cap = memory_store.get_capability(capability_id, sess.platform)
        if not cap:
            return err("CAPABILITY_NOT_FOUND", f"未找到能力: {capability_id}")
        params = params or {}
        exec_mode = cap.get("exec_mode") or "context"
        url = memory_store.render_template(cap["url_template"], params)
        headers = memory_store.render_template(cap.get("headers_template") or {}, params)
        query = memory_store.render_template(cap.get("query_template") or {}, params)
        body = memory_store.render_template(cap.get("body_template"), params)
        method = (cap.get("method") or "GET").upper()
        extra: dict[str, Any] = {}
        try:
            if exec_mode == "page_mtop":
                mtop = cap.get("mtop") or {}
                api, v = mtop.get("api"), mtop.get("v")
                if not api:
                    m = _MTOP_PATH_RE.search(urlparse(url).path)
                    if m:
                        api, v = m.group(1), v or m.group(2)
                if not api:
                    return err("INVALID_MTOP", "能力缺少 mtop api", capability_id=capability_id)
                outcome = await self._page_mtop(
                    sess.active_page(),
                    api=api,
                    v=v or "1.0",
                    data=body,
                    method=mtop.get("type") or method,
                    need_login=bool(mtop.get("need_login")),
                    options=None,
                    lib_url=None,
                    timeout_ms=timeout_ms,
                )
                if not outcome.get("ok"):
                    return {**outcome, "capability_id": capability_id}
                status = None
                text, data = outcome["text"], outcome["result"]
                extra = {
                    "mtop_success": outcome["success"],
                    "mtop_ret": outcome["ret"],
                    "hint": outcome["hint"],
                }
            elif exec_mode == "page_fetch":
                raw = await self._page_fetch(
                    sess.active_page(),
                    method=method,
                    url=url,
                    query=query,
                    headers=headers,
                    body=body,
                    timeout_ms=timeout_ms,
                )
                if not raw.get("ok"):
                    return {**raw, "capability_id": capability_id}
                status = raw.get("status")
                text = raw.get("text") or ""
                data = self._parse_body(text, jsonp=jsonp)
            else:
                status, _, text = await self._context_request(
                    sess, method=method, url=url, query=query, headers=headers, body=body
                )
                data = self._parse_body(text, jsonp=jsonp)
        except asyncio.TimeoutError:
            return err("PAGE_CALL_TIMEOUT", f"重放超时 ({timeout_ms}ms)", capability_id=capability_id)
        except Exception as e:  # noqa: BLE001
            return err("REPLAY_FAILED", str(e), capability_id=capability_id)

        extracted = memory_store.extract_by_paths(data, cap.get("output_paths") or {}) if data is not None else {}
        payload = ok(
            session_id=session_id,
            capability_id=capability_id,
            exec_mode=exec_mode,
            status=status,
            extracted=extracted,
            **extra,
            **_body_payload(text, data, body_max_chars),
        )
        if exec_mode == "page_mtop" and not extra.get("mtop_success"):
            payload["ok"] = False
            payload["code"] = "MTOP_FAILED"
        if exec_mode == "page_mtop" and self._ret_means_logged_out(extra.get("mtop_ret")):
            self._revoke_login(sess)
            try:
                page_url = sess.active_page().url
            except Exception:  # noqa: BLE001
                page_url = ""
            self._overlay_login_block(payload, self._login_block(sess, page_url=page_url))
        return payload


RUNTIME = Runtime()
