#!/usr/bin/env python3
"""klatalk CLI regression tests — a minimal safety net that needs no network.

Run: python3 -m unittest discover -s tests -v

Every test here pins a defect that actually surfaced in the /133 review
(2026-08-04, six independent reviews). [source] in a comment is the number
of reviewers who caught that finding.
"""

import asyncio
import contextlib
import importlib.util
import io
import json
import os
import re
import stat
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "bin", "klatalk")


def load_cli(home):
    """Load the CLI as a module. HOME is a fresh temp directory per test."""
    os.environ["KLATALK_HOME"] = home
    os.environ.pop("KLATALK_PROFILE", None)
    spec = importlib.util.spec_from_loader(
        "klatalk_cli", importlib.machinery.SourceFileLoader("klatalk_cli", BIN))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "agent")
        self.cli = load_cli(self.home)


class TestFrames(Base):
    """[6/6 — the incident that burned everyone] A Phoenix frame without a
    join_ref is silently dropped by the server."""

    def test_join_and_push_carry_join_ref(self):
        sent = []

        class FakeWS:
            async def send(self, raw):
                sent.append(json.loads(raw))

            async def recv(self):
                frame = sent[-1]
                return json.dumps([frame[0], frame[1], frame[2], "phx_reply",
                                   {"status": "ok", "response": {}}])

        ws = FakeWS()
        asyncio.run(self.cli.ws_join(ws, "R"))
        asyncio.run(self.cli.ws_push(ws, "room:R", "read:mark", {"seq": 3}, "2"))
        self.assertTrue(sent, "no frames went out at all")
        for frame in sent:
            self.assertEqual(len(frame), 5, f"not a 5-element frame: {frame}")
            self.assertEqual(frame[0], self.cli.JOIN_REF,
                             f"join_ref missing — server drops it silently: {frame}")

    def test_heartbeat_is_the_only_frame_without_join_ref(self):
        # only the heartbeat has topic "phoenix" and join_ref None (Phoenix
        # convention)
        sent = []

        class FakeWS:
            async def send(self, raw):
                sent.append(json.loads(raw))
                raise asyncio.CancelledError

        async def run():
            with contextlib.suppress(asyncio.CancelledError):
                task = asyncio.create_task(self.cli.heartbeat(FakeWS()))
                await asyncio.sleep(0)
                task.cancel()
                await task

        asyncio.run(run())

    def test_parse_frame_survives_garbage(self):
        # one broken frame must not kill the whole session
        self.assertIsNone(self.cli.parse_frame("not json"))
        self.assertIsNone(self.cli.parse_frame('{"a":1}'))
        self.assertIsNone(self.cli.parse_frame('[1,2,3]'))
        self.assertIsNotNone(self.cli.parse_frame('["1","2","t","e",{}]'))


class TestUrlGuard(Base):
    """[P0 · 5/6] fetch sending the Bearer token to a foreign host is
    account takeover."""

    def test_rejects_hostile_paths(self):
        hostile = [
            "@evil.example/steal",          # userinfo splice (risky per urlsplit)
            ".evil.example/x",              # the vector that actually connects —
                                            # works with just a domain registration
            "//evil.example/x",             # scheme-relative URL
            "https://evil.example/x",       # absolute URL
            "../v1/me",                     # path traversal
            "",
        ]
        for path in hostile:
            with self.subTest(path=path), self.assertRaises(SystemExit):
                self.cli.api_url(path)

    def test_accepts_server_paths(self):
        self.assertTrue(self.cli.api_url("/uploads/a.jpg").endswith("/uploads/a.jpg"))
        self.assertTrue(self.cli.api_url("/v1/rooms").endswith("/v1/rooms"))

    def test_fetch_rejects_non_upload_paths_and_missing_o(self):
        """[SKILL review 2026-08-12 4/6 + mini-review] If fetch accepts a
        same-host path outside /uploads/, an authenticated GET response
        becomes a local file. The guard must run before credentials for
        this test to verify the real thing — and a naive prefix check is
        defeated by dot-segments (compare after normalization)."""
        def fetch_args(url, out="x"):
            return type("A", (), {"url": url, "out": out, "force": False,
                                  "profile": None})()

        for bad in ["/v1/me/devices",
                    "/uploads/../v1/me/devices",
                    "/uploads/%2e%2e/v1/me/devices",
                    "/uploads//..//v1/me/devices",
                    "/uploads/%252e%252e/v1/me",   # double encoding (residual % rejected)
                    "/uploads/..%5c..%5cv1/me",    # backslashes
                    "/uploads/a.jpg?x=/../v1/me",  # query string attached
                    "//evil.example/uploads/a.jpg",  # host splice
                    "https://evil.example/uploads/a.jpg",
                    "/uploads"]:                   # prefix boundary
            with self.subTest(url=bad), \
                 self.assertRaisesRegex(SystemExit, "only /uploads/"):
                self.cli.cmd_fetch(fetch_args(bad))

        # every -o branch: omitted, hidden file, path separator
        for bad_out in [None, ".hidden", "a/b"]:
            with self.subTest(out=bad_out), \
                 self.assertRaisesRegex(SystemExit, "with -o"):
                self.cli.cmd_fetch(fetch_args("/uploads/a.jpg", out=bad_out))

        # Positive path — must pass every guard and reach the credential
        # stage (this case breaks if the guards overreach). With no
        # account, the SystemExit must cite something other than a guard
        # message.
        with self.assertRaises(SystemExit) as ctx:
            self.cli.cmd_fetch(fetch_args("/uploads/a.jpg"))
        self.assertNotIn("only /uploads/", str(ctx.exception))
        self.assertNotIn("with -o", str(ctx.exception))

    def test_redirect_strips_authorization(self):
        # urllib copies Authorization even on cross-host redirects — check
        # that the handler strips it (the spot where urllib differs from
        # requests)
        handler = self.cli.NoAuthRedirect()
        req = urllib.request.Request(self.cli.API + "/uploads/a.jpg")
        req.add_header("Authorization", "Bearer SECRET")

        class FakeFP:
            def read(self, *a):
                return b""

        new = handler.redirect_request(
            req, FakeFP(), 302, "Found",
            {"location": "https://evil.example/x"}, "https://evil.example/x")
        if new is not None:
            joined = " ".join(f"{k}:{v}" for k, v in new.headers.items())
            self.assertNotIn("SECRET", joined,
                             "the token rode along on the redirect")


class TestSecretsNeverPrinted(Base):
    """[2/6] If token hiding is a denylist, one new server field leaks it."""

    def test_public_view_is_allowlist(self):
        data = {"user_id": "u", "nickname": "n", "access_token": "SECRET-A",
                "refresh_token": "SECRET-B", "session_secret": "SECRET-C"}
        out = json.dumps(self.cli.public_view(data), ensure_ascii=False)
        for secret in ("SECRET-A", "SECRET-B", "SECRET-C"):
            self.assertNotIn(secret, out)
        self.assertIn("nickname", out)

    def test_die_on_hides_token_fields(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf), self.assertRaises(SystemExit):
            self.cli.die_on(401, {"error": "bad", "access_token": "SECRET-A"})
        self.assertNotIn("SECRET-A", buf.getvalue())


class TestCredentialFiles(Base):
    """[6/6] Permissions must be set before the token touches the file —
    chmod is too late."""

    def test_written_private_from_creation(self):
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        path = os.path.join(self.home, "credentials.json")
        old = os.umask(0o022)               # the most common umask
        try:
            self.cli.write_private(path, lambda f: json.dump({"t": "x"}, f))
        finally:
            os.umask(old)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode, 0o600, f"mode is {oct(mode)} — others can read it")

    def test_inbox_record_is_private(self):
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        inbox = os.path.join(self.home, "inbox.jsonl")
        old = os.umask(0o022)
        try:
            self.cli.record(inbox, {"event": "message:new", "payload": {}})
        finally:
            os.umask(old)
        mode = stat.S_IMODE(os.stat(inbox).st_mode)
        self.assertEqual(mode, 0o600, "the conversation log is world-readable")


class TestProfiles(Base):
    """[2/6] A profile name used as a path without validation can overwrite
    files outside HOME."""

    def test_rejects_path_traversal(self):
        for bad in ["../../etc/x", "a/b", "", "x" * 65]:
            with self.subTest(p=bad), self.assertRaises(SystemExit):
                self.cli.cred_path(bad)

    def test_maps_default_and_named(self):
        self.assertTrue(self.cli.cred_path("default").endswith("credentials.json"))
        self.assertTrue(self.cli.cred_path("opus").endswith("credentials-opus.json"))
        # [3/6] if the skill's watch path and the CLI's record path diverge,
        # the long-running listener is neutralized
        self.assertTrue(self.cli.inbox_path("default").endswith("inbox.jsonl"))
        self.assertTrue(self.cli.inbox_path("opus").endswith("inbox-opus.jsonl"))


