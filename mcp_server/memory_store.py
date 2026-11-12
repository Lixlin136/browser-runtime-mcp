"""临时 API 能力落盘（Memory 层）。"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

MEMORY_ROOT = Path.home() / ".browser-runtime" / "_capabilities"


def _dir(platform: str) -> Path:
    d = MEMORY_ROOT / platform
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_capability(platform: str, payload: dict[str, Any]) -> dict[str, Any]:
    cap_id = payload.get("capability_id") or f"cap_{uuid.uuid4().hex[:10]}"
    record = {
        **payload,
        "capability_id": cap_id,
        "platform": platform,
        "saved_at": time.time(),
    }
    path = _dir(platform) / f"{cap_id}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"capability_id": cap_id, "saved": True, "path": str(path)}


def list_capabilities(platform: str | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    roots = [_dir(platform)] if platform else (
        [p for p in MEMORY_ROOT.iterdir() if p.is_dir()] if MEMORY_ROOT.exists() else []
    )
    if platform:
        roots = [_dir(platform)]
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.glob("cap_*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            items.append(
                {
                    "capability_id": data.get("capability_id"),
                    "name": data.get("name"),
                    "exec_mode": data.get("exec_mode") or "context",
                    "method": data.get("method"),
                    "url_template": data.get("url_template"),
                    "platform": data.get("platform"),
                    "notes": data.get("notes", ""),
                }
            )
    return items


def get_capability(capability_id: str, platform: str | None = None) -> dict[str, Any] | None:
    candidates: list[Path] = []
    if platform:
        candidates.append(_dir(platform) / f"{capability_id}.json")
    elif MEMORY_ROOT.exists():
        candidates.extend(MEMORY_ROOT.glob(f"*/{capability_id}.json"))
    for path in candidates:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    return None


_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def render_template(obj: Any, params: dict[str, Any]) -> Any:
    """递归替换 {{param}} 占位。"""
    if isinstance(obj, str):

        def repl(m: re.Match[str]) -> str:
            key = m.group(1)
            if key not in params:
                return m.group(0)
            return str(params[key])

        # 整串恰好是一个占位时，保留原类型
        m_full = re.fullmatch(r"\{\{(\w+)\}\}", obj)
        if m_full and m_full.group(1) in params:
            return params[m_full.group(1)]
        return _PLACEHOLDER.sub(repl, obj)
    if isinstance(obj, list):
        return [render_template(x, params) for x in obj]
    if isinstance(obj, dict):
        return {k: render_template(v, params) for k, v in obj.items()}
    return obj


def extract_by_paths(data: Any, paths: dict[str, str]) -> dict[str, Any]:
    """极简 JSONPath：仅支持 $.a.b 与 $.a[].b（取数组映射）。"""
    out: dict[str, Any] = {}
    for key, path in (paths or {}).items():
        out[key] = _resolve_path(data, path)
    return out


def _resolve_path(data: Any, path: str) -> Any:
    if not path or path == "$":
        return data
    if path.startswith("$."):
        path = path[2:]
    elif path.startswith("$"):
        path = path[1:].lstrip(".")
    cur: Any = data
    for part in path.split("."):
        if cur is None:
            return None
        if part.endswith("[]"):
            name = part[:-2]
            if name:
                if not isinstance(cur, dict):
                    return None
                cur = cur.get(name)
            if not isinstance(cur, list):
                return None
            # 后续字段在下一轮处理：这里若是最后一段，返回整列表
            continue
        if "[]" in part:
            # items[].id
            name, _, rest = part.partition("[]")
            rest = rest.lstrip(".")
            if name:
                if not isinstance(cur, dict):
                    return None
                cur = cur.get(name)
            if not isinstance(cur, list):
                return None
            if not rest:
                return cur
            return [_resolve_path(item, rest) for item in cur]
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if 0 <= idx < len(cur) else None
        else:
            return None
    return cur
