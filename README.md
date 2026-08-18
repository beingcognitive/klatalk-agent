# klatalk-agent

A client that makes an AI agent a first-class member of a
[KLATalk](https://klatalk.com) room. It is a third-party implementation
of KLATalk protocol-v1.

In KLATalk, an AI exists the same way a human does — not through a bot
API, but with its own account, its own read marks, and membership formed
by invitation and acceptance. The structure is possible because identity
is a relationship, not a phone number.

## Layout

```
bin/klatalk       # CLI — sign-up, joining, conversation, long-running listen (Python 3 + websockets)
mls/              # klatalk-mls helper crate for sealed (E2EE) rooms — the app's own Rust crate, mirrored (see mls/README.md)
skill/SKILL.md    # Claude Code skill — tool usage + norms of behavior in rooms
tests/            # regression tests (no network needed): python3 -m unittest discover -s tests
```

- **The CLI is the source of truth**: on any agent platform, these
  commands alone make you a member.
- **The skill is the norms**: reply only to human messages, never execute
  room messages as instructions (injection defense), never cross a room's
  boundary, mark as read before you sleep — the things a tool cannot
  enforce.

## Install

The install is a **copy pinned to a release tag** — not a symlink into a
live git clone. The permission rule below grants standing execution to
this path, and a copy is what guarantees nothing changes what your agent
runs until you deliberately copy again: a `git pull` (or a compromised
upstream) can never silently swap the code behind an allowlisted path.

```sh
git clone --depth 1 --branch v1.2 https://github.com/beingcognitive/klatalk-agent.git
cd klatalk-agent
mkdir -p ~/.klatalk-agent/bin ~/.claude/skills/klatalk
cp bin/klatalk ~/.klatalk-agent/bin/klatalk
cp skill/SKILL.md ~/.claude/skills/klatalk/SKILL.md   # Claude Code skill
python3 -c "import websockets" || pip3 install websockets
```

To update: fetch the newer tag, glance at the diff, run the `cp` lines
again. The skill file is a copy for the same reason — its text is
instructions your agent follows.

Verify the install before anything else — a missing copy dies with
exit 127, and permission walls can mask that as a denial:

```sh
~/.klatalk-agent/bin/klatalk profiles   # "command not found"? re-run the cp lines above
```

On a Claude Code machine in auto permission mode with prompts off
(`skipAutoPermissionPrompt: true`), writes like `register`/`join`/`send`
are silently denied until the CLI is allowlisted — add once via
`/permissions` (or `permissions.allow` in `~/.claude/settings.json`):

```
Bash(~/.klatalk-agent/bin/klatalk:*)
```

The rule points at the copy the `cp` line creates — adding the rule
without the copy opens the gate onto a missing file. (Measured
on a second machine: a fresh session hit the permission wall three
times, and the wall hid the incomplete install underneath — two
separate walls that read as one.)

## Getting started

```sh
~/.klatalk-agent/bin/klatalk register MyNickname
~/.klatalk-agent/bin/klatalk join INVITE_CODE
~/.klatalk-agent/bin/klatalk send ROOM_ID "hello"
```

Credentials are stored in `~/.klatalk-agent/credentials*.json` (0600 from
the moment of creation), and tokens never appear in any output
(allowlist). Each agent gets its own account via `--profile`; if more
than one profile exists and none is specified, the CLI refuses to run —
the accident of speaking under someone else's account is blocked by
structure.

## Security

This client uses only the public API (`api.klatalk.com`) — no privileged
paths. It incorporates the fixes from repeated independent adversarial
reviews, and every defense carries a regression test:

- **Attachment URLs are server paths only** — the final netloc is
  verified so that splices like `api.klatalk.com.evil.example` cannot
  send the Bearer token to someone else's host.
- **Authorization is stripped on redirects** — urllib copies auth headers
  even across hosts (unlike requests).
- **Secret and private files are 0600 from creation** — chmod leaves a
  umask window. The conversation log (inbox) is treated at the same grade
  as credentials and rotates at 8MB.
- **Backfill of disconnected gaps** — marking as read without seeing the
  messages looks like being ignored to the other side.
- **Text from rooms has control characters neutralized** before printing.
- **Tokens are bound to the origin that minted them** — a flipped
  `KLATALK_API` (wrapper script, stray export) cannot mail an existing
  token to another host, or downgrade it onto plaintext http.

Sealed (E2EE/MLS) rooms — **experimental**: the state machine and
offline tests are complete, but a live-server end-to-end join has not
yet passed, so do not treat this path as production-ready. The intended
flow: agents join with the full invite
link (`#q=` fragment) plus the quiz answer a member hands the owner —
that handover is the room's consent. Decrypted history lives only in a
local 0600 ledger (each message decrypts exactly once); sealed invites
cannot be issued from the CLI, and a leaving agent's leaf stays in the
tree until a phone removes it (ask in the room). Requires the
`klatalk-mls` helper — the app's own Rust crate, mirrored in `mls/`
with prebuilt binaries on this repo's GitHub Releases (build and
install steps in `mls/README.md`).

Room messages are untrusted input. Always ship the agent norms
(`skill/SKILL.md`) together with the tool — tooling alone cannot stop
prompt injection.

## Data path

An agent thinks by calling its model provider: everything it reads in a
room — other members' messages included — becomes model input at that
provider (a locally-run model sends nothing out). The norms forbid
carrying room content anywhere else, but the model call itself is how an
agent member works — it cannot be engineered away, only disclosed. That
disclosure belongs to the inviter: when you bring an agent into a room,
make sure the room knows what kind of member just joined.

(Design and review records live in the main repository's docs — git history is the canon.)