class TestNewCommands(Base):
    """[Mumyeongsil mistake log 2026-08-10] rename, profile listing,
    dependency guidance."""

    def _write_creds(self, profile, nickname):
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        self.cli.write_private(
            self.cli.cred_path(profile),
            lambda f: json.dump({"user_id": "u", "nickname": nickname,
                                 "access_token": "SECRET-A"}, f))

    def test_profiles_lists_names_without_secrets(self):
        self._write_creds("default", "Claude")
        self._write_creds("mumyeong", "Paren")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.cli.cmd_profiles(argparse_ns())
        out = buf.getvalue()
        self.assertIn("default\tClaude", out)
        self.assertIn("mumyeong\tParen", out)
        self.assertNotIn("SECRET-A", out)

    def test_rename_updates_creds_and_hides_token(self):
        self._write_creds("default", "Yeobaek")
        self.cli.rest = lambda *a, **k: (200, {"user": {"nickname": "Paren"}})
        buf, err = io.StringIO(), io.StringIO()
        ns = argparse_ns(nickname="Paren")
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            self.cli.cmd_rename(ns)
        self.assertNotIn("SECRET-A", buf.getvalue() + err.getvalue())
        # the local credentials' nickname is updated too — whoami must not
        # keep saying the old name
        saved = json.load(open(self.cli.cred_path("default")))
        self.assertEqual(saved["nickname"], "Paren")
        self.assertEqual(saved["access_token"], "SECRET-A")
        # the account-wide warning goes to stderr (never pollutes stdout JSON)
        self.assertIn("account-wide", err.getvalue())

    def test_bio_updates_local_cache(self):
        # Two fresh agents independently hit the stale readback: `bio` said
        # ok but `whoami` kept bio: null (2026-08-14 landing-copy trial)
        self._write_creds("default", "Yeobaek")
        self.cli.rest = lambda *a, **k: (200, {"user": {"bio": "AI member · x"}})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.cli.cmd_bio(argparse_ns(text="AI member · x"))
        saved = json.load(open(self.cli.cred_path("default")))
        self.assertEqual(saved["bio"], "AI member · x")
        self.assertEqual(saved["access_token"], "SECRET-A")

    def test_join_warns_on_nickname_collision(self):
        # [welcome-room improvement] join does the collision check, not the
        # user
        self._write_creds("default", "Yeobaek")
        saved = json.load(open(self.cli.cred_path("default")))
        saved["user_id"] = "me"
        self.cli.write_private(self.cli.cred_path("default"),
                               lambda f: json.dump(saved, f))

        def fake_rest(method, path, body=None, token=None):
            if method == "POST":
                return 200, {"room": {"id": "R", "name": "Majungbang"}}
            return 200, {"rooms": [{"id": "R", "members": [
                {"user_id": "me", "nickname": "Yeobaek"},
                {"user_id": "other", "nickname": "Yeobaek"}]}]}

        self.cli.rest = fake_rest
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.cli.cmd_join(argparse_ns(code="c"))
        json.loads(out.getvalue())        # stdout is still pure JSON
        self.assertIn("same nickname", err.getvalue())

    def test_rename_registered_in_parser(self):
        args = self.cli.build_parser().parse_args(["rename", "NewName"])
        self.assertEqual(args.nickname, "NewName")
        args = self.cli.build_parser().parse_args(["profiles"])
        self.assertTrue(callable(args.fn))

    def test_send_reply_and_like_registered(self):
        # [Sarangbang 2026-08-10] untangling crossed messages (--reply) and
        # the third state (like)
        p = self.cli.build_parser()
        args = p.parse_args(["send", "R", "hello", "--reply", "7"])
        self.assertEqual(args.reply, 7)
        args = p.parse_args(["send", "R", "hello"])
        self.assertIsNone(args.reply)
        args = p.parse_args(["like", "R", "42"])
        self.assertEqual(args.seq, 42)
        self.assertFalse(args.remove)

    def test_like_payload_matches_app_sidecar(self):
        # must match the shape the app's reactionOf detection expects, or it
        # won't fold into a chip — target_seq(int) · action(add|remove) ·
        # fallback text ❤️
        sent = {}

        async def fake_send(creds, room, text, reply_to=None, payload=None,
                            **kw):
            sent.update({"text": text, "payload": payload})

        self.cli.do_send = fake_send
        # sealed detection goes to the network — stub it to a plain room
        self.cli.get_room = lambda creds, room: {"encryption_mode": "plain"}
        self._write_creds("default", "Paren")
        self.cli.cmd_like(argparse_ns(room="R", seq=42, remove=False))
        r = sent["payload"]["reaction"]
        self.assertEqual(r["target_seq"], 42)
        self.assertEqual(r["action"], "add")
        # the server's valid_reaction_sidecar? enforces map_size==2 — any
        # extra key gets rejected as invalid_message (measured 2026-08-10)
        self.assertEqual(set(r), {"target_seq", "action"})
        self.assertEqual(sent["payload"]["text"], "❤️")
        self.cli.cmd_like(argparse_ns(room="R", seq=42, remove=True))
        self.assertEqual(sent["payload"]["reaction"]["action"], "remove")


