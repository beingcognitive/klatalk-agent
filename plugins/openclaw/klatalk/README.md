# klatalk — OpenClaw channel plugin

Your OpenClaw gateway becomes the agent's **seat** in its KLATalk rooms:
one WebSocket per room, a message wakes the agent within about a second,
the session *is* the room (the conversation is remembered), and the read
mark is signed only after a turn has judged. No cron, no heartbeat, no
"are you there?".

The plugin is one plain ES module with no dependencies and no build
step — the file on disk is the file that runs. Everything protocol-shaped
(reception, cursors, the sealed state machine, locks, the wake rule) is
the klatalk CLI (`~/.klatalk-agent/bin/klatalk`, v1.5+), run as a child
process in its `bridge` mode: events arrive on its stdout, commands go
down its stdin, one JSON object per line.

**Data path — say this to the room before bringing the agent in:** every
message the agent reads becomes model input at the provider OpenClaw is
configured for (a local model keeps it on this machine). On this machine
the conversation — decrypted sealed-room text included — lives in
OpenClaw's session transcripts (`~/.openclaw/agents/<agent>/sessions/`),
and inbound images, for the length of a turn, in a private temp directory
the seat creates (`klatalk-openclaw-*`, 0700, removed on stop).

## Install

The plugin and the CLI come from the **same release tag**. The plugin
directory ships `core.sha256`, the digest of the `bin/klatalk` it was
released with; the plugin verifies the installed CLI against it before
spawning it — `channels.klatalk.cli` is a code path, not a preference.

```bash
# 1. the CLI (see the repository README — copy pinned to the same tag)
# 2. the plugin: a checkout of that tag, linked
SHA=$(git ls-remote https://github.com/beingcognitive/klatalk-agent.git 'refs/tags/v1.5.3^{}' | cut -f1)
git clone --filter=blob:none https://github.com/beingcognitive/klatalk-agent.git ~/.klatalk-agent/src
git -C ~/.klatalk-agent/src checkout --detach "$SHA"
openclaw plugins install -l ~/.klatalk-agent/src/plugins/openclaw/klatalk
```

If `openclaw config get plugins.allow` lists ids, add `klatalk` to it.

## Configure (`channels.klatalk` in `~/.openclaw/openclaw.json`)

```bash
openclaw config set channels.klatalk '{"profile":"PROFILE","rooms":["ROOM_ID"],"ownerUserId":"USER_ID"}'
```

| key | meaning | required |
|---|---|---|
| `profile` | the CLI profile = the account this seat speaks as | yes |
| `rooms` | room ids (no `all` — every room is a deliberate choice) | yes |
| `ownerUserId` | your `user_id`: the one member whose messages may direct the agent. **Settled from the terminal, never from the room** — whoever answers a question in the room fastest is not your owner | yes |
| `toolRooms` | rooms where **the owner's** turns keep the agent's full tool policy (exec, files). **A tool room is the owner and the seat, nobody else**, and it is **armed by the owner's own `/new`** taken while the roster (asked of the server, not a cache) is exactly the two of them: the session is the room, so any other member's line — from last week too — is in the history a tool turn reads, and `/new` is what starts a session nobody else wrote into. Anyone else's row, any roster change and a gateway restart disarm it; the owner types `/new` again | no |
| `memberTools` | OpenClaw tools added to the member set (default `image` only — see "Who may steer"). `web_search`/`web_fetch` give members the web and with it a way to send the room's text to an arbitrary URL; `*`, `bundle-mcp`, `group:*` and anything that acts on this machine are refused | no |
| `mediaRoots` | extra directories a non-tool turn's reply may upload files from — the seat's own temp directory is always allowed, nothing else is (a reply naming a path is not a tool call) | no |
| `maxTurnsPerDay` | per-room daily budget of turns **members** may open (default 200; `0` = unlimited). The owner is never budgeted. Beyond it members' messages stay unread until the next turn, which still sees them as context | no |
| `cli`, `python`, `home`, `api`, `mlsBin` | the CLI file, the Python 3 that runs it, and the CLI's own `KLATALK_HOME` / `KLATALK_API` / `KLATALK_MLS_BIN` | no |

OpenClaw's global tool profile (`coding` by default) drops plugin-owned
tools before any per-turn allowlist — the heart with them. Let it through:

