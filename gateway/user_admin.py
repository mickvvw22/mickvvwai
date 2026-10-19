"""用户管理（在线人员 / 禁言 / 每人专属提示词）—— 2026-10-19 新增。

为什么独立成模块：这些状态必须**持久化**（重启网关不能丢），且入站路径
（gateway/run_inbound.py）与命令处理（gateway/slash_commands_model.py）都要读它，
所以放在两边都能 import 的地方，不塞进 mixin。

存储（都在 $HERMES_HOME/gateway/，600 权限，JSON）：
  * users.json        —— 已知用户：{chat_id: {"name":..., "note":..., "first_seen":..., "last_seen":...}}
  * user_blocks.json  —— 禁言：{chat_id: {"until": <epoch秒>, "reason":..., "by":...}}
  * user_souls.json   —— 每人专属提示词：{chat_id: "<souls/ 下的名字>"}
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

DURATION_RE = re.compile(r"^(\d+)\s*(s|m|h|d|秒|分|分钟|小时|天)?$", re.I)


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def _dir() -> Path:
    d = hermes_home() / "gateway"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read(name: str) -> dict:
    p = _dir() / name
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write(name: str, data: dict) -> None:
    p = _dir() / name
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(p)


# ---------------- 用户 ----------------

def remember_user(chat_id: Any, name: str = "", platform: str = "", note: str = "") -> None:
    """记下见过的用户（/pusers 靠它显示名字）。永不抛异常。"""
    try:
        cid = str(chat_id or "").strip()
        if not cid:
            return
        users = _read("users.json")
        entry = users.get(cid) or {}
        now = int(time.time())
        entry.setdefault("first_seen", now)
        entry["last_seen"] = now
        if name:
            entry["name"] = name
        if platform:
            entry["platform"] = platform
        if note:
            entry["note"] = note
        users[cid] = entry
        _write("users.json", users)
    except Exception:
        pass


def get_users() -> dict:
    return _read("users.json")


def claim_block_notice(chat_id: Any, cooldown: int = 300) -> bool:
    """禁言通知去重：距上次通知超过 cooldown 秒才返回 True（并记下时间）。"""
    try:
        users = _read("users.json")
        cid = str(chat_id)
        entry = users.get(cid) or {}
        now = int(time.time())
        if now - int(entry.get("block_notified_at") or 0) < cooldown:
            return False
        entry["block_notified_at"] = now
        users[cid] = entry
        _write("users.json", users)
        return True
    except Exception:
        return True


def set_note(chat_id: Any, note: str) -> None:
    try:
        users = _read("users.json")
        cid = str(chat_id)
        entry = users.get(cid) or {}
        entry["note"] = note
        users[cid] = entry
        _write("users.json", users)
    except Exception:
        pass


# ---------------- 禁言（强制下线一段时间） ----------------

def parse_duration(text: str) -> Optional[int]:
    """'30'/'30m'/'2h'/'1d'/'90分钟' → 秒；无法解析返回 None。"""
    t = (text or "").strip().lower()
    if not t:
        return None
    m = DURATION_RE.match(t)
    if not m:
        return None
    n = int(m.group(1))
    unit = (m.group(2) or "m").lower()
    mult = {"s": 1, "秒": 1, "m": 60, "分": 60, "分钟": 60,
            "h": 3600, "小时": 3600, "d": 86400, "天": 86400}[unit]
    return n * mult


def set_block(chat_id: Any, seconds: Optional[int], reason: str = "", by: str = "") -> Optional[int]:
    """禁言/拉黑。

    ``seconds=None`` → **永久拉黑**（直到管理员手动恢复），返回 None；
    否则禁言到 now+seconds，返回到期 epoch。
    """
    blocks = _read("user_blocks.json")
    until = None if seconds is None else int(time.time()) + max(0, int(seconds))
    blocks[str(chat_id)] = {"until": until, "reason": reason, "by": by,
                            "set_at": int(time.time()), "permanent": until is None}
    _write("user_blocks.json", blocks)
    return until


def is_permanent(entry: dict) -> bool:
    return bool(entry) and (entry.get("permanent") is True or entry.get("until") is None)


def clear_block(chat_id: Any) -> bool:
    blocks = _read("user_blocks.json")
    existed = str(chat_id) in blocks
    blocks.pop(str(chat_id), None)
    _write("user_blocks.json", blocks)
    return existed


def block_status(chat_id: Any) -> Optional[dict]:
    """未禁言/已到期返回 None；仍在禁言返回 {"until","remaining","permanent","reason","by"}。

    永久拉黑时 ``until``/``remaining`` 为 None，``permanent`` 为 True。
    """
    try:
        blocks = _read("user_blocks.json")
        cid = str(chat_id)
        entry = blocks.get(cid)
        if not entry:
            return None
        if is_permanent(entry):
            return {"until": None, "remaining": None, "permanent": True,
                    "reason": entry.get("reason") or "", "by": entry.get("by") or ""}
        until = int(entry.get("until") or 0)
        now = int(time.time())
        if until <= now:
            blocks.pop(cid, None)
            _write("user_blocks.json", blocks)
            return None
        return {"until": until, "remaining": until - now, "permanent": False,
                "reason": entry.get("reason") or "", "by": entry.get("by") or ""}
    except Exception:
        return None


def list_blocks() -> dict:
    """只返回仍在生效的禁言。"""
    out = {}
    for cid in list(_read("user_blocks.json").keys()):
        st = block_status(cid)
        if st:
            out[cid] = st
    return out


def human_duration(seconds: Optional[int]) -> str:
    """秒 → 人话。``None`` = 永久（拉黑不过期）。"""
    if seconds is None:
        return "永久"
    s = max(0, int(seconds))
    if s >= 86400:
        return f"{s // 86400} 天 {(s % 86400) // 3600} 小时"
    if s >= 3600:
        return f"{s // 3600} 小时 {(s % 3600) // 60} 分"
    if s >= 60:
        return f"{s // 60} 分钟"
    return f"{s} 秒"


def blocked_members() -> list:
    """当前被下线/拉黑的成员：[{"chat_id","name","status"}]（给选择器与恢复菜单用）。"""
    users = get_users()
    out = []
    for cid, st in list_blocks().items():
        out.append({"chat_id": cid,
                    "name": (users.get(cid, {}) or {}).get("name") or cid,
                    "status": st})
    return out


# ---------------- 每人专属提示词 ----------------

def soul_map() -> dict:
    return _read("user_souls.json")


def set_user_soul(chat_id: Any, soul_name: Optional[str]) -> bool:
    m = soul_map()
    cid = str(chat_id)
    if soul_name:
        m[cid] = str(soul_name)
    else:
        m.pop(cid, None)
    _write("user_souls.json", m)
    return True


def soul_file_for(chat_id: Any) -> Optional[Path]:
    """该用户专属提示词的路径（存在才返回），否则 None → 用全局 SOUL.md。"""
    try:
        name = soul_map().get(str(chat_id))
        if not name:
            return None
        safe = Path(str(name)).name
        p = hermes_home() / "souls" / f"{safe}.md"
        return p if p.exists() else None
    except Exception:
        return None


def available_souls() -> list:
    try:
        d = hermes_home() / "souls"
        if not d.is_dir():
            return []
        return sorted(p.stem for p in d.glob("*.md"))
    except Exception:
        return []