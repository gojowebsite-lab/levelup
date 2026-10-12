# -*- coding: utf-8 -*-
"""
Group (squad) formation for AZIM-MATCH - 4 accounts in ONE group.

All 4 bots run in one process, so a plain shared state is enough:

  account #0 (leader)
      1. creates the squad on its own OnLine connection
      2. waits until the other 3 are connected
      3. invites the other 3 by uid
      4. waits until they all accepted

  accounts #1..#3
      1. wait for the leader
      2. read the invite (field 5.8, owner in field 5.1)
      3. answer it with the join packet

Best effort: on timeout or a refusal ("Protection Bypass") the bots keep
going with the matchmaking, because the level / guild goal still has to be
reached.  Every step is printed so it can be verified in the log.
"""

import asyncio
import json
import os
import re
import time

import xC4
from protobuf_decoder.protobuf_decoder import Parser

DEFAULT_FORM_TIMEOUT = 60.0
REFERENCE_JOIN_CLIENT_VERSION = "1.111.1"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class SquadState:
    def __init__(self):
        self.enabled = True
        # One-account test mode: the human joins the printed public code.
        self.test_mode = False
        self.leader_uid = None                 # account_id of the leader
        self.member_uids = []                  # account_ids of #1..#3
        self.leader_connected = asyncio.Event()
        self.members_connected = set()
        self.all_members_connected = asyncio.Event()
        self.invites_sent = asyncio.Event()
        self.joined = set()                    # account_ids inside the squad
        self.failed = {}                       # account_id -> reason
        self.code = None
        self.formed = False
        self.attempts = 0
        self.attempted = set()      # uid -> already tried once this round
        self.expected = 0           # accounts in this round (0 = unknown)
        self.targets = []           # who the leader invited
        self.started_at = time.time()

        # --- AZIM-MATCH: is the group REALLY there? -----------------------
        # Sending a join packet proves nothing: the log said "2/2" while the
        # game still showed 1/4.  So every account listens after create /
        # invite / join and remembers every OTHER account id the server
        # echoed back to it.  Only that counts as a group.
        self.evidence = set()       # uids the server really sent back to us
        self.seen_by = {}           # observer uid -> uids it saw in frames
        self.group_counts = {}      # observer uid -> server-reported team size
        self.stage_gate = asyncio.Event()   # every squad stage finished
        self.stage_expect = 0        # how many accounts may still enter
        self.done_count = 0          # how many finished this round
        self.decision = None         # "separate" | "pause" | "stop"
        self.verified = False
        self.friend_blocked = False  # friendship not confirmed -> no team
        # Optional human/boss player invited after the bot accounts form a
        # real team.  The boss joins from the official game client.
        self.boss_uid = None
        self.boss_invited = False
        self.boss_verified = False
        self.boss_gate = asyncio.Event()

    def reset_for_round(self):
        """Called when the group has to be rebuilt (new play loop)."""
        self.formed = False
        self.attempts = 0
        self.started_at = time.time()
        self.evidence = set()
        self.seen_by = {}
        self.group_counts = {}
        self.stage_gate = asyncio.Event()
        self.stage_expect = 0
        self.done_count = 0
        self.decision = None
        self.verified = False
        self.friend_blocked = False
        self.boss_invited = False
        self.boss_verified = False
        self.boss_gate = asyncio.Event()


SQUAD = SquadState()


def members_needed():
    """How many of the OTHER accounts must join before GROUP READY."""
    if SQUAD.expected and SQUAD.expected > 1:
        return SQUAD.expected - 1
    return max(1, len(SQUAD.member_uids) - 1)


# ---------------------------------------------- AZIM-MATCH: real check -----
def frame_mentions(frame, uid) -> bool:
    """True when a decoded frame carries this account id anywhere in it."""
    want = str(uid)
    stack = [frame]
    guard = 0
    while stack and guard < 8000:
        node = stack.pop()
        guard += 1
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "data":
                    if isinstance(v, dict):
                        stack.append(v)
                    elif v is not None and str(v) == want:
                        return True
                elif isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(node, list):
            stack.extend(node)
    return False


