# -*- coding: utf-8 -*-
"""
Friend request to the boss account (RequestAddingFriend).

Pure HTTP with the token of the already logged in account - it does NOT open
a new session, so it is safe for the accounts.  Sent once per account, right
after the login.  "duplicate / already / exist" in the answer counts as
success: the boss only has to accept it on his side.
"""

import asyncio
import json
import os
from typing import Tuple

import httpx

import GetFriend_Res_pb2
from guild import encrypt_api

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

FALLBACK_URLS = [
    "https://clientbp.ppmainecoonghj.com/RequestAddingFriend",
    "https://clientbp.common.ggbluefox.com/RequestAddingFriend",
    "https://client.ind.freefiremobile.com/RequestAddingFriend",
]

_sent = {}          # account uid -> True once the request went through


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out).hex()


def friend_payload(target_uid) -> str:
    """08 <fixed> 10 <varint target uid> 18 01 - the bytes the game sends."""
    return "08a7c4839f1e10" + _varint(int(target_uid)) + "1801"


def load_cfg():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def boss_uid() -> str:
    fr = load_cfg().get("friend", {})
    if isinstance(fr, dict):
        uid = str(fr.get("boss_uid", "") or "").strip()
        if uid:
            return uid
    return "14216035246"


def friend_enabled() -> bool:
    fr = load_cfg().get("friend", {})
    if isinstance(fr, dict):
        return bool(fr.get("enabled", True))
    return True


def _urls_for(server_url):
    urls = []
    if server_url:
        base = str(server_url).rstrip("/")
        if base:
            urls.append(base + "/RequestAddingFriend")
    urls += [u for u in FALLBACK_URLS if u not in urls]
    return urls


def _headers(token, release_version):
    return {
        "Authorization": "Bearer " + str(token),
        "X-Unity-Version": "2018.4.11f1",
        "X-GA": "v1 1",
        "ReleaseVersion": release_version or "OB55",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Dalvik/2.1.0 (Linux; Android 9)",
    }


async def send_friend_request(target_uid, token, server_url=None,
                              release_version=None, label="") -> Tuple[bool, str]:
    """-> (True, detail) when the request was accepted or already exists."""
    target_uid = int(target_uid)
    if not token:
        return False, "no token for this account"

    body = bytes.fromhex(encrypt_api(friend_payload(target_uid)))
    hdrs = _headers(token, release_version)
    last = "no endpoint tried"

    for url in _urls_for(server_url):
        try:
            async with httpx.AsyncClient(verify=False, timeout=20) as client:
                r = await client.post(url, content=body, headers=hdrs)
            text = (r.text or "").strip()
            low = text.lower()
            detail = ("[FRIEND] " + str(label) + " -> " + url.split("//")[1][:40]
                      + " | HTTP " + str(r.status_code) + " | " + text[:70])
            if "duplicate" in low or "already" in low or "exist" in low:
                print(detail + "   (already friends / already pending)")
                return True, "already friends or pending: " + text[:60]
            if r.status_code == 200:
                print(detail + "   REQUEST SENT - boss must accept")
                return True, "friend request sent"
            last = detail
            print(detail)
        except Exception as e:
            last = ("[FRIEND] " + str(label) + " -> " + url[:48]
                    + " error: " + type(e).__name__ + ": " + str(e))
            print(last)
    return False, last


async def send_to_boss(account_data, label="") -> Tuple[bool, str]:
    """One friend request per account per run, best effort."""
    if not friend_enabled():
        return False, "friend requests are disabled in config.json"
    boss = boss_uid()
    if not boss:
        return False, "no friend.boss_uid in config.json"
    key = str(label or (account_data or {}).get("auth_uid") or "?")
    if _sent.get(key):
        return True, "already sent earlier in this run"
    ok, detail = await send_friend_request(
        boss,
        (account_data or {}).get("token"),
        (account_data or {}).get("server_url"),
        (account_data or {}).get("release_version"),
        label=key,
    )
    if ok:
        _sent[key] = True
        print("[FRIEND] " + key + " -> boss " + str(boss) + " done (1 request per account)")
    return ok, detail


# ====================== AZIM-MATCH: friends each other =====================
#
# The accounts of this round send a friend request to EVERY other account
# and afterwards the friend list is really fetched to verify it - a request
# on its own proves nothing.

async def request_all(targets, account_data, label="", gap=1.0) -> dict:
    """One friend request per target -> {target: (ok, detail)}."""
    out = {}
    for t in targets:
        ok, detail = await send_friend_request(
            t,
            (account_data or {}).get("token"),
            (account_data or {}).get("server_url"),
            (account_data or {}).get("release_version"),
            label=f"{label}->{t}",
        )
        out[str(t)] = (ok, detail)
        await asyncio.sleep(gap)
    return out


def _varint_hex(v) -> str:
    buf, v = [], int(v)
    while True:
        t = v & 0x7F
        v >>= 7
        if v:
            buf.append(t | 0x80)
        else:
            buf.append(t)
            break
    return "".join(f"{b:02x}" for b in buf)


def _getfriend_urls(server_url):
    urls = []
    base = str(server_url or "").rstrip("/")
    if base:
        urls.append(base + "/GetFriend")
    urls += [
        "https://clientbp.ppmainecoonghj.com/GetFriend",
        "https://clientbp.common.ggbluefox.com/GetFriend",
        "https://client.ind.freefiremobile.com/GetFriend",
    ]
    out, seen = [], set()
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


async def get_friend_list(token, account_id, server_url=None, release_version=None):
    """-> ([(uid, name), ...] | None, error).  Pure check - writes nothing."""
    if not token:
        return None, "no token for this account"
    try:
        body = bytes.fromhex(encrypt_api("08" + _varint_hex(int(account_id))))
    except Exception as e:
        return None, f"payload build failed: {type(e).__name__}: {e}"
    hdrs = _headers(token, release_version)
    last = "no endpoint tried"

    for url in _getfriend_urls(server_url):
        try:
            async with httpx.AsyncClient(verify=False, timeout=20) as client:
                r = await client.post(url, content=body, headers=hdrs)
            short = url.split("//")[1][:44]
            if r.status_code != 200:
                last = f"HTTP {r.status_code} @ {short}"
                print(f"[FRIEND] GetFriend {short} -> {last}")
                continue
            raw = r.content
            try:
                from guild import decrypt_api as _dec
                raw = bytes.fromhex(_dec(raw.hex()))
            except Exception:
                raw = r.content          # GetFriend often answers plain protobuf
            p = GetFriend_Res_pb2.GetFriend()
            p.ParseFromString(raw)
            seen, out = set(), []
            for f in p.PlayerList:
                u = str(f.PlayerUid)
                if u and u != "0" and u not in seen:
                    seen.add(u)
                    out.append((u, f.PlayerName or u))
            return out, ""
        except Exception as e:
            last = f"{type(e).__name__}: {e} @ {url.split('//')[1][:44]}"
    return None, last


async def check_friends(account_data, targets, label="", tries=4, delay=6.0):
    """Look the friend list up until every target is in it (or tries run out).

    -> (present, missing, friends, err)
    """
    targets = [str(t) for t in targets]
    present, missing, friends, err = [], list(targets), [], ""
    for i in range(1, tries + 1):
        friends, err = await get_friend_list(
            (account_data or {}).get("token"),
            (account_data or {}).get("account_id"),
            (account_data or {}).get("server_url"),
            (account_data or {}).get("release_version"),
        )
        if friends is None:
            print(f"[FRIEND] {label}: friend list try {i}/{tries} failed: {err}")
            if i < tries:
                await asyncio.sleep(delay)
            continue
        have = {u for u, _ in friends}
        present = [t for t in targets if t in have]
        missing = [t for t in targets if t not in have]
        print(f"[FRIEND] {label}: list has {len(friends)} entr(y/ies) - "
              f"{len(present)}/{len(targets)} of this round found (try {i}/{tries})"
              + (f"  missing: {', '.join(missing)}" if missing else ""))
        if not missing:
            return present, missing, friends, ""
        if i < tries:
            await asyncio.sleep(delay)
    return present, missing, friends, err or ("missing: " + ", ".join(missing))