class TestReadIsJudgment(Base):
    """[founder-approved 2026-08-10] A read mark is a signature of judgment,
    not a receipt of delivery — listen must never send read:mark by any
    path (backfill or realtime)."""

    def test_listen_records_but_never_marks_read(self):
        sent = []

        class FakeWS:
            def __init__(self):
                self._eof = False

            async def send(self, raw):
                sent.append(json.loads(raw))

            async def recv(self):
                f = sent[-1]
                return json.dumps([f[0], f[1], f[2], "phx_reply",
                                   {"status": "ok", "response": {}}])

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self._eof:
                    raise KeyboardInterrupt   # one lap, then bail out
                self._eof = True
                return json.dumps([None, None, "room:R", "message:new",
                                   {"seq": 7, "content": {}}])

            async def close(self):
                pass

        async def fake_connect(token):
            return FakeWS()

        self.cli.ws_connect = fake_connect
        self.cli.rest = lambda *a, **k: (200, {"messages": [
            {"seq": 5, "content": {}}, {"seq": 6, "content": {}}]})
        inbox = os.path.join(self.tmp, "inbox.jsonl")
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                asyncio.run(self.cli.do_listen(
                    {"access_token": "T", "nickname": "n"}, "R", inbox))
        marks = [f for f in sent if len(f) == 5 and f[3] == "read:mark"]
        self.assertEqual(marks, [],
                         "listen marked as read — forging the unread '1'"
                         " (the liveness indicator)")
        with open(inbox) as f:
            seqs = [json.loads(line)["payload"]["seq"] for line in f]
        self.assertEqual(seqs, [5, 6, 7], "backfill/realtime records missing")

    def test_unread_reports_without_marking(self):
        self._creds()
        paths = []

        def fake_rest(method, path, body=None, token=None):
            paths.append((method, path))
            if path == "/v1/rooms":
                return 200, {"rooms": [
                    {"id": "R1", "name": "Attic", "last_seq": 10,
                     "my_last_read_seq": 8, "members": []},
                    {"id": "R2", "name": "Calm", "last_seq": 3,
                     "my_last_read_seq": 3, "members": []}]}
            return 200, {"messages": [
                {"seq": 9, "sender_id": "u1",
                 "content": {"payload": {"type": "text", "text": "question"}}},
                {"seq": 10, "sender_id": "u1",
                 "content": {"payload": {"type": "text", "text": "answer"}}}]}

        self.cli.rest = fake_rest
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.cli.cmd_unread(argparse_ns(room=None, limit=100))
        out = buf.getvalue()
        self.assertIn("unread 2", out)
        self.assertNotIn("Calm", out, "a room with 0 unread showed up")
        # naming a room also prints the bodies — the material for judgment
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.cli.cmd_unread(argparse_ns(room="R1", limit=100))
        self.assertIn("question", buf.getvalue())
        # every call is a GET — looking must never create a read (a write)
        self.assertTrue(all(m == "GET" for m, _ in paths))

    def _creds(self):
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        self.cli.write_private(
            self.cli.cred_path("default"),
            lambda f: json.dump({"user_id": "me", "nickname": "Paren",
                                 "access_token": "SECRET-A"}, f))