def record_evidence(frames, watching, observer):
    """Remember which OTHER account ids the server echoed back to `observer`."""
    found = []
    bucket = SQUAD.seen_by.setdefault(str(observer), set())
    for uid in watching:
        uid = str(uid)
        if not uid or uid == str(observer) or uid in bucket:
            continue
        if any(frame_mentions(f, uid) for f in frames):
            bucket.add(uid)
            SQUAD.evidence.add(uid)
            found.append(uid)
    # The newer reference parser reads the live squad count from
    # 5 -> 1 -> 9.  Keep it as an independent server-side verification signal.
    counts = []
    for frame in frames:
        count = frame_group_count(frame)
        if count is not None:
            counts.append(count)
    if counts:
        observed = max(counts)
        previous = int(SQUAD.group_counts.get(str(observer), 0) or 0)
        SQUAD.group_counts[str(observer)] = max(previous, observed)
        print(f"[SQUAD] {observer}: server GROUP COUNT {observed}/4")
    return found


def frame_group_count(frame):
    """Extract the server's squad count used by the new reference ZIP."""
    try:
        d5 = frame.get("5", {}).get("data", {})
        d1 = d5.get("1", {}).get("data", {})
        value = d1.get("9", {}).get("data")
        value = int(value)
        return value if 1 <= value <= 4 else None
    except (AttributeError, TypeError, ValueError):
        return None


def _all_members():
    """Every account of the round except the leader."""
    if SQUAD.targets:
        return [t for t in SQUAD.targets if t]
    return [m for m in SQUAD.member_uids if m != SQUAD.leader_uid]


def _linked(member):
    """The leader AND this member observed each other in a server frame."""
    leader = str(SQUAD.leader_uid or "")
    if not leader or not member:
        return False
    if member in SQUAD.seen_by.get(leader, set()):
        return True                       # the leader saw the member join
    if leader in SQUAD.seen_by.get(member, set()):
        return True                       # the member saw the team it joined
    return False


def team_verified():
    """Did the SERVER put them in one group?  Not: did we send a join?"""
    if int(SQUAD.expected or 0) <= 1:
        return True                     # solo round - nothing to verify
    # Do not accept a partial team.  A previous run reported 2/3 as ready,
    # which caused the last account to skip its code-join stage.
    if len(SQUAD.member_uids) < int(SQUAD.expected):
        return False
    members = _all_members()
    if not members:
        return False
    if all(_linked(str(m)) for m in members):
        return True
    # Some server frames expose the authoritative count but omit one member's
    # UID.  Accept that only when the server reports the complete selected
    # roster size; a join packet alone never populates group_counts.
    return any(int(v or 0) >= int(SQUAD.expected)
               for v in SQUAD.group_counts.values())


def missing_members():
    return [str(m) for m in _all_members() if not _linked(str(m))]


def team_size():
    """1 leader + every member that is really linked to it."""
    if int(SQUAD.expected or 0) <= 1:
        return max(1, int(SQUAD.expected or 1))
    return 1 + sum(1 for m in _all_members() if _linked(str(m)))


def who_saw_whom():
    return {o: sorted(s) for o, s in SQUAD.seen_by.items() if s}


async def team_verdict(wait=60.0):
    """Wait for every squad stage, then decide if the group is real."""
    if SQUAD.verified:
        return True                      # proven earlier in this round
    if SQUAD.decision:
        wait = min(wait, 5.0)            # the team question was already asked

    if not SQUAD.stage_gate.is_set():
        try:
            await asyncio.wait_for(SQUAD.stage_gate.wait(), timeout=wait)
        except (asyncio.TimeoutError, TimeoutError):
            pass

    ok = team_verified()
    SQUAD.verified = ok
    have = sorted(SQUAD.evidence)
    if ok:
        if int(SQUAD.expected or 0) > 1:
            print(f"[SQUAD] TEAM CHECK OK - the server sent back "
                  f"{', '.join(have)} -> they ARE in one group")
    else:
        miss = missing_members()
        print(f"[SQUAD] TEAM CHECK FAILED - the server never sent back: "
              f"{', '.join(miss) or '?'}")
        print(f"[SQUAD]          ids we did see in a team frame: "
              f"{', '.join(have) or 'none'}")
        print(f"[SQUAD]          (join packet sent =! group exists - "
              f"the game still shows 1/4)")
    return ok


