#!/usr/bin/env python3
"""Trigger a live friend request from the RUNNING AZIM-MATCH process.

This script does NOT log in and does NOT touch the game/UDP sockets.  It only
writes friend_request_trigger.json; the already running Main.py picks it up
with its live in-memory session tokens and answers in
friend_request_result.json.

Usage:
    python3 send_friend_once.py [target_uid]     (default 15563343778)
"""

import asyncio
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
TRIGGER = os.path.join(BASE, "friend_request_trigger.json")
RESULT = os.path.join(BASE, "friend_request_result.json")
DEFAULT_TARGET = "15563343778"
WAIT_SEC = 180.0


async def main():
    target = str(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TARGET).strip()
    if not target.isdigit():
        print(f"[FRIEND-ONCE] target must be numeric: {target}")
        return 1

    # A stale result would fool the wait below - drop it first.
    try:
        os.remove(RESULT)
    except OSError:
        pass

    with open(TRIGGER, "w", encoding="utf-8") as f:
        json.dump({"target": target, "created_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                  f, indent=1)
    print(f"[FRIEND-ONCE] trigger written -> target {target}")
    print("[FRIEND-ONCE] waiting for the running bot (workers are NOT disturbed) ...")

    deadline = time.time() + WAIT_SEC
    while time.time() < deadline:
        if os.path.exists(RESULT):
            try:
                with open(RESULT, "r", encoding="utf-8") as f:
                    out = json.load(f)
            except Exception as e:
                print(f"[FRIEND-ONCE] result unreadable: {e}")
                return 1
            try:
                os.remove(RESULT)
            except OSError:
                pass

            if out.get("error"):
                print(f"[FRIEND-ONCE] ERROR - {out['error']}")
                return 1

            print(f"\n[FRIEND-ONCE] target {out.get('target')} -> "
                  f"{out.get('ok')} OK / {out.get('failed')} FAILED "
                  f"at {out.get('finished_at')}")
            for aid, r in (out.get("results") or {}).items():
                status = "OK  " if r.get("ok") else "FAIL"
                print(f"  [{status}] {aid} - {r.get('detail')}")
            
            failed_count = int(out.get("failed") or 0)
            return 1 if failed_count > 0 else 0
        await asyncio.sleep(1.0)

    if os.path.exists(TRIGGER):
        print("[FRIEND-ONCE] TIMEOUT - the AZIM-MATCH process never picked up "
              "the trigger (bot not running or old code).")
    else:
        print("[FRIEND-ONCE] TIMEOUT - trigger was consumed but no result "
              "was written; check match.log for [FRIEND-TRIGGER].")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))