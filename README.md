# AZIM-MATCH - UID/password BR matcher

Guest account generation is standalone and is **not started by Main.py**:

    cd AZIM-MATCH
    python3 GUEST_GEN_FIXED.py

After generation, put the returned UID/password record in the local
`DARK_MASTER.json` beside `Main.py`. AZIM-MATCH loads that file directly.

Any number of selected UID/password accounts is accepted; there is no forced
2-to-4 account validation.

Start it, **paste the access token(s) at the prompt** (one per line, empty
line = done), and every account logs in **with the token only** - no uid,
no password, no MajorLogin on an account that already has a session.

Current config: each token runs independently -> queue **Lone Wolf**
(no squad, no friend request and no boss invite) -> play -> **when the game
ends the same account starts the next round again**.

**No level check and no guild prompt** - the account is already level 8+
and already in a guild, so there is nothing to wait for.

## Run

    cd /sdcard/download/AZIM-MATCH
    bash run.sh          # or: python3 -u Main.py

At the start:

    [LOGIN] Paste your access token(s) - one per line.
    [LOGIN] Press Enter on an EMPTY line when you are done.
      token> <paste>
      token>            <- Enter on an empty line = start

Fallbacks, in order: `AZIM_TOKENS` env var, then `tokens.txt`
(one token per line, `#` comments). Duplicate tokens are dropped.

`match.log` collects everything (also when the console scrolls away).

## Files

| file | what it does |
|------|--------------|
| `Main.py` | engine: **token login**, BR StartMatch, match found, UDP play loop, play supervisor |
| `squad.py` | creates the **1/4 team**, invites, and **proves** the server accepted it |
| `friend.py` | **friends first**: request each other, then verify with `GetFriend` |
| `guild.py` | `RequestJoinClan` HTTP join - not used anymore (account is already in a guild) |
| `safety.py` | ban guard + "no second MajorLogin in 5 days" + staggered logins |
| `config.json` | movement, squad, safety, matchmaking, dashboard |
| `tokens.txt` | optional: one access token per line (only read if the prompt stays empty) |

## Friends first

> The current `config.json` is set to independent `lone_wolf` mode, so this
> team/friend section is inactive. Enable `battle_royale` and `squad` only
> when you want the multi-account team flow again.

Before anything connects to the game gateway, every account of the round:

1. waits until all of them finished their login (roster is full, max 120 s),
2. sends a **friend request to every other account**,
3. fetches the real friend list (`POST /GetFriend`, read-only) and checks that
   each of the others is in it,
4. re-requests and re-checks anything that is still missing,
5. prints `ALL n/n confirmed in the friend list` or `NOT confirmed - missing ...`.

Only then are the group and the matchmaking started. The boss request
(`config.json -> friend.boss_uid`) is sent before squad formation so the
human can accept it in the official game client.

**Hard gate:** right before the team (`create` -> `invite` -> `join`) the
friend list is checked **again**. If any account is missing, `squad_stage`
returns immediately and sends **zero packets** - no team is created until
everybody really is a friend:

```
[FRIEND] A: CHECKING the friend list BEFORE the team (1 other account(s))
[FRIEND] A: 1 not friends yet -> sending the request again: 18461450004
[FRIEND] A: STILL NOT in the friend list: 18461450004
[FRIEND] A: FRIENDSHIP NOT CONFIRMED for the round -> no team will be created
[SQUAD] A: team NOT created - the accounts are not confirmed friends
[SQUAD] TEAM CHECK FAILED ... -> your command in Termux (s / b / q)
```

Note: FF has no "accept" endpoint in this codebase, so both sides request
each other - the server puts them together when the requests cross.

## The team is checked, not assumed

Sending a join packet does **not** mean there is a group - the log used to
print `GROUP READY 2/2` while the game still showed `1/4`.

Now:

1. the leader creates a **1/4 team** (a Free Fire squad always has 4 slots),
2. it invites every other account, each account answers with the join packet,
3. **both sides listen** to the frames the server sends back and remember
   every other account id that appears in them,
4. the leader re-invites once if somebody is still missing,
5. only then: `GROUP READY - the server really put 2 account(s) in one team (2/2)`.

A member counts as *in the team* when **either** the leader saw it join
**or** the member saw the team it joined - one real frame is enough proof.

### Human boss player

With `squad.invite_boss: true`, the bot accounts first form and verify their
team. The leader then repeatedly invites `friend.boss_uid` for up to
`squad.boss_wait_sec` seconds. Keep the boss account logged into the official
Free Fire app, accept the friend request and team invite. Matchmaking starts
only after the server echoes the boss UID in the team frame; otherwise the
round stops without starting a separate match.