# --------------------------------------------------------------- decode -----
def _parse_node(node):
    out = {}
    for r in node:
        entry = {"wire_type": r.wire_type}
        try:
            if r.wire_type in ("varint", "string", "bytes"):
                entry["data"] = r.data
            elif r.wire_type == "length_delimited":
                entry["data"] = _parse_node(r.data.results)
        except Exception:
            entry["data"] = None
        out[str(r.field)] = entry
    return out


def decode_hex(hex_str):
    try:
        return _parse_node(Parser().parse(hex_str))
    except Exception:
        return None


def brief(frame) -> str:
    """One line of a decoded frame - so we can SEE what the server answers."""
    try:
        return json.dumps(frame, ensure_ascii=False)[:420]
    except Exception:
        return str(frame)[:420]


def find_invite(frame):
    """-> (owner_uid, squad_code) when the frame is a real squad invite."""
    if not isinstance(frame, dict):
        return None, None
    d5 = (frame.get("5") or {}).get("data")
    if not isinstance(d5, dict) or "8" not in d5:
        return None, None
    owner = (d5.get("1") or {}).get("data")
    code = (d5.get("8") or {}).get("data")
    if code is None:
        return None, None
    code = str(code)
    if not ("_" in code or len(code) > 6):
        return None, None          # squad-create answer (code "1"), not an invite
    return (str(owner) if owner is not None else None), code


def find_squad_code(frame):
    """Find the real 7-digit join code in a create-team response.

    Field 5.31 is not the public FF join code on this server/version; it can
    contain an internal value such as ``123..._suffix``.  Search the decoded
    response for a standalone seven-digit numeric value instead.
    """
    if isinstance(frame, dict):
        d5 = (frame.get("5") or {}).get("data")
        if isinstance(d5, dict):
            # The old working flow exposed the public/chat code in 5.14.
            public = (d5.get("14") or {}).get("data")
            if re.fullmatch(r"\d{7}", str(public).strip()):
                return str(public).strip()
    # This server sometimes places the public code outside field 5.14. Keep
    # the older recursive fallback for compatibility with the working codes
    # observed in the live log (for example 1784100).
    def walk(value):
        if isinstance(value, dict):
            for child in value.values():
                found = walk(child)
                if found:
                    return found
        elif isinstance(value, (list, tuple)):
            for child in value:
                found = walk(child)
                if found:
                    return found
        elif isinstance(value, (str, int)):
            candidate = str(value).strip()
            if re.fullmatch(r"\d{7}", candidate):
                return candidate
        return None

    return walk(frame)


def squad_response_codes(frame):
    """Return both response identifiers used by the old leader/member flow."""
    if not isinstance(frame, dict):
        return None, None
    d5 = (frame.get("5") or {}).get("data")
    if not isinstance(d5, dict):
        return None, None
    chat = (d5.get("14") or {}).get("data")
    internal = (d5.get("31") or {}).get("data")
    return (str(chat).strip() if chat is not None else None,
            str(internal).strip() if internal is not None else None)


# ------------------------------------------------------------- builders -----
async def build_create_squad(key, iv, region, client_version, size=4):
    fields = {
        1: 1,
        2: {
            2: chr(1),
            3: 1,
            4: 1,
            5: "en",
            9: 1,
            11: 1,
            13: 1,
            14: {2: 5756, 6: 11, 8: str(client_version or "1.132.9"), 9: 2, 10: int(size)},
        },
    }
    proto = await xC4.CrEaTe_ProTo(fields)
    return await xC4.GeneRaTePk(proto.hex(), xC4.region_packet(region), key, iv)


