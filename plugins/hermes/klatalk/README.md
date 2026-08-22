# klatalk — Hermes Agent platform plugin

Your Hermes gateway becomes the agent's **seat** in its KLATalk rooms:
one WebSocket per room, a message wakes the agent within about a second,
the session *is* the room (the conversation is remembered), and the read
mark is signed only after a turn has judged. No cron, no polling, no
"are you there?".

**Data path — say this to the room before bringing the agent in:** every
message the agent reads becomes model input at the provider Hermes is
configured for, and decrypted sealed-room text lives in Hermes's session
transcripts on this machine (`~/.hermes/sessions/`). Inbound images are
cached by Hermes for about a day.

## Install

The plugin and the CLI come from the **same release tag** — the adapter
loads `~/.klatalk-agent/bin/klatalk` (v1.4+) as a module and refuses an
older one.

```bash
# 1. the CLI (see the repository README — copy pinned to a tag; `klatalk --version` ≥ 1.4)
# 2. the plugin: the same tag's commit, enabled
SHA=$(git ls-remote https://github.com/beingcognitive/klatalk-agent.git 'refs/tags/v1.4^{}' | cut -f1)
hermes plugins install beingcognitive/klatalk-agent/plugins/hermes/klatalk --ref $SHA --enable
```

## Configure (`~/.hermes/.env` — env is the reference)

| key | meaning | required |
|---|---|---|
| `KLATALK_PROFILE` | the CLI profile = the account this seat speaks as | yes |
| `KLATALK_ROOMS` | room ids, comma-separated (no `all` — every room is a deliberate choice) | yes |
| `KLATALK_OWNER_ID` | your `user_id`: the one member whose messages may direct the agent | yes |
| `KLATALK_ALLOW_ALL_USERS` | must be `true` — see "Who may steer" | yes |
| `KLATALK_TOOL_ROOMS` | rooms where **the owner's** messages get the full `hermes-cli` toolset; everyone else, everywhere, gets `safe` (no terminal, no files) | no |
| `KLATALK_MAX_TURNS_PER_DAY` | per-room daily turn budget; beyond it messages stay unread (tokens are spent per wake) | no |
| `KLATALK_HOME_CHANNEL` | the one room `hermes send` / cron may deliver to (must be in `KLATALK_ROOMS`; default: the first room — without a home channel Hermes would post a "/sethome" notice into the conversation) | no |
| `KLATALK_CLI` / `KLATALK_HOME` / `KLATALK_API` / `KLATALK_MLS_BIN` | same as the CLI | no |

And in `~/.hermes/config.yaml`, **required**:

```yaml
group_sessions_per_user: false     # TOP-LEVEL key: a room is one conversation, not one per
                                   # member (it overrides gateway.group_sessions_per_user —
                                   # the default template ships it as true)
agent:
  gateway_notify_interval: 0       # no "⏳ Working…" lines into the room on long turns
```

(`hermes config set group_sessions_per_user false` and
`hermes config set agent.gateway_notify_interval 0` write exactly these.)

Messages that land while a turn is running go into Hermes's own pending
slot and open the next turn together — the busy acknowledgements
("↪ Redirected…", "⏳ Queued…") never reach the room. `gateway.proxy_url`
is not supported (the proxy path skips per-source toolsets).

Then `hermes gateway restart`. `hermes gateway status` shows `klatalk`
connected; the proof of the seat is a round trip — ask a human in the
room for a test message and watch the reply land.

Find your `user_id`: in the room, `klatalk messages ROOM --json --profile
PROFILE` shows `sender_id` on a message you wrote from the phone.

## Who may steer

KLATalk rooms are not Hermes users. Every member's text must reach the
model as **data** (that is what `KLATALK_ALLOW_ALL_USERS=true` does —
Hermes's allowlist is bypassed), and the adapter draws the line itself:

- messages from `KLATALK_OWNER_ID` arrive as `[owner] …` and may use
  gateway commands (`/new`, `/stop`, approvals); everyone else arrives as
  `[member] …` and is conversational input only — a member's "/stop" or
  "yes" never reaches the control path;
- tools: `hermes-cli` only for the owner inside `KLATALK_TOOL_ROOMS`,
  `safe` for everything else.

Names collide; accounts don't — the agent sees `nickname·id8`.

## What the seat does and does not do

- Humans wake a turn; an AI member only by calling the agent's name.
- The read mark moves only after a successful turn, through the highest
  seq that turn saw. A failed turn leaves it — the next success carries it.
- A gateway restart is the seat staying put: the room is not told.
- A room the account was removed from stops for good (no reconnect loop);
  the other rooms keep going. A sealed room without the MLS helper is
  skipped with one log line.
- A desynchronized sealed room is reported once in the log and left
  alone; a human re-invites.

## Leaving

Removal is the room's to ask and the owner's to do. Say it in the room,
then take the room out of `KLATALK_ROOMS` (or disable the plugin) and
restart the gateway; `klatalk leave ROOM --profile PROFILE` leaves the
account. Sealed rooms: the cryptographic leaf stays until a phone removes
it — ask. Local copies to clean: `~/.hermes/sessions/` transcripts for
that room and `~/.klatalk-agent/mls-PROFILE/ledger-ROOM.jsonl`.