If the check fails you get:

```
[SQUAD] TEAM CHECK FAILED - the server never sent back: 18461450004
[SQUAD]          ids we did see in a team frame: none
[!] [TEAM] THE ACCOUNTS ARE NOT IN ONE TEAM (join/invite sent, but the server never confirmed)
  type a command here in Termux:
        s = start match SEPARATELY (no group)
        b = BREAK / pause until you press Enter
        q = STOP everything
[TEAM] command>
```

One answer is shared by **all** accounts of the round, and it is asked again
for the next round. With no terminal attached (EOF) it falls back to `s`, so
the bot never hangs. `q` stops the whole play loop and ends the program.

## The device fingerprint

`get_device_for_account()` used to pick width, height, dpi, memory and CPU
**independently at random**, which produced phone screens that do not exist:

```
devices.json (old):  8 plausible, 33 impossible
  1440x1600 (ratio 1.11)   <- emulator window shape
   720x3200 (ratio 4.44)
  1080x1600, 1280x1600, 1440x2400, 720x2400, 1080x3200, 1280x3200
```

Now width / height / dpi / memory / cpu come from **one spec per model**
(`MODEL_SPECS`, all 1080x2400 or 1080x2412), an existing entry with an
impossible screen is regenerated on load, and every account gets its **own**
install directory instead of the one path that all of them shared.

Honest limit: `MajorLoginReq` has **no emulator flag** - fields 3..100 are
device/screen/gpu/cpu only, and the payload is byte-identical to the proven
LEVEL-UP capture. So there was never an "is emulator = 1" to turn off; what
could be fixed were the self-contradicting fingerprints above. Whether
Garena moves the accounts out of the emulator pool can only be seen in a
live run (fewer/faster matches, and the new `[QUEUE]` lines).

## What is proven

* Battle Royale StartMatch, match-found frame (`0300`, >=300 bytes),
  UDP handshake, keepalive and the movement simulator come from the
  working `LEVEL-UP` bot - this folder only re-wires it.
* Session safety: cache TTL is 7 days (LEVEL-UP had 1200 s = a new
  MajorLogin every 20 minutes), plus a hard 5 day gap, a Garena ban
  verdict before every login and 18 s between the 4 logins.
* Region: StartMatch uses `03` + region byte -> **`0314` for IND**
  (the old code hardcoded `0315`, which is "everywhere but IND").
* Shared movement: every account uses the same seed, so all 4 walk the
  same path and stay together.

## What needs the first live run

1. **StartMatch opcode** - `config.json -> matchmaking.start_match_opcodes`
   is `["0314","0315"]`. The bot sends `0314` (correct for IND), and if no
   `MATCH FOUND` frame comes back it automatically switches to `0315`
   (every 5th attempt) and locks on to whichever one the server accepts.
   No manual editing needed - just read `match.log`.
2. **Group formation** - `CreateNewSquad` is known good, the invite/accept
   step has historically been answered with `Protection Bypass (code 71)`
   in AZIM-BOT. `squad.py` logs every step; if the accept is refused the
   bots still play (independent Battle Royale queues). **Fix in this
   version:** the roster used to be built from login uids *before* the
   logins, so the leader invited an empty list. It is now filled with each
   `account_id` right after that account logs in, and the leader waits up
   to 90 s for the whole roster before inviting.
3. **Guild join** - not called anymore: the account is already in a guild
   and `glory_supervisor` (level 8 gate + guild prompt) has been replaced
   by `play_supervisor`, which only restarts the round when the workers end.

## Matchmaking (current status)

`match.log` of the last full run: the server **accepts** every StartMatch
(823 x "Match starting, please wait...") but `MATCH FOUND! Loading...`
never arrived (**0**), so no game was ever played and every account stayed
at level 2. Two fixes are already in:

* the queue is sent **once** and then waits `matchmaking.wait_for_match_sec`
  (120 s) instead of spamming it every 3 s;
* the gateway idle timeout no longer throws that wait away after 40 s -
  it keeps the socket warm with a keepalive and only re-queues once the
  full 120 s passed.

Still open: **why the server never sends the match-found frame.**

## Config (`config.json`)

* `matchmaking.wait_for_match_sec` - 120 (re-queue only after this)
* `movement.shared` - true, `interval_sec` 2.5 -> identical movement
* `squad.enabled` - true/false
* `safety.login_stagger_sec` - 18 s between the 4 logins
* `safety.major_login_gap_days` - 5 (never a second MajorLogin inside)
* `dashboard` - false (set true for the web dashboard on :20335)
