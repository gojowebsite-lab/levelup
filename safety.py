# -*- coding: utf-8 -*-
"""
Ban-safe login rails for AZIM-MATCH.

Mirrors the rules that keep AZIM-BOT's accounts alive:

  * ONE ACCOUNT = ONE SESSION.  A second MajorLogin of the same uid is what
    Garena sees as "same account on 2 devices" -> account ban.
  * MAJOR_LOGIN_GAP (default 5 days): no new MajorLogin inside the window,
    the cached session is reused instead.
  * Garena ban verdict is printed BEFORE the login is attempted, so every
    failure says BANNED vs NOT BANNED.
  * The 4 accounts are logged in one after another, never at the same time.
"""

import json
import os
import random
import time
from typing import Optional, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(BASE_DIR, "major_login_history.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

BAN_API = "https://ff.garena.com/api/antihack/check_banned?lang=en&uid={uid}"

_memo = None
_memo_ts = 0.0
_MEMO_TTL = 5.0


# ---------------------------------------------------------------- config ----
def load_config() -> dict:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def safety_cfg() -> dict:
    cfg = load_config().get("safety", {})
    return cfg if isinstance(cfg, dict) else {}


def major_login_gap() -> float:
    try:
        return float(safety_cfg().get("major_login_gap_days", 5)) * 86400.0
    except Exception:
        return 5 * 86400.0


def login_stagger() -> float:
    try:
        return float(safety_cfg().get("login_stagger_sec", 18))
    except Exception:
        return 18.0


# ---------------------------------------------------------------- history ---
def _load_history() -> dict:
    global _memo, _memo_ts
    now = time.time()
    if _memo is not None and (now - _memo_ts) < _MEMO_TTL:
        return _memo
    data = {}
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                parsed = json.load(f)
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            data = {}
    _memo, _memo_ts = data, now
    return data


def _save_history(data: dict) -> None:
    global _memo, _memo_ts
    try:
        tmp = HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, HISTORY_FILE)
        _memo, _memo_ts = data, time.time()
    except Exception as e:
        print(f"[SAFETY] could not write login history: {e}")


def last_major_login(uid) -> float:
    return float(_load_history().get(str(uid), {}).get("last_login", 0) or 0)


def record_major_login(uid, account_id: Optional[str] = None, note: str = "") -> None:
    data = _load_history()
    data[str(uid)] = {
        "last_login": time.time(),
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "account_id": str(account_id or ""),
        "note": note,
    }
    _save_history(data)
    print(f"[SAFETY] MajorLogin recorded for uid {uid} "
          f"(next allowed in {major_login_gap() / 86400:.0f} days)")


def major_login_allowed(uid) -> Tuple[bool, str]:
    """False when this uid already produced a MajorLogin inside the window."""
    last = last_major_login(uid)
    if last <= 0:
        return True, "no MajorLogin recorded yet"
    age = time.time() - last
    gap = major_login_gap()
    if age < gap:
        left = (gap - age) / 86400.0
        return False, (f"uid {uid} logged in {age / 3600:.1f} h ago - a second "
                       f"MajorLogin inside {gap / 86400:.0f} days = 2 sessions "
                       f"= ban. Wait {left:.1f} more days or reuse the cache.")
    return True, f"last MajorLogin {age / 86400:.1f} days ago"


# ------------------------------------------------------------ uid -> acc ----
_uid_map = None


def uid_to_account_id(uid) -> Optional[str]:
    global _uid_map
    if _uid_map is None:
        _uid_map = {}
        base = os.path.dirname(os.path.abspath(__file__))
        for cand in (os.path.join(base, "DARK_MASTER.json"),
                     os.path.join(base, "DARK_MASTER", "DARK_MASTER.json"),
                     "/sdcard/downloads/DARK_MASTER/DARK_MASTER.json",
                     "/sdcard/download/DARK_MASTER/DARK_MASTER.json"):
            try:
                with open(cand, "r", encoding="utf-8") as f:
                    rows = json.load(f)
                for row in rows if isinstance(rows, list) else []:
                    if row.get("uid") and row.get("account_id"):
                        _uid_map[str(row["uid"])] = str(row["account_id"])
            except Exception:
                pass
    return _uid_map.get(str(uid))


async def ban_verdict(account_id) -> Tuple[int, str]:
    """(1, period) banned / (0, '') clean / (-1, reason) could not tell."""
    if not account_id:
        return -1, "no account_id"
    url = BAN_API.format(uid=account_id)
    try:
        import httpx
        async with httpx.AsyncClient(verify=False, timeout=12) as client:
            r = await client.get(url, headers={"User-Agent": "okhttp/3.12.13"})
            if r.status_code != 200:
                return -1, f"HTTP {r.status_code}"
            data = r.json()
            if not isinstance(data, dict) or "is_banned" not in data:
                return -1, f"unexpected body {str(data)[:80]}"
            return int(data.get("is_banned", 0)), str(data.get("period", "") or "")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


async def pre_login_guard(uid, account_id: Optional[str] = None,
                          check_ban: bool = True) -> Tuple[bool, str]:
    """Called right before a MajorLogin.  (False, why) stops the login."""
    ok, why = major_login_allowed(uid)
    if not ok:
        return False, why

    if not check_ban:
        return True, why

    aid = account_id or uid_to_account_id(uid)
    verdict, period = await ban_verdict(aid)
    if verdict == 1:
        return False, (f"Garena says this account IS BANNED (account_id {aid}"
                       f"{', period ' + period if period else ''}) - it can never "
                       f"log in with any token.")
    if verdict == 0:
        print(f"[SAFETY] Garena check: uid {uid} NOT banned (is_banned=0)")
    else:
        print(f"[SAFETY] ban check inconclusive for uid {uid}: {period}")
    return True, why


async def stagger_login(index: int) -> None:
    """Log the 4 accounts one after another, never at the same time."""
    if index <= 0:
        return
    delay = login_stagger() + random.uniform(0.0, 9.0)
    print(f"[SAFETY] waiting {delay:.0f}s before login #{index + 1} "
          f"(accounts never log in together)")
    await _sleep(delay)


async def _sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)