class TestInviteLedger(Base):
    """[08-10 morning incident] +N is a use count, not capacity — wording
    and remaining-uses listing."""

    def _creds(self):
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        self.cli.write_private(
            self.cli.cred_path("default"),
            lambda f: json.dump({"user_id": "me", "nickname": "Paren",
                                 "access_token": "SECRET-A"}, f))

    def test_invite_explains_plus_n_semantics(self):
        self._creds()
        self.cli.rest = lambda *a, **k: (201, {"invite": {
            "url": "https://klatalk.com/r/c0de", "code": "c0de",
            "max_uses": 2, "expires_at": "2026-08-17T00:00:00Z"}})
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.cli.cmd_invite(argparse_ns(room="R", max_uses=2,
                                            ttl_days=None, open=False))
        json.loads(out.getvalue())          # stdout is still pure JSON
        self.assertIn("+2 uses", err.getvalue())
        self.assertIn("not the room's capacity", err.getvalue())

    def test_invites_shows_remaining_uses(self):
        self._creds()
        self.cli.rest = lambda *a, **k: (200, {"invites": [
            {"id": "i1", "created_by": "me", "max_uses": 5, "use_count": 2,
             "expires_at": "2026-08-17T00:00:00Z",
             "has_quiz": False, "quiz_locked": False},
            {"id": "i2", "created_by": "other", "max_uses": None,
             "use_count": 9, "expires_at": "2026-08-17T00:00:00Z",
             "has_quiz": True, "quiz_locked": True}]})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.cli.cmd_invites(argparse_ns(room="R", json=False))
        out = buf.getvalue()
        self.assertIn("+3 uses left", out)   # 5 - 2, the use-count grammar
        self.assertIn("issued by me", out)
        self.assertIn("unlimited", out)
        self.assertIn("quiz (locked)", out)

    def test_new_commands_registered(self):
        p = self.cli.build_parser()
        args = p.parse_args(["unread"])
        self.assertIsNone(args.room)
        args = p.parse_args(["unread", "R", "--limit", "5"])
        self.assertEqual((args.room, args.limit), ("R", 5))
        args = p.parse_args(["invites", "R", "--json"])
        self.assertTrue(args.json)


def argparse_ns(**kw):
    import argparse
    return argparse.Namespace(**kw)


class TestArgs(Base):
    """[2/6] --profile must also work after the subcommand (the skill's
    examples are written that way)."""

    def test_profile_accepted_after_subcommand(self):
        p = self.cli.build_parser()
        args = p.parse_args(["rooms", "--profile", "opus"])
        self.assertEqual(args.profile, "opus")
        args = p.parse_args(["--profile", "opus", "rooms"])
        self.assertEqual(args.profile, "opus")

    def test_zero_is_a_real_value(self):
        # [3/6] --max-uses 0 means "an invite that admits no one", not
        # "unspecified"
        args = self.cli.build_parser().parse_args(["invite", "R", "--max-uses", "0"])
        self.assertEqual(args.max_uses, 0)
        self.assertIsNotNone(args.max_uses)


class TestUntrustedText(Base):
    """[3/6] Room text is untrusted — terminal control chars get neutralized."""

    def test_control_chars_escaped(self):
        # Korean text intentionally kept: exercises non-ASCII handling
        # alongside the control-char escaping
        out = self.cli.clean("정상\x1b[2K\r가짜 프롬프트")
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\r", out)
        self.assertIn("정상", out)

    def test_summarize_never_raises(self):
        for payload in [{"type": "text", "text": "hello"},
                        {"type": "image", "url": "/uploads/a.jpg"},
                        {"type": "file"}, {"type": "system", "text": "joined"},
                        {"type": "unknown", "x": 1}, {}]:
            with self.subTest(p=payload):
                self.assertIsInstance(self.cli.summarize_payload(payload), str)


class TestApiBase(Base):
    def test_trailing_slash_normalized(self):
        # a trailing slash in KLATALK_API must not produce //v1/...
        self.assertFalse(self.cli.API.endswith("/"))
        self.assertTrue(self.cli.WS.startswith("wss://"))




class TestAiMarker(Base):
    """Dual-prefix [AI] detection — English canonical + Korean legacy.

    Anglicized 2026-08-10 (founder decision) with no flag day: live accounts
    registered before the change still carry the Korean prefix, and treating
    them as unmarked would break norm 3's reply budget for known AIs.
    """

    def test_is_ai_accepts_both_prefixes(self):
        self.assertTrue(self.cli.is_ai({"bio": "AI member · Claude Code"}))
        # Case/spacing variance is real once the prefix is English —
        # detection casefolds and strips leading whitespace
        self.assertTrue(self.cli.is_ai({"bio": "AI Member · GPT"}))
        self.assertTrue(self.cli.is_ai({"bio": " ai member · x"}))
        # Korean legacy prefix: intentionally kept — live wire convention
        self.assertTrue(self.cli.is_ai({"bio": "AI 멤버 · Codex"}))
        self.assertFalse(self.cli.is_ai({"bio": "just a member"}))
        self.assertFalse(self.cli.is_ai({"bio": None}))
        self.assertFalse(self.cli.is_ai({}))


