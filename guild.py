# -*- coding: utf-8 -*-
"""
Guild join for AZIM-MATCH  (RequestJoinClan)

Pure HTTP - it does NOT open a second game session, so it is safe for the
accounts.  The payload is AES-CBC encrypted exactly like the game client
does it, then posted as the raw encrypted bytes with the account token
as Bearer.

The guild only accepts accounts that are far enough along - that is why the
play loop stops at config.json -> level_goal (8) and waits for the guild id
in the terminal.
"""

import json
from typing import List, Optional, Tuple

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

# fixed API key used by the game client for HTTP payloads (same as
# Freefire-TCP-BOT-main encrypt_api)
_API_KEY = bytes([89, 103, 38, 116, 99, 37, 68, 69, 117, 104, 54, 37, 90, 99, 94, 56])
_API_IV = bytes([54, 111, 121, 90, 68, 114, 50, 50, 69, 51, 121, 99, 104, 106, 77, 37])

FALLBACK_URLS = [
    "https://clientbp.common.ggbluefox.com/RequestJoinClan",
    "https://client.ind.freefiremobile.com/RequestJoinClan",
    "https://clientbp.ggblueshark.com/RequestJoinClan",
]


def encrypt_api(plain_hex: str) -> str:
    """PKCS7 pad + AES-CBC -> hex (exactly what the game client posts)."""
    cipher = AES.new(_API_KEY, AES.MODE_CBC, _API_IV)
    return cipher.encrypt(pad(bytes.fromhex(plain_hex), AES.block_size)).hex()


def _varint(n: int) -> str:
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


def payload_formats(guild_id) -> List[str]:
    """Build join payloads, prioritising the format from NoPcLogo.zip.

    The reference ZIP's encrypted body decrypts to ``08 <guild-id varint>``:
    protobuf field 1, varint.  Its bundled value is hardcoded, so only the
    wire format is reused here; the requested guild ID remains dynamic.
    """
    gid = int(str(guild_id).strip())
    raw = str(gid).encode("utf-8")
    return [
        f"08{_varint(gid)}",                   # reference ZIP format
        f"0a{_varint(len(raw))}{raw.hex()}",   # field 1, string  (most likely)
        f"12{_varint(len(raw))}{raw.hex()}",   # field 2, string
        f"10{_varint(gid)}",                   # field 2, varint
    ]


def _headers(token: str, release_version: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 11; ASUS_Z01QD Build/PI)",
        "X-Unity-Version": "2018.4.11f1",
        "X-GA": "v1 1",
        "ReleaseVersion": release_version or "OB55",
        "Connection": "Keep-Alive",
        "Accept-Encoding": "gzip",
    }


def _urls_for(server_url: Optional[str]) -> List[str]:
    urls = []
    if server_url:
        base = str(server_url).rstrip("/")
        if base:
            urls.append(f"{base}/RequestJoinClan")
    urls += [u for u in FALLBACK_URLS if u not in urls]
    return urls


async def join_guild(token: str, guild_id, server_url: Optional[str],
                     release_version: Optional[str], label: str = "",
                     extra_tokens: Optional[List[str]] = None) -> Tuple[bool, str]:
    """Try every payload format against every endpoint.

    Returns (True, detail) on the first 200/201, otherwise (False, last error).
    """
    tokens = [t for t in [token, *(extra_tokens or [])] if t]
    if not tokens:
        return False, "no token available for the account"
    if not guild_id:
        return False, "no guild id"

    urls = _urls_for(server_url)
    formats = payload_formats(guild_id)
    last = "no attempt made"

    for tok in tokens:
        hdrs = _headers(tok, release_version)
        for idx, plain in enumerate(formats, 1):
            enc = encrypt_api(plain)
            body = bytes.fromhex(enc)
            for url in urls:
                try:
                    async with httpx.AsyncClient(verify=False, timeout=15) as client:
                        r = await client.post(url, content=body, headers=hdrs)
                    detail = (f"[GUILD] {label} fmt#{idx} -> {url.split('//')[1][:38]} "
                              f"| guild {guild_id} | HTTP {r.status_code} "
                              f"| {len(r.content)} bytes")
                    if r.status_code in (200, 201):
                        print(f"{detail}  JOIN REQUEST ACCEPTED")
                        return True, detail
                    last = detail
                    print(detail)
                    if r.status_code in (401, 403):
                        # token rejected - the next token is worth a try
                        last = f"{detail} | token rejected"
                        break
                except Exception as e:
                    last = f"[GUILD] {label} fmt#{idx} -> {url[:48]} error: {e}"
                    print(last)
            else:
                continue
            break  # token rejected -> try the next token

    return False, last


def guild_id_is_valid(guild_id) -> bool:
    try:
        return int(str(guild_id).strip()) > 0
    except Exception:
        return False


async def load_account_tokens(uid: str) -> dict:
    """token / access_token / server_url of a logged-in account (no new login)."""
    try:
        from dashboard_server import bot_state
    except Exception:
        return {}
    creds = bot_state.account_credentials.get(str(uid)) or {}
    if creds:
        return creds
    for key, data in bot_state.account_credentials.items():
        if str(data.get("auth_uid")) == str(uid):
            return data
    return {}