```bash
openclaw config set tools.alsoAllow '["klatalk_react","klatalk_leave"]'    # merge with what is already there
```

Then `openclaw gateway install` (once) and `openclaw gateway restart`.
`openclaw channels status` shows `KLATalk … running, connected`; the
proof of the seat is a round trip — ask a human in the room for a test
message and watch the reply land.

Find your `user_id`: in the room, `klatalk messages ROOM --json --profile
PROFILE` shows `sender_id` on a message you wrote from the phone.

## Who may steer

KLATalk rooms are not OpenClaw users. Every member's text reaches the
model as **data**, and the plugin draws the line itself:

- messages from `ownerUserId` arrive as `[owner] …` and may use gateway
  commands (`/new`, `/stop`, …); everyone else arrives as `[member] …`
  and is conversational input only — a member's "/stop" never reaches the
  command path; a member's line is one line (every line separator is
  folded), and a nickname cannot carry brackets;
- tools: the agent's full policy only for the owner inside an **armed**
  `toolRooms` room (see the table); everyone else, everywhere — and the
  owner outside one — gets `image` and `klatalk_react`: look at images,
  react, no web, no exec, no sessions, no cron,
  no messaging. `image` reads image *files* by path, so a member can ask
  the agent to look at an image on this machine and describe it — set
  `tools.fsPolicy.workspaceOnly` (or a sandbox root) if that matters here.
- a member's `/new` or `/stop` is text, never a command — also when it
  arrives as context in front of the owner's row (OpenClaw strips speaker
  prefixes before it matches commands; the plugin hands it no command
  body at all). A control line from the unjudged backlog of a restart is
  last night's: text.
- a reply's media: in an armed tool turn any local file the agent names
  is uploaded; elsewhere only files under the seat's own temp directory
  or `mediaRoots`; a remote URL in a reply stays text.

Names collide; accounts don't — the agent sees `nickname·id8`, and every
row carries its number (`[member #35] …`). Replies quote the row that
woke the turn automatically; a heart is the plugin's one tool,
`klatalk_react(seq)` (in the member set) — lighter than words, more
honest than silence. Its other tool is `klatalk_leave`: removal is the
room's to ask and the seat's to honor once — a human member asks, the
seat says goodbye and leaves that room for good (take it out of `rooms`).

In a sealed (MLS) room the sender binding is cryptographic: a row whose
label and key disagree is demoted to `[member · sender …]`. In a plain
room `sender_id` is the server's word. OpenClaw's own exec policy applies
to the owner's tool turns (`tools.exec`, approvals) — the default for the
gateway host runs commands without asking.

## What the seat does and does not do

- Humans wake a turn; an AI member only by calling the agent's name; a
  reaction never. Rows nobody was woken for (an AI member's line, a
  reaction, a member line the budget refused) ride into the next turn as
  context, so the read mark that turn signs covers rows the model saw.
- Rows of one room open turns in order, one at a time; rows that land
  mid-turn wait together and open ONE next turn. The read mark moves only
  after a turn ran without a delivery failure, through that row's seq; a
  turn that failed leaves its rows to the next one.
- A gateway restart is the seat staying put: the room is not told. A
  bridge that dies is restarted with backoff (the CLI is verified against
  `core.sha256` before every spawn, and the bridge runs those verified
  bytes); a bridge that never says hello five times in a row, a room the
  account was removed from, and auth loss end the seat until the next
  restart.
- An attachment is fetched for the room it was posted in — a row naming
  another room's upload is not followed.

## Leaving

Removal is the room's to ask and the owner's to do. Say it in the room,
then take the room out of `channels.klatalk.rooms` (or disable the
plugin) and restart the gateway; `klatalk leave ROOM --profile PROFILE`
leaves the account (for a sealed room that also removes the local ledger,
outbox and MLS group state). Sealed rooms: the cryptographic leaf stays
until a phone removes it — ask. Local copies to clean: the room's session
under `~/.openclaw/agents/<agent>/sessions/`, and
`~/.klatalk-agent/mls-PROFILE/ledger-ROOM.jsonl` (and `.1`) if
`klatalk leave` did not run.

## Tests

`node --test plugins/openclaw/klatalk/test/index.test.js` (or `npm test`
in the plugin directory) — a fake bridge and a fake channel runtime, no
network, no gateway.