class TestApprovalCanon(Base):
    """approval-v1 §1 — the canon IS the request: sorted keys, no spaces,
    axis derived from the action. Nobody re-canonicalizes downstream, so
    byte stability here is signature stability everywhere."""

    def test_canon_shape_and_axis(self):
        c = self.cli.canonical_request("room.invite.create", "r1", "u1",
                                       3600, 1)
        body = json.loads(c)
        self.assertEqual(list(body.keys()), sorted(body.keys()))
        self.assertEqual(body["approver_axis"], "room")
        self.assertEqual(body["v"], 1)
        # compact separators — no spaces anywhere
        self.assertNotIn(b" ", c)

        env = json.loads(self.cli.canonical_request(
            "envelope.custom", "r1", "u1", 60, 2))
        self.assertEqual(env["approver_axis"], "envelope")

    def test_canon_target_optional(self):
        with_target = json.loads(self.cli.canonical_request(
            "room.pin.set", "r1", "u1", 60, 1, target="42"))
        self.assertEqual(with_target["target"], "42")
        without = json.loads(self.cli.canonical_request(
            "room.pin.set", "r1", "u1", 60, 1))
        self.assertNotIn("target", without)


class TestSealedRooms(Base):
    """agent-mls M2 — sealed-room machinery that runs without a server.
    The single-ingress pump, the plaintext ledger, and the join routing."""

    def _creds(self):
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        self.cli.write_private(
            self.cli.cred_path("default"),
            lambda f: json.dump({"user_id": "u", "nickname": "P",
                                 "access_token": "T", "device_id": "D"}, f))
        return {"access_token": "T", "device_id": "D"}

    def test_quiz_fragment_and_b64url(self):
        self.assertEqual(self.cli.b64url_decode("AAAA"), b"\x00\x00\x00")
        m = self.cli.QUIZ_FRAGMENT.search(
            "https://klatalk.com/r/ab12cd34#q=QUJD.7Iqk7YWM7YKk")
        self.assertTrue(m)
        self.assertEqual(self.cli.b64url_decode(m.group(1)), b"ABC")

    def test_ledger_dedup_first_wins_and_0600(self):
        self.cli.ledger_append("default", "R",
                               [{"seq": 1, "kind": "application",
                                 "payload": {"t": "a"}}])
        # a crash between fsync and ack duplicates lines — first wins
        self.cli.ledger_append("default", "R",
                               [{"seq": 1, "kind": "application",
                                 "payload": {"t": "dup"}},
                                {"seq": 2, "kind": "handshake"}])
        recs = self.cli.ledger_read("default", "R")
        self.assertEqual([r["seq"] for r in recs], [1, 2])
        self.assertEqual(recs[0]["payload"]["t"], "a")
        mode = stat.S_IMODE(
            os.stat(self.cli.ledger_path("default", "R")).st_mode)
        self.assertEqual(mode, 0o600)

    def test_sealed_pump_receipts_ledger_ack_desync(self):
        import base64 as b64
        acks, skips = [], []

        def fake_rest(method, path, body=None, token=None):
            m = re.search(r"after_seq=(\d+)", path)
            if not m:
                return 200, {"rooms": []}    # get_room sweep (binding pass)
            after = int(m.group(1))
            rows = [
                {"seq": 1, "content": {"v": 1,
                                       "payload": {"type": "system"}}},
                {"seq": 2, "sender_id": "u1",
                 "content": {"v": 2, "payload": {"ct": "QQ=="}}},
                {"seq": 3, "sender_id": "u2",
                 "content": {"v": 2, "payload": {"ct": "Qg=="}}},
            ]
            return 200, {"messages": [r for r in rows if r["seq"] > after]}

        pt = b64.b64encode(
            json.dumps({"type": "text", "text": "hi"}).encode()).decode()

        def fake_mls(creds, profile, op, payload=None):
            if op == "cursor":
                return {"cursor": 0, "resume": 0}
            if op == "ingest":
                # only v2 rows reach the helper — the system line must not
                self.assertEqual([m["seq"] for m in payload["messages"]],
                                 [2, 3])
                return {"receipts": [
                    {"seq": 2, "replayed": False, "pruned": False,
                     "incoming": {"kind": "application", "sender": "dev1",
                                  "plaintext_b64": pt}},
                    {"seq": 3, "error": "process message: bad ciphertext"},
                ]}
            if op == "ingest-ack":
                acks.append(payload["upto_seq"])
                return {"removed": 1}
            if op == "ingest-skip":
                skips.append(payload["seq"])
                return {"ok": True}
            raise AssertionError(op)

        self.cli.rest = fake_rest
        self.cli.mls_op = fake_mls
        creds = self._creds()
        recs = self.cli.sealed_pump(creds, "default", "R")
        # Verdict (impl /133): an application decrypt failure does not
        # kill the room — advance with a placeholder + skip; desync is
        # for commit failures only
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["payload"]["text"], "hi")
        self.assertEqual(recs[0]["sender_id"], "u1")
        self.assertEqual(acks, [2])
        self.assertEqual(skips, [3])
        self.assertFalse(self.cli.is_desynced("default", "R"))
        led = {r["seq"]: r for r in self.cli.ledger_read("default", "R")}
        self.assertIn("unreadable", led[3]["payload"]["text"])

    def test_sealed_pump_commit_failure_desyncs(self):
        # Only a commit (hint none) failure and WrongEpoch (= we are
        # behind — the helper absorbs past-epoch echoes as Own) desync,
        # after one retry (§4-3, mini-review verdict)
        calls = {"n": 0}

        def fake_rest(method, path, body=None, token=None):
            after = int(re.search(r"after_seq=(\d+)", path).group(1))
            rows = [{"seq": 7, "sender_id": "u1",
                     "content": {"v": 2, "hint": "none",
                                 "payload": {"ct": "QQ=="}}}]
            return 200, {"messages": [r for r in rows if r["seq"] > after]}

        def fake_mls(c, p, op, payload=None):
            if op == "cursor":
                return {"cursor": 0, "resume": 0}
            if op == "ingest":
                calls["n"] += 1
                return {"receipts": [{"seq": 7, "error": "merge failed"}]}
            raise AssertionError(op)

        self.cli.rest = fake_rest
        self.cli.mls_op = fake_mls
        creds = self._creds()
        self.cli.sealed_pump(creds, "default", "RC")
        self.assertEqual(calls["n"], 2)     # verdict after one retry
        self.assertTrue(self.cli.is_desynced("default", "RC"))

    def test_join_routes_sealed_on_fragment(self):
        called = {}

        def fake_sealed_join(args, creds, profile, code, frag):
            called.update(code=code, frag=bool(frag))

        self.cli.sealed_join = fake_sealed_join
        self._creds()
        self.cli.cmd_join(argparse_ns(
            code="https://klatalk.com/r/abcd2345#q=QUJD.QQ"))
        self.assertEqual(called, {"code": "abcd2345", "frag": True})

    def test_invite_refuses_sealed_room(self):
        self._creds()
        self.cli.get_room = lambda creds, room: {"encryption_mode": "mls10"}
        with self.assertRaises(SystemExit):
            self.cli.cmd_invite(argparse_ns(room="R", max_uses=None,
                                            ttl_days=None))



    # ---- M3: roster verification, pins, sender binding ----

    def _leaf(self, dev, key_b64):
        return {"identity": dev, "signature_key_b64": key_b64}

    OWN_KEY = "c2VsZg=="   # b64("self")

    def _wire_verify(self, leaves, members, registry):
        def fake_mls(c, p, op, payload=None):
            if op == "list-members":
                return {"members": leaves}
            if op == "signing-public-key":
                return {"public_key_b64": self.OWN_KEY}
            raise AssertionError(op)
        self.cli.mls_op = fake_mls
        self.cli.get_room = lambda c, r: {"members": members}
        self.cli.device_key_map = lambda c, room: registry

    @staticmethod
    def _reg(uid, key, generation=1, history=None):
        return {"user_id": uid, "public_key": key,
                "generation": generation, "history": history or []}

    def test_verify_roster_pins_on_first_sight(self):
        creds = self._creds()
        self._wire_verify(
            leaves=[self._leaf("D", self.OWN_KEY), self._leaf("dev9", "a2V5")],
            members=[{"user_id": "u9"}],
            registry={"dev9": self._reg("u9", "a2V5")})
        self.assertTrue(self.cli.verify_roster(creds, "default", "R",
                                               quick=True))
        pins = self.cli.load_pins("default")
        self.assertEqual(pins["dev9"]["user_id"], "u9")
        # self leaf ("D") is never pinned or checked
        self.assertNotIn("D", pins)

    def test_verify_roster_pin_beats_registry(self):
        creds = self._creds()
        # pin says one key; leaf AND registry agree on another — a rewritten
        # registry must not launder a swapped leaf (§5-3)
        self.cli.save_pins("default",
                           {"dev9": {"user_id": "u9", "key": "b2xk"}})
        self._wire_verify(
            leaves=[self._leaf("dev9", "bmV3")],
            members=[{"user_id": "u9"}],
            registry={"dev9": self._reg("u9", "bmV3")})
        self.assertFalse(self.cli.verify_roster(creds, "default", "R",
                                                quick=True))
        self.assertTrue(self.cli.is_unverified("default", "R"))
        # durable — a fresh verify refuses without re-sweeping
        self.assertFalse(self.cli.verify_roster(creds, "default", "R",
                                                quick=True))

    def test_verify_roster_unresolved_holds_send(self):
        creds = self._creds()
        self._wire_verify(
            leaves=[self._leaf("D", self.OWN_KEY),
                    self._leaf("ghost", "a2V5")],
            members=[{"user_id": "u1"}],
            registry={})
        self.assertFalse(self.cli.verify_roster(creds, "default", "R",
                                                quick=True))
        # not a durable block — the joiner's ack may just not have landed
        self.assertFalse(self.cli.is_unverified("default", "R"))

    def test_pin_match_skips_registry_and_rotation_repins(self):
        creds = self._creds()
        # ① A pin match IS the completed verification — even when the
        #   registry no longer answers for that device (member left,
        #   device revoked), sending must not be blocked (app parity)
        self.cli.save_pins("default",
                           {"gone": {"user_id": "uG", "key": "a2V5",
                                     "generation": 1}})
        self._wire_verify(
            leaves=[self._leaf("gone", "a2V5")],
            members=[],          # a member gone from the registry
            registry={})
        self.assertTrue(self.cli.verify_roster(creds, "default", "R",
                                               quick=True))
        # ② A legitimate rekey: re-pin when the append-only history
        #   explains pinned generation → current contiguously — not a
        #   permanent block (§5-6)
        self.cli.save_pins("default",
                           {"dev9": {"user_id": "u9", "key": "b2xk",
                                     "generation": 1}})
        hist = [{"generation": 1, "public_key": "b2xk"},
                {"generation": 2, "public_key": "bmV3"}]
        self._wire_verify(
            leaves=[self._leaf("dev9", "bmV3")],
            members=[{"user_id": "u9"}],
            registry={"dev9": self._reg("u9", "bmV3", 2, hist)})
        self.assertTrue(self.cli.verify_roster(creds, "default", "R2",
                                               quick=True))
        self.assertEqual(self.cli.load_pins("default")["dev9"]["generation"],
                         2)

    def test_sender_binding_flag_in_pump(self):
        import base64 as b64
        creds = self._creds()
        self.cli.save_pins("default",
                           {"dev1": {"user_id": "uREAL", "key": "k"}})

        def fake_rest(method, path, body=None, token=None):
            after = int(re.search(r"after_seq=(\d+)", path).group(1))
            rows = [{"seq": 5, "sender_id": "uFAKE",
                     "content": {"v": 2, "payload": {"ct": "QQ=="}}}]
            return 200, {"messages": [r for r in rows if r["seq"] > after]}

        pt = b64.b64encode(json.dumps({"type": "text",
                                       "text": "x"}).encode()).decode()

        def fake_mls(c, p, op, payload=None):
            if op == "cursor":
                return {"cursor": 0, "resume": 0}
            if op == "ingest":
                return {"receipts": [
                    {"seq": 5, "incoming": {"kind": "application",
                                            "sender": "dev1",
                                            "plaintext_b64": pt}}]}
            if op == "ingest-ack":
                return {"removed": 1}
            raise AssertionError(op)

        self.cli.rest = fake_rest
        self.cli.mls_op = fake_mls
        recs = self.cli.sealed_pump(creds, "default", "R")
        self.assertTrue(recs[0]["sender_binding_failed"])
        line = self.cli.sealed_record_line(recs[0], {})
        self.assertIn("sender binding failed", line)



    def test_message_line_shows_reply_arrow(self):
        # A reply must show what it points at on the text surface too —
        # field report: the arrow lived only in the JSON and vanished
        # from the agent's view
        line = self.cli.message_line(
            {"seq": 15, "sender_id": "u1", "reply_to_seq": 10,
             "content": {"payload": {"type": "text", "text": "sounds good"}}},
            {"u1": ("Alex", False)})
        self.assertIn("↩#10", line)
        plain = self.cli.message_line(
            {"seq": 16, "sender_id": "u1",
             "content": {"payload": {"type": "text", "text": "no reason"}}},
            {"u1": ("Alex", False)})
        self.assertNotIn("↩", plain)


if __name__ == "__main__":
    unittest.main()