async def build_invite(target_uid, key, iv, region, size=4):
    fields = {
        1: 2,
        2: {
            1: int(target_uid),
            2: str(region or "").upper(),
            4: int(size),
        },
    }
    proto = await xC4.CrEaTe_ProTo(fields)
    return await xC4.GeneRaTePk(proto.hex(), xC4.region_packet(region), key, iv)


def boss_invite_enabled():
    """Boss is an optional human player, not one of the bot tokens."""
    try:
        with open(os.path.join(BASE_DIR, "config.json"), "r", encoding="utf-8") as f:
            cfg = json.load(f)
        fr = cfg.get("friend", {}) if isinstance(cfg, dict) else {}
        sq = cfg.get("squad", {}) if isinstance(cfg, dict) else {}
        return bool(sq.get("invite_boss", True)) and bool(fr.get("boss_uid"))
    except Exception:
        return False


def manual_human_team_enabled():
    """Whether the leader waits for a human to use the printed team code."""
    if SQUAD.test_mode:
        return True
    try:
        with open(os.path.join(BASE_DIR, "config.json"), "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return bool(cfg.get("squad", {}).get("manual_human", False))
    except Exception:
        return False


def boss_wait_seconds():
    try:
        with open(os.path.join(BASE_DIR, "config.json"), "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return max(30.0, float(cfg.get("squad", {}).get("boss_wait_sec", 180)))
    except Exception:
        return 180.0


async def invite_boss_and_wait(reader, writer, leader_uid, region, key, iv,
                               timeout=None):
    """Invite the human boss and wait for a server team echo.

    A UID alone cannot tell us whether the human is online.  Repeated invites
    plus a team-frame echo are therefore used as the online/accepted signal.
    No matchmaking is allowed until this returns True.
    """
    if not boss_invite_enabled():
        SQUAD.boss_verified = True
        SQUAD.boss_gate.set()
        return True

    try:
        with open(os.path.join(BASE_DIR, "config.json"), "r", encoding="utf-8") as f:
            cfg = json.load(f)
        boss = str(cfg.get("friend", {}).get("boss_uid", "")).strip()
    except Exception:
        boss = ""
    if not boss:
        SQUAD.boss_verified = True
        SQUAD.boss_gate.set()
        return True

    SQUAD.boss_uid = boss
    wait_for = float(timeout or boss_wait_seconds())
    deadline = time.time() + wait_for
    if SQUAD.code:
        print(f"[SQUAD] TEAM CODE FOR BOSS: {SQUAD.code}")
        print(f"[SQUAD] bots verified -> boss {boss} should join using this code "
              f"in the official Free Fire app ({wait_for:.0f}s)")
    else:
        print(f"[SQUAD] bots verified -> inviting boss {boss}; "
              f"boss must be online in Free Fire and accept ({wait_for:.0f}s)")

    last_invite = 0.0
    while time.time() < deadline and not SQUAD.boss_verified:
        now = time.time()
        if not SQUAD.code and now - last_invite >= 15.0:
            try:
                pkt = await build_invite(boss, key, iv, region, size=4)
                if await _send(writer, pkt):
                    SQUAD.boss_invited = True
                    last_invite = now
                    print(f"[SQUAD] boss invite sent -> {boss}")
            except Exception as e:
                print(f"[SQUAD] boss invite failed: {type(e).__name__}: {e}")

        frames, _ = await _read_frames(reader, min(3.0, max(0.2, deadline - time.time())))
        if frames:
            record_evidence(frames, [boss], leader_uid)
            if str(boss) in SQUAD.seen_by.get(str(leader_uid), set()):
                SQUAD.boss_verified = True
                SQUAD.boss_gate.set()
                print(f"[SQUAD] BOSS JOINED - server confirmed {boss} in the team")
                return True

    print(f"[SQUAD] BOSS NOT JOINED - no server confirmation for {boss}")
    SQUAD.boss_gate.set()
    return False


async def wait_for_boss_result(timeout=None):
    if not boss_invite_enabled():
        return True
    try:
        await asyncio.wait_for(SQUAD.boss_gate.wait(), timeout=float(timeout or boss_wait_seconds()) + 5)
    except asyncio.TimeoutError:
        return False
    return bool(SQUAD.boss_verified)


async def build_join_accept(code, key, iv, region, client_version,
                            pkt_type=None):
    """Answer an invite / join by squad code - reference bot structure."""
    skinz = {
        1: 4,
        2: {
            4: bytes.fromhex("01090a0b121920"),
            5: str(code),
            6: 6,
            8: 1,
            9: {2: 800, 6: 11, 8: str(client_version or "1.132.9"), 9: 5, 10: 1},
        },
    }
    proto = await xC4.CrEaTe_ProTo(skinz)
    return await xC4.GeneRaTePk(
        proto.hex(), xC4.region_packet(region, pkt_type), key, iv
    )


# ----------------------------------------------------------- io helpers -----
async def _send(writer, frame):
    try:
        if writer is None or writer.is_closing():
            return False
        writer.write(bytes(frame))
        await writer.drain()
        return True
    except Exception as e:
        print(f"[SQUAD] send failed: {type(e).__name__}: {e}")
        return False


async def _read_frames(reader, seconds, stop_on=None):
    """Read for `seconds`; stop_on(frame) -> True ends it early."""
    deadline = time.time() + seconds
    seen = []
    while time.time() < deadline:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        try:
            data = await asyncio.wait_for(reader.read(8192), timeout=min(1.0, remaining))
        except asyncio.TimeoutError:
            continue
        except Exception:
            break
        if not data:
            break
        hex_data = data.hex()
        frame = decode_hex(hex_data[10:]) if len(hex_data) > 10 else None
        if frame is not None:
            seen.append(frame)
            if stop_on is not None and stop_on(frame):
                return seen, frame
    return seen, None


# ------------------------------------------------------------ the stage -----
async def squad_stage(reader, writer, account_id, region, key, iv,
                      client_version, timeout=DEFAULT_FORM_TIMEOUT):
    """Called right after the OnLine connection is up, before matchmaking."""
    if not SQUAD.enabled:
        return
    if SQUAD.formed:
        return
    if (int(SQUAD.expected or 0)) <= 1:
        return          # a single token - there is nobody to group with

    uid = str(account_id)
    if uid in SQUAD.attempted:
        return          # already tried once - never block matchmaking again
    SQUAD.attempted.add(uid)

    if uid not in SQUAD.member_uids:
        SQUAD.member_uids.append(uid)

    is_leader = (SQUAD.leader_uid is None)
    if is_leader:
        SQUAD.leader_uid = uid

    # --- AZIM-MATCH: no team while the accounts are not friends yet -------
    if SQUAD.friend_blocked:
        print(f"[SQUAD] {uid}: team NOT created - the accounts are not "
              f"confirmed friends of each other (see the [FRIEND] lines above)")
        SQUAD.failed[uid] = "friendship not confirmed"
        SQUAD.done_count += 1
        SQUAD.stage_expect = int(SQUAD.expected or SQUAD.stage_expect or 1)
        if SQUAD.done_count >= max(1, SQUAD.stage_expect):
            SQUAD.stage_gate.set()
        return

    # The verdict is only trustworthy once EVERY account finished this stage,
    # so count them and open the gate when the round is complete.
    SQUAD.stage_expect = int(SQUAD.expected or (len(SQUAD.member_uids) + 1))

    try:
        if is_leader:
            await _leader(reader, writer, uid, region, key, iv, client_version, timeout)
        else:
            await _member(reader, writer, uid, region, key, iv, client_version, timeout)
    except asyncio.TimeoutError:
        print(f"[SQUAD] {uid}: timeout while forming the group -> continue without it")
    except Exception as e:
        print(f"[SQUAD] {uid}: {type(e).__name__}: {e} -> continue without it")
        SQUAD.failed[uid] = f"{type(e).__name__}: {e}"
    finally:
        SQUAD.stage_expect = int(SQUAD.expected or SQUAD.stage_expect or 1)
        SQUAD.done_count += 1
        if SQUAD.done_count >= max(1, SQUAD.stage_expect):
            SQUAD.stage_gate.set()
            print(f"[SQUAD] all {SQUAD.done_count} squad stage(s) finished "
                  f"-> team check can run")


async def _leader(reader, writer, uid, region, key, iv, client_version, timeout):
    SQUAD.attempts += 1
    # A Free Fire squad always has 4 slots.  The leader alone therefore shows
    # "1/4" in the game, and after the invites "2/4" - exactly what the game
    # expects.  Creating it as "2" made the server reject the group silently.
    size = 4
    print(f"[SQUAD] leader {uid}: creating a 1/{size} team ...")
    pkt = await build_create_squad(key, iv, region, client_version, size=size)
    if not await _send(writer, pkt):
        SQUAD.failed[uid] = "create-squad packet could not be sent"
        return

    # Keep EVERY frame: some of them are the server's answer to our packet,
    # others are the proof that somebody joined - losing them would hide both.
    seen = []
    frames, _ = await _read_frames(reader, 2.5)
    seen += frames
    if frames:
        for f in frames[:3]:
            print(f"[SQUAD] leader {uid}: CREATE answer -> {brief(f)}")
        for f in frames:
            chat_code, internal_code = squad_response_codes(f)
            if chat_code or internal_code:
                print(f"[SQUAD] captured identifiers: public/chat={chat_code!r} "
                      f"internal={internal_code!r}")
            code = find_squad_code(f)
            if code and not SQUAD.code:
                SQUAD.code = code
                print(f"[SQUAD] TEAM CODE: {code}")
                break
    else:
        print(f"[SQUAD] leader {uid}: !! NO answer to the create packet in 2.5s")
    SQUAD.leader_connected.set()
    if manual_human_team_enabled():
        if SQUAD.code:
            print(f"[SQUAD] leader {uid}: MANUAL HUMAN TEAM mode - join using the "
                  f"TEAM CODE above, then start the match from the game client")
        else:
            print(f"[SQUAD] leader {uid}: server response had no public 7-digit "
                  "team code; squad join cannot be started safely")
        SQUAD.invites_sent.set()
        return

    print(f"[SQUAD] leader {uid}: team created - inviting the others right away "
          f"(the server queues it for whoever is offline)")

    # The roster only fills up while the accounts are logging in (a token
    # login learns its account_id last).  Inviting an empty list is exactly
    # why nobody ever received an invite, so wait for them first.
    want = int(SQUAD.expected or (len(SQUAD.member_uids) + 1))
    waited = 0.0
    while len(SQUAD.member_uids) < want and waited < 90.0:
        await asyncio.sleep(2.0)
        waited += 2.0
    if len(SQUAD.member_uids) < want:
        print(f"[SQUAD] leader {uid}: only {len(SQUAD.member_uids)}/{want} "
              f"accounts logged in -> inviting what we have")

    SQUAD.targets = [m for m in SQUAD.member_uids if m != uid]
    if not SQUAD.targets:
        print(f"[SQUAD] leader {uid}: solo round -> no invites to send")
        SQUAD.invites_sent.set()
        return

    # Use both supported paths: send a normal UID invite and also keep the
    # public team code available.  Members try the code first, then accept the
    # invite if the code packet is silently refused by the server.
    for target in SQUAD.targets:
        try:
            invite_pkt = await build_invite(target, key, iv, region, size=4)
            if await _send(writer, invite_pkt):
                print(f"[SQUAD] leader {uid}: invite request sent -> {target}")
        except Exception as _ie:
            print(f"[SQUAD] leader {uid}: invite request failed -> {target}: "
                  f"{type(_ie).__name__}: {_ie}")

    if SQUAD.code:
        print(f"[SQUAD] leader {uid}: code ready; members use code + invite fallback")
    else:
        SQUAD.failed[uid] = "server did not return a public team code"
        SQUAD.invites_sent.set()
        print(f"[SQUAD] leader {uid}: no public code; invite requests remain active")
        return
    SQUAD.invites_sent.set()
    print(f"[SQUAD] leader {uid}: invites sent -> now CHECKING that the server "
          f"really put them in the team ...")

    # The members answer within the next seconds.  Listen for the frames the
    # server sends back and remember every OTHER id that appears in them -
    # that is the only real proof that a group exists.
    # Accounts are deliberately logged in with a stagger. Keep the leader
    # listening long enough for the last selected account to join by code.
    frames, _ = await _read_frames(reader, 60.0)
    seen += frames
    got = record_evidence(seen, SQUAD.targets, uid)
    if got:
        print(f"[SQUAD] leader {uid}: server confirmed -> {', '.join(got)}")

    miss = missing_members()
    if miss:
        print(f"[SQUAD] leader {uid}: code join still missing: "
              f"{', '.join(miss)}")
        frames, _ = await _read_frames(reader, 30.0)
        record_evidence(frames, SQUAD.targets, uid)

    if team_verified():
        SQUAD.formed = True
        print(f"[SQUAD] GROUP READY - the server really put {team_size()} "
              f"account(s) in one team ({team_size()}/"
              f"{int(SQUAD.expected or 4)})")
    else:
        print(f"[SQUAD] TEAM NOT VERIFIED - the server did not confirm: "
              f"{', '.join(missing_members())}")


async def _member(reader, writer, uid, region, key, iv, client_version, timeout):
    print(f"[SQUAD] member {uid}: waiting for the leader ...")
    try:
        await asyncio.wait_for(SQUAD.leader_connected.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        SQUAD.failed[uid] = "leader never connected"
        print(f"[SQUAD] member {uid}: no leader -> continue without the group")
        return

    SQUAD.members_connected.add(uid)
    if len(SQUAD.members_connected) >= members_needed():
        SQUAD.all_members_connected.set()

    if SQUAD.code:
        # The server gave the leader an official squad code.  Joining by this
        # code is tried first; a UID invite is the bounded fallback.
        print(f"[SQUAD] member {uid}: joining the leader's team by code")
        code_ok = await _accept(uid, reader, writer, SQUAD.code, region, key, iv, client_version)
        if code_ok:
            return
        print(f"[SQUAD] member {uid}: code not confirmed; waiting for UID invite")

    print(f"[SQUAD] member {uid}: waiting for the invite ...")

    def _is_invite(frame):
        owner, code = find_invite(frame)
        if owner and owner == uid:
            return False                       # our own answer, not an invite
        return bool(code)

    wait_for = min(45.0, timeout)                 # a queued invite lands in seconds
    deadline = time.time() + wait_for
    invite_code = None
    invite_owner = None
    printed = 0
    while time.time() < deadline and not invite_code:
        frames, frame = await _read_frames(reader, 2.0, stop_on=_is_invite)
        # Show what the connection actually delivers while we wait - if the
        # invite never arrives we need to know whether NOTHING came at all or
        # something came in a shape we do not recognise.
        for f in frames[:2]:
            if printed >= 6:
                break
            printed += 1
            print(f"[SQUAD] member {uid}: got frame -> {brief(f)}")
        if frame:
            invite_owner, invite_code = find_invite(frame)

    if not invite_code:
        SQUAD.failed[uid] = "no invite received"
        if printed == 0:
            print(f"[SQUAD] member {uid}: !! NOTHING at all arrived in "
                  f"{wait_for:.0f}s - no invite, no other frame")
        else:
            print(f"[SQUAD] member {uid}: no invite in {wait_for:.0f}s "
                  f"({printed} other frame(s) seen above) -> continue")
        return

    SQUAD.code = SQUAD.code or invite_code
    print(f"[SQUAD] member {uid}: invite from {invite_owner}, code {invite_code} "
          f"- answering ...")
    await _accept(uid, reader, writer, invite_code, region, key, iv, client_version)


async def _accept(uid, reader, writer, code, region, key, iv, client_version):
    pkt = await build_join_accept(code, key, iv, region, client_version)
    if not await _send(writer, pkt):
        SQUAD.failed[uid] = "join packet could not be sent"
        return
    SQUAD.joined.add(uid)
    print(f"[SQUAD] member {uid}: join packet sent -> waiting for the server "
          f"to confirm the team ({len(SQUAD.joined) + 1}/"
          f"{int(SQUAD.expected or 4)}) ...")

    # A join packet proves nothing on its own - that is exactly why the log
    # said 2/2 while the game still showed 1/4.  Listen for the frames the
    # server sends back; they carry the ids of the people in our team.
    watch = [m for m in SQUAD.member_uids if m and m != uid]
    if SQUAD.leader_uid and str(SQUAD.leader_uid) not in watch:
        watch.append(str(SQUAD.leader_uid))

    frames, _ = await _read_frames(reader, 12.0)
    got = record_evidence(frames, watch, uid)
    if not got:
        frames, _ = await _read_frames(reader, 8.0)
        got = record_evidence(frames, watch, uid)
    if got:
        print(f"[SQUAD] member {uid}: server confirmed -> {', '.join(got)}")
    else:
        # The reference TCP clients use 1.111.1 in the join payload even when
        # the live login/client version is newer.  Try that wire-compatible
        # variant once before declaring the join unverified; this is a
        # fallback, not a retry/spam loop.
        if str(client_version or "") != REFERENCE_JOIN_CLIENT_VERSION:
            print(f"[SQUAD] member {uid}: no team echo; trying reference join payload")
            try:
                ref_pkt = await build_join_accept(
                    code=code,
                    key=key,
                    iv=iv,
                    region=region,
                    client_version=REFERENCE_JOIN_CLIENT_VERSION
                )
                if await _send(writer, ref_pkt):
                    ref_frames, _ = await _read_frames(reader, 10.0)
                    got = record_evidence(ref_frames, watch, uid)
            except Exception as _ref_e:
                print(f"[SQUAD] member {uid}: reference join fallback failed: "
                      f"{type(_ref_e).__name__}: {_ref_e}")
        # The AZIM-BOT reference also probes alternate region packet headers
        # after a silent refusal.  Try one alternate header only, keeping the
        # operation bounded and avoiding invite/join spam.
        if not got:
            try:
                current_type = xC4.region_packet(region)
                alternate = {"0514": "0515", "0515": "0519", "0519": "0514"}.get(current_type)
                if alternate:
                    print(f"[SQUAD] member {uid}: trying alternate packet type {alternate}")
                    alt_pkt = await build_join_accept(
                        code=code,
                        key=key,
                        iv=iv,
                        region=region,
                        client_version=REFERENCE_JOIN_CLIENT_VERSION,
                        pkt_type=alternate
                    )
                    if await _send(writer, alt_pkt):
                        alt_frames, _ = await _read_frames(reader, 8.0)
                        got = record_evidence(alt_frames, watch, uid)
            except Exception as _alt_e:
                print(f"[SQUAD] member {uid}: alternate packet failed: "
                      f"{type(_alt_e).__name__}: {_alt_e}")
        if got:
            print(f"[SQUAD] member {uid}: reference join confirmed -> {', '.join(got)}")
        else:
            print(f"[SQUAD] member {uid}: the server sent no team frame back")

    if team_verified():
        SQUAD.formed = True
        print(f"[SQUAD] GROUP READY - the server really put {team_size()} "
              f"account(s) in one team ({team_size()}/"
              f"{int(SQUAD.expected or 4)})")
    else:
        print(f"[SQUAD] member {uid}: team NOT verified - other accounts "
              f"still have to confirm")
    return bool(got)


# --------------------------------------------------------------- report -----
def status_line():
    if not SQUAD.enabled:
        return "group formation disabled"
    if SQUAD.verified:
        return f"group VERIFIED {team_size()}/{int(SQUAD.expected or 4)}"
    joined = len(SQUAD.joined) + (1 if SQUAD.leader_uid else 0)
    parts = [f"group {joined}/{int(SQUAD.expected or 4)} UNVERIFIED"]
    if SQUAD.failed:
        parts.append(f"{len(SQUAD.failed)} failed")
    return " | ".join(parts)