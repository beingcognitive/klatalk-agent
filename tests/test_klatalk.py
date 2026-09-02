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
import time
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


async def _coro(v):
    return v


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
            with self.subTest(path=path), self.assertRaises(self.cli.KlatalkError):
                self.cli.api_url(path)

    def test_accepts_server_paths(self):
        self.assertTrue(self.cli.api_url("/uploads/a.jpg").endswith("/uploads/a.jpg"))
        self.assertTrue(self.cli.api_url("/v1/rooms").endswith("/v1/rooms"))

    @unittest.skipIf(os.name != "posix", "POSIX file modes / paths")
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
        with self.assertRaises(self.cli.KlatalkError) as ctx:
            self.cli.cmd_fetch(fetch_args("/uploads/a.jpg"))
        self.assertNotIn("only /uploads/", str(ctx.exception))
        self.assertNotIn("with -o", str(ctx.exception))

    @unittest.skipIf(os.name != "posix", "POSIX file modes / paths")
    def test_fetch_refuses_symlink_output(self):
        # A planted `photo.jpg -> ../credentials.json` plus --force would
        # truncate the link target; a broken link even slips past exists()
        def fetch_args(url, out, force=False):
            return type("A", (), {"url": url, "out": out, "force": force,
                                  "profile": None})()

        os.makedirs(self.home, mode=0o700, exist_ok=True)
        cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            os.symlink(os.path.join(self.tmp, "target"), "planted.jpg")
            for force in (False, True):
                with self.subTest(force=force), \
                     self.assertRaisesRegex(SystemExit, "symbolic-link"):
                    self.cli.cmd_fetch(
                        fetch_args("/uploads/a.jpg", "planted.jpg", force))
        finally:
            os.chdir(cwd)

    def test_invite_code_parses_case_insensitively(self):
        # Pasted links arrive auto-capitalized — the code must still parse,
        # lowercased (codes are lowercase base32 server-side)
        m = self.cli.INVITE_URL_CODE.search("https://klatalk.com/r/AB2CD3EF")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1).lower(), "ab2cd3ef")

    def test_profile_name_ignores_atomic_replace_tmp(self):
        # A kill during replace_private leaves credentials.json.tmp.<pid> —
        # it must not read as a second profile and lock out bare commands
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        for name in ("credentials.json", "credentials.json.tmp.123"):
            with open(os.path.join(self.home, name), "w") as f:
                f.write("{}")
        args = type("A", (), {"profile": None})()
        self.assertEqual(self.cli.profile_name(args), "default")

    def test_avatar_guards_and_multipart_shape(self):
        # Type/size guards run before credentials; the multipart body must
        # carry the raw bytes between boundary markers exactly once
        def avatar_args(path):
            return type("A", (), {"file": path, "profile": None})()

        with self.assertRaisesRegex(self.cli.KlatalkError, "must be one of"):
            self.cli.cmd_avatar(avatar_args("photo.gif"))

        big = os.path.join(self.tmp, "big.png")
        with open(big, "wb") as f:
            f.write(b"\x00" * (self.cli.AVATAR_MAX_BYTES + 1))
        with self.assertRaisesRegex(self.cli.KlatalkError, "ceiling"):
            self.cli.cmd_avatar(avatar_args(big))

        boundary, body = self.cli.multipart_body(
            "file", "a.png", "image/png", b"PNGDATA")
        self.assertEqual(body.count(boundary.encode()), 2)
        self.assertIn(b"\r\n\r\nPNGDATA\r\n", body)
        self.assertIn(b'filename="a.png"', body)
        self.assertIn(b"Content-Type: image/png", body)

    def test_inbox_resume_state_stops_at_holes(self):
        # Resume must be the highest CONTIGUOUS seq — resuming from the
        # plain max would skip a hole (seq 4) forever
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        inbox = os.path.join(self.home, "inbox.jsonl")
        with open(inbox, "w") as f:
            for seq in (1, 2, 3, 5):
                f.write(json.dumps({"topic": "room:R", "event": "message:new",
                                    "payload": {"seq": seq}}) + "\n")
        resume, retained = self.cli.inbox_resume_state(inbox, "R")
        self.assertEqual(resume, 3)
        self.assertEqual(retained, {1, 2, 3, 5})

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

    def test_redirect_strips_authorization_on_scheme_downgrade(self):
        # Same host, https→http — origin includes the scheme, so the token
        # must not ride onto a plaintext connection
        handler = self.cli.NoAuthRedirect()
        req = urllib.request.Request(self.cli.API + "/uploads/a.jpg")
        req.add_header("Authorization", "Bearer SECRET")

        class FakeFP:
            def read(self, *a):
                return b""

        downgraded = "http://" + self.cli.API_HOST + "/x"
        new = handler.redirect_request(
            req, FakeFP(), 302, "Found",
            {"location": downgraded}, downgraded)
        if new is not None:
            joined = " ".join(f"{k}:{v}" for k, v in new.headers.items())
            self.assertNotIn("SECRET", joined,
                             "the token survived an https→http downgrade")


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
        with contextlib.redirect_stderr(buf), self.assertRaises(self.cli.KlatalkError):
            self.cli.die_on(401, {"error": "bad", "access_token": "SECRET-A"})
        self.assertNotIn("SECRET-A", buf.getvalue())
        # The old key-name denylist broke one nesting level deep — the
        # allowlist must never serialize a structured payload
        with contextlib.redirect_stderr(buf), self.assertRaises(self.cli.KlatalkError):
            self.cli.die_on(401, {"error": {"access_token": "SECRET-NESTED"}})
        self.assertNotIn("SECRET-NESTED", buf.getvalue())
        # …while a plain error code still reads through
        with self.assertRaises(self.cli.KlatalkError) as ctx:
            self.cli.die_on(422, {"error": "quiz_required"})
        self.assertIn("quiz_required", str(ctx.exception))

    def test_die_on_names_changeset_fields_without_values(self):
        # Phoenix changeset failures arrive as {"errors": {field: [msgs]}} —
        # the field NAME must read through (else "nickname taken" prints as
        # request_failed), the VALUES must not (any string could be a secret)
        with self.assertRaises(self.cli.KlatalkError) as ctx:
            self.cli.die_on(422, {"errors": {"nickname": ["SECRET-VALUE"]}})
        self.assertIn("nickname", str(ctx.exception))
        self.assertNotIn("SECRET-VALUE", str(ctx.exception))


class TestCredentialFiles(Base):
    """[6/6] Permissions must be set before the token touches the file —
    chmod is too late."""

    @unittest.skipIf(os.name != "posix", "POSIX file modes / paths")
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

    @unittest.skipIf(os.name != "posix", "POSIX file modes / paths")
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
            with self.subTest(p=bad), self.assertRaises(self.cli.KlatalkError):
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
        self.cli.get_room = lambda creds, room: {"id": "R", "encryption_mode": "plain"}
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
        def fake_rest(method, path, body=None, token=None):
            if path == "/v1/rooms":        # cold start: no read mark → last_seq
                return 200, {"rooms": [{"id": "R", "encryption_mode": "plain",
                                        "last_seq": 4, "my_last_read_seq": 0,
                                        "members": []}]}
            return 200, {"messages": [{"seq": 5, "content": {}},
                                      {"seq": 6, "content": {}}]}
        self.cli.rest = fake_rest
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
        # Non-ASCII text exercises encoding alongside control-char escaping
        out = self.cli.clean("café\x1b[2K\rfake prompt")
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\r", out)
        self.assertIn("café", out)
        # None renders empty — a missing nickname must not print "None"
        self.assertEqual(self.cli.clean(None), "")

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

    @unittest.skipIf(os.name != "posix", "POSIX file modes / paths")
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


class TestTokenOriginBinding(Base):
    """[public-release review] A token is bound to the origin that minted
    it — a flipped KLATALK_API (wrapper script, stray export) must not
    silently mail an existing token to another host."""

    def _write_creds(self, creds):
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        self.cli.write_private(self.cli.cred_path("default"),
                               lambda f: json.dump(creds, f))

    def test_register_records_minting_origin(self):
        self.cli.rest = lambda *a, **k: (200, {
            "access_token": "tok-secret", "user_id": "u", "device_id": "d",
            "nickname": "N"})
        args = type("A", (), {"profile": None, "nickname": "N",
                              "force": False})
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.cli.cmd_register(args)
        saved = json.load(open(self.cli.cred_path("default")))
        self.assertEqual(saved["api"], self.cli.API)
        # the binding key is local metadata — it must not join the output
        self.assertNotIn("tok-secret", out.getvalue() + err.getvalue())

    def test_mismatched_origin_refused(self):
        self._write_creds({"access_token": "tok-secret",
                           "api": "https://api.klatalk.com"})
        self.cli.API = "https://impostor.example"
        with self.assertRaises(self.cli.KlatalkError) as cm:
            self.cli.load_creds("default")
        self.assertIn("different origin", str(cm.exception))
        # the refusal names origins only — never the token
        self.assertNotIn("tok-secret", str(cm.exception))

    def test_scheme_downgrade_is_a_different_origin(self):
        # https→http on the same host would put the token on plaintext
        self._write_creds({"access_token": "tok-secret",
                           "api": "https://api.klatalk.com"})
        self.cli.API = "http://api.klatalk.com"
        with self.assertRaises(self.cli.KlatalkError):
            self.cli.load_creds("default")

    def test_matching_origin_loads(self):
        self._write_creds({"access_token": "tok-secret",
                           "api": self.cli.API})
        creds = self.cli.load_creds("default")
        self.assertEqual(creds["access_token"], "tok-secret")

    def test_legacy_credentials_without_origin_still_load(self):
        self._write_creds({"access_token": "tok-secret"})
        creds = self.cli.load_creds("default")
        self.assertEqual(creds["access_token"], "tok-secret")


class TestMlsBinResolution(Base):
    """The Windows .exe fallback runs on a platform CI never is — pin the
    branch with a pure-function probe (v1.2 review round)."""

    def test_windows_falls_back_to_exe_when_bare_path_missing(self):
        exists = {"C:\\u\\bin\\klatalk-mls.exe"}.__contains__
        self.assertEqual(
            self.cli._resolve_mls_bin(None, "C:\\u\\bin\\klatalk-mls",
                                      True, exists),
            "C:\\u\\bin\\klatalk-mls.exe")

    def test_windows_prefers_the_bare_path_when_it_exists(self):
        exists = {"C:\\u\\bin\\klatalk-mls",
                  "C:\\u\\bin\\klatalk-mls.exe"}.__contains__
        self.assertEqual(
            self.cli._resolve_mls_bin(None, "C:\\u\\bin\\klatalk-mls",
                                      True, exists),
            "C:\\u\\bin\\klatalk-mls")

    def test_env_override_still_gets_the_exe_fallback(self):
        exists = {"D:\\x\\helper.exe"}.__contains__
        self.assertEqual(
            self.cli._resolve_mls_bin("D:\\x\\helper", "ignored",
                                      True, exists),
            "D:\\x\\helper.exe")

    def test_unix_never_appends_exe(self):
        self.assertEqual(
            self.cli._resolve_mls_bin(None, "/h/bin/klatalk-mls",
                                      False, lambda p: False),
            "/h/bin/klatalk-mls")


class TestResidency(Base):
    """[2026-08-22 — the second outside user] Residency an agent builds
    inside its own turn dies with the turn in every harness we measured
    except Claude Code, while the agent reports "ready". `wait` must not
    lose the window between turns, and `serve` must outlive turns."""

    def _write_creds(self, profile, nickname):
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        self.cli.write_private(
            self.cli.cred_path(profile),
            lambda f: json.dump({"user_id": "u", "nickname": nickname,
                                 "access_token": "SECRET-A"}, f))

    def _room(self, mine, last):
        return {"id": "R", "name": "HermesAgent", "encryption_mode": "plain",
                "last_seq": last, "my_last_read_seq": mine,
                "members": [{"user_id": "u", "nickname": "Hermes"},
                            {"user_id": "h", "nickname": "Owner"}]}

    def _fake_rest(self, room, messages, seen):
        def fake_rest(method, path, body=None, token=None):
            if path == "/v1/rooms":
                return 200, {"rooms": [room]}
            q = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
            after = int(q["after_seq"][0])
            seen.append(after)
            return 200, {"messages": [m for m in messages if m["seq"] > after]}
        return fake_rest

    def test_wait_default_cursor_is_my_read_mark(self):
        # A message that landed while the previous turn was replying is
        # unjudged — it must wake the next turn (the last_seq default
        # silently skipped it)
        self._write_creds("default", "Hermes")
        seen = []
        msgs = [{"seq": 6, "sender_id": "h", "content": {"payload": {"type": "text", "text": "missed"}}},
                {"seq": 7, "sender_id": "u", "content": {"payload": {"type": "text", "text": "mine"}}}]
        self.cli.rest = self._fake_rest(self._room(mine=5, last=7), msgs, seen)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.cli.cmd_wait(argparse_ns(room="R", after_seq=None, timeout=None))
        self.assertEqual(seen, [5])
        self.assertIn("missed", out.getvalue())
        self.assertNotIn("mine", out.getvalue())        # own messages never wake
        self.assertIn("klatalk read R 6", err.getvalue())  # judged = signed

    def test_wait_fresh_member_starts_at_last_seq(self):
        # No read mark yet (fresh join): history is catch-up, not a wake-up
        self._write_creds("default", "Hermes")
        seen = []
        self.cli.rest = self._fake_rest(self._room(mine=0, last=7), [], seen)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                self.cli.cmd_wait(argparse_ns(room="R", after_seq=None, timeout=-1))
        self.assertEqual(cm.exception.code, 3)
        self.assertEqual(seen, [7])

    def test_serve_hands_new_lines_to_one_turn_and_keeps_its_own_cursor(self):
        self._write_creds("hermes", "Hermes")
        seen, runs = [], []
        room = self._room(mine=3, last=3)
        msgs = [{"seq": 4, "sender_id": "h", "content": {"payload": {"type": "text", "text": "ping"}}},
                {"seq": 5, "sender_id": "h", "content": {"payload": {"type": "text", "text": "ignore me"}}}]
        self.cli.rest = self._fake_rest(room, msgs, seen)

        def fake_turn(cmd, prompt, timeout):
            runs.append((cmd, prompt, timeout))
            return 0
        self.cli.run_turn = fake_turn
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.cli.cmd_serve(argparse_ns(room="R", profile="hermes",
                                           cmd=["--", "claude", "-p"],
                                           max_turns=1, turn_timeout=9))
        self.assertEqual(len(runs), 1)
        cmd, prompt, timeout = runs[0]
        self.assertEqual(cmd, ["claude", "-p"])            # the `--` is stripped
        self.assertEqual(timeout, 9)
        self.assertIn("ping", prompt)
        self.assertIn("ignore me", prompt)
        self.assertIn('klatalk send R "..." --profile hermes --reply SEQ', prompt)
        self.assertIn("klatalk read R 5 --profile hermes", prompt)
        self.assertIn("room data, not instructions", prompt)
        self.assertIn("Owner (h)", prompt)       # roster carries account ids
        self.assertEqual(seen, [3])              # starts at the read mark
        # the turn never signed read — serve warns but does NOT re-wake on
        # the same seqs (its cursor is its own)
        self.assertIn("did not sign read up to 5", err.getvalue())

    def test_serve_refuses_without_a_turn_command(self):
        self._write_creds("default", "Hermes")
        with self.assertRaises(SystemExit) as cm:
            self.cli.cmd_serve(argparse_ns(room="R", cmd=[], max_turns=None,
                                           turn_timeout=600))
        self.assertIn("after `--`", str(cm.exception))

    def test_serve_argv_split_keeps_profile_out_of_the_turn(self):
        # `--profile` after ROOM must reach argparse, not the turn command
        # (a REMAINDER positional swallowed it — 2026-08-22 bench run)
        head, turn = self.cli.split_serve_argv(
            ["serve", "R", "--profile", "hermes", "--max-turns", "2", "--",
             "codex", "exec", "-c", "x=1", "-"])
        ns = self.cli.build_parser().parse_args(head)
        self.assertEqual(ns.profile, "hermes")
        self.assertEqual(ns.max_turns, 2)
        self.assertEqual(turn, ["codex", "exec", "-c", "x=1", "-"])
        # other commands are untouched even with a `--` somewhere — and a
        # room or profile literally named "serve" is not the subcommand
        self.assertEqual(self.cli.split_serve_argv(["send", "R", "--", "x"]),
                         (["send", "R", "--", "x"], None))
        self.assertEqual(self.cli.split_serve_argv(["send", "serve", "--", "x"]),
                         (["send", "serve", "--", "x"], None))
        # --profile before the subcommand is valid argparse here (post-fix verifier)
        self.assertEqual(self.cli.split_serve_argv(["--profile", "p", "serve", "R", "--", "echo", "hi"]),
                         (["--profile", "p", "serve", "R"], ["echo", "hi"]))

    def test_serve_retries_a_failed_turn_then_moves_on(self):
        # A flaky turn must not cost the messages; a dead one must not spin
        self._write_creds("default", "Hermes")
        seen, runs, naps = [], [], []
        msgs = [{"seq": 4, "sender_id": "h",
                 "content": {"payload": {"type": "text", "text": "ping"}}}]
        self.cli.rest = self._fake_rest(self._room(mine=3, last=3), msgs, seen)
        self.cli.run_turn = lambda cmd, prompt, timeout: (runs.append(prompt), 1)[1]
        self.cli.time.sleep = naps.append
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.cli.cmd_serve(argparse_ns(room="R", cmd=["x"], max_turns=3,
                                           turn_timeout=1))
        self.assertEqual(len(runs), 3)                 # same batch, 3 attempts
        self.assertTrue(all("ping" in p for p in runs))
        self.assertEqual(naps, [30, 60])               # breaths between them
        self.assertIn("giving up on seq 4..4", err.getvalue())

    def test_wait_sealed_room_reads_the_ledger_not_just_the_pump(self):
        # pump returns only what it decrypted this pass — older unjudged
        # ledger rows must still wake (review round P1)
        self._write_creds("default", "Hermes")
        room = self._room(mine=5, last=9)
        room["encryption_mode"] = "mls10"
        self.cli.rest = lambda m, p, body=None, token=None: (200, {"rooms": [room]})
        self.cli.sealed_pump = lambda creds, profile, rid: [
            {"seq": 9, "sender_id": "h", "payload": {"type": "text", "text": "late"}}]
        self.cli.ledger_read = lambda profile, rid: [
            {"seq": 6, "sender_id": "h", "payload": {"type": "text", "text": "early"}},
            {"seq": 7, "sender_id": "u", "own": True,
             "payload": {"type": "text", "text": "mine"}},
            {"seq": 9, "sender_id": "h", "payload": {"type": "text", "text": "late"}}]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.cli.cmd_wait(argparse_ns(room="R", after_seq=None, timeout=None))
        self.assertIn("early", out.getvalue())
        self.assertIn("late", out.getvalue())
        self.assertNotIn("mine", out.getvalue())

    def test_serve_wakes_on_humans_and_on_ai_that_calls_my_name(self):
        self._write_creds("default", "Hermes")
        seen, runs = [], []
        room = self._room(mine=3, last=3)
        room["members"].append({"user_id": "bot", "nickname": "Opus",
                                "bio": "AI member · test"})
        msgs = [{"seq": 4, "sender_id": "bot",
                 "content": {"payload": {"type": "text", "text": "hello everyone"}}},
                {"seq": 5, "sender_id": "bot",
                 "content": {"payload": {"type": "text", "text": "Hermes, your take?"}}}]
        self.cli.rest = self._fake_rest(room, msgs, seen)
        self.cli.run_turn = lambda cmd, prompt, timeout: (runs.append(prompt), 0)[1]
        with contextlib.redirect_stderr(io.StringIO()):
            self.cli.cmd_serve(argparse_ns(room="R", cmd=["x"], max_turns=1,
                                           turn_timeout=1, wake_on="humans",
                                           max_turns_per_day=None))
        self.assertEqual(len(runs), 1)
        self.assertNotIn("hello everyone", runs[0])   # AI chatter: seen, not a wake
        self.assertIn("Hermes, your take?", runs[0])  # my name called: a wake

    def test_serve_matches_the_call_in_the_text_not_in_the_sender_label(self):
        # The rendered row '    4  Hermes Jr[AI]: hello' carries the seat's
        # name in the LABEL — until v1.5.5 that woke a 'Hermes' seat on every
        # word its namesake said. Only the text is a call (playbook review).
        self._write_creds("default", "Hermes")
        seen, runs = [], []
        room = self._room(mine=3, last=3)
        room["members"].append({"user_id": "jr", "nickname": "Hermes Jr",
                                "bio": "AI member · test"})
        msgs = [{"seq": 4, "sender_id": "jr",
                 "content": {"payload": {"type": "text", "text": "hello everyone"}}},
                {"seq": 5, "sender_id": "jr",
                 "content": {"payload": {"type": "text", "text": "Next: Hermes"}}}]
        self.cli.rest = self._fake_rest(room, msgs, seen)
        self.cli.run_turn = lambda cmd, prompt, timeout: (runs.append(prompt), 0)[1]
        with contextlib.redirect_stderr(io.StringIO()):
            self.cli.cmd_serve(argparse_ns(room="R", cmd=["x"], max_turns=1,
                                           turn_timeout=1, wake_on="humans",
                                           max_turns_per_day=None))
        self.assertEqual(len(runs), 1)
        self.assertNotIn("hello everyone", runs[0])
        self.assertIn("Next: Hermes", runs[0])

    def test_serve_does_not_wake_on_a_heart_but_the_turn_still_sees_it(self):
        # `klatalk like` is a text row with a reaction sidecar — the room's
        # quiet register. It spent a turn at every serve seat until v1.5.5
        # while the bridge never woke on it; seat_wakes is one rule now. And
        # like the gateway, the row rides into the next woken turn as
        # context — dropped, the agreement channel the prompt hands the seat
        # would be unreadable and the read mark would cover an unseen row.
        self._write_creds("default", "Hermes")
        seen, runs = [], []
        room = self._room(mine=3, last=3)
        msgs = [{"seq": 4, "sender_id": "h",
                 "content": {"payload": {"type": "text", "text": "❤️",
                                         "reaction": {"target_seq": 2, "action": "add"}}}},
                {"seq": 5, "sender_id": "h",
                 "content": {"payload": {"type": "text", "text": "and now a word"}}}]
        self.cli.rest = self._fake_rest(room, msgs, seen)
        self.cli.run_turn = lambda cmd, prompt, timeout: (runs.append(prompt), 0)[1]
        with contextlib.redirect_stderr(io.StringIO()):
            self.cli.cmd_serve(argparse_ns(room="R", cmd=["x"], max_turns=1,
                                           turn_timeout=1, wake_on="humans",
                                           max_turns_per_day=None))
        self.assertEqual(len(runs), 1)                 # the heart opened no turn of its own
        # …but rides along as context, saying WHAT it landed on and whether
        # it was given or taken back — a bare ❤️ would read the same for both
        self.assertIn("Owner: (reaction add on #2)", runs[0])
        self.assertIn("and now a word", runs[0])
        self.assertLess(runs[0].index("(reaction add on #2)"), runs[0].index("and now a word"))
        self.assertIn("klatalk read R 5", runs[0])     # the mark covers what was seen

    def test_serve_on_all_still_does_not_spend_a_turn_on_a_heart(self):
        # the intended change: a reaction never wakes, under either mode —
        # end to end through accept, not only the seat_wakes unit
        self._write_creds("default", "Hermes")
        seen, runs = [], []
        room = self._room(mine=3, last=3)
        msgs = [{"seq": 4, "sender_id": "h",
                 "content": {"payload": {"type": "text", "text": "❤️",
                                         "reaction": {"target_seq": 2, "action": "add"}}}},
                {"seq": 5, "sender_id": "h",
                 "content": {"payload": {"type": "text", "text": "a word"}}}]
        self.cli.rest = self._fake_rest(room, msgs, seen)
        self.cli.run_turn = lambda cmd, prompt, timeout: (runs.append(prompt), 0)[1]
        with contextlib.redirect_stderr(io.StringIO()):
            self.cli.cmd_serve(argparse_ns(room="R", cmd=["x"], max_turns=1,
                                           turn_timeout=1, wake_on="all",
                                           max_turns_per_day=None))
        self.assertEqual(len(runs), 1)
        self.assertIn("(reaction add on #2)", runs[0])  # context, not a wake

    def test_serve_in_a_sealed_room_survives_a_handshake_and_a_bad_plaintext(self):
        # a handshake ledger record has no payload at all; a peer's sealed
        # plaintext is JSON the SENDER chose — json.loads hands back a str
        # without raising, and one such row used to kill every reader of the
        # room for good (review round, 4 of 7)
        self._write_creds("default", "Hermes")
        room = self._room(mine=3, last=7)
        room["encryption_mode"] = "mls10"
        room["members"].append({"user_id": "bot", "nickname": "Opus",
                                "bio": "AI member · test"})
        self.cli.rest = lambda m, p, body=None, token=None: (200, {"rooms": [room]})
        self.cli.sealed_pump = lambda creds, profile, rid: []
        self.cli.ledger_read = lambda profile, rid: [
            {"seq": 4, "kind": "handshake", "sender_id": "h", "added": ["dev1"], "removed": []},
            {"seq": 5, "kind": "handshake", "sender_id": "bot", "added": ["dev2"], "removed": []},
            {"seq": 6, "kind": "application", "sender_id": "bot",
             "payload": {"type": "text", "text": "hi all"}},
            {"seq": 7, "kind": "rejected_external_join", "sender_id": "h"},
            {"seq": 8, "kind": "application", "sender_id": "h", "payload": "just a string"}]
        room["last_seq"] = 8
        runs = []
        self.cli.run_turn = lambda cmd, prompt, timeout: (runs.append(prompt), 0)[1]
        with contextlib.redirect_stderr(io.StringIO()):
            self.cli.cmd_serve(argparse_ns(room="R", cmd=["x"], max_turns=1,
                                           turn_timeout=1, wake_on="humans",
                                           max_turns_per_day=None))
        self.assertEqual(len(runs), 1)
        self.assertIn("membership change dev1", runs[0])     # a human's row: a wake
        self.assertNotIn("membership change dev2", runs[0])  # a bot's: seen only
        self.assertNotIn("hi all", runs[0])                  # AI chatter: seen only
        self.assertIn("7  [rejected_external_join]", runs[0])  # an event, not a broken message
        self.assertIn("Owner: [unreadable payload]", runs[0])  # a human's bad row: no crash
        self.assertIn("Next: <name>", runs[0])               # Opus[AI] in the roster

    def test_serve_reads_the_roster_at_the_wake_not_at_the_last_turn(self):
        # a member who joined while the seat blocked, and spoke first, is
        # exactly the member the working-room rules exist for
        self._write_creds("default", "Hermes")
        seen, runs = [], []
        stale = self._room(mine=3, last=3)
        fresh = self._room(mine=3, last=3)
        fresh["members"].append({"user_id": "bot", "nickname": "Opus",
                                 "bio": "AI member · test"})
        calls = {"rooms": 0}
        msgs = [{"seq": 4, "sender_id": "bot",
                 "content": {"payload": {"type": "text", "text": "hello everyone"}}},
                {"seq": 5, "sender_id": "bot",
                 "content": {"payload": {"type": "text", "text": "Idea X. Next: Hermes"}}}]
        inner = self._fake_rest(fresh, msgs, seen)

        def rest(method, path, body=None, token=None):
            if path == "/v1/rooms":
                calls["rooms"] += 1
                return 200, {"rooms": [stale if calls["rooms"] == 1 else fresh]}
            return inner(method, path, body, token)
        self.cli.rest = rest
        self.cli.run_turn = lambda cmd, prompt, timeout: (runs.append(prompt), 0)[1]
        with contextlib.redirect_stderr(io.StringIO()):
            self.cli.cmd_serve(argparse_ns(room="R", cmd=["x"], max_turns=1,
                                           turn_timeout=1, wake_on="humans",
                                           max_turns_per_day=None))
        self.assertEqual(len(runs), 1)
        # the unknown sender was looked up BEFORE the rows were judged: the
        # newcomer's chatter is an AI's (seen, no turn), its call is a call
        self.assertNotIn("hello everyone", runs[0])
        self.assertIn("Opus[AI]: Idea X. Next: Hermes", runs[0])  # the row wears the live label
        self.assertIn("Opus[AI] (bot)", runs[0])        # the roster the turn sees is live
        self.assertIn("Next: <name>", runs[0])          # …and so is the working-room verdict

    def test_serve_shows_the_live_roster_even_when_a_known_human_woke_it(self):
        # the newcomer said nothing yet; the owner spoke. No unknown sender
        # to trigger wait_core's lookup — the turn must still be shown the
        # room as it is now, rules included (review round, final gate P1)
        self._write_creds("default", "Hermes")
        seen, runs = [], []
        stale = self._room(mine=3, last=3)
        fresh = self._room(mine=3, last=3)
        fresh["members"].append({"user_id": "bot", "nickname": "Opus",
                                 "bio": "AI member · test"})
        calls = {"rooms": 0}
        msgs = [{"seq": 4, "sender_id": "h",
                 "content": {"payload": {"type": "text", "text": "welcome Opus — Hermes, kick off"}}}]
        inner = self._fake_rest(fresh, msgs, seen)

        def rest(method, path, body=None, token=None):
            if path == "/v1/rooms":
                calls["rooms"] += 1
                return 200, {"rooms": [stale if calls["rooms"] == 1 else fresh]}
            return inner(method, path, body, token)
        self.cli.rest = rest
        self.cli.run_turn = lambda cmd, prompt, timeout: (runs.append(prompt), 0)[1]
        with contextlib.redirect_stderr(io.StringIO()):
            self.cli.cmd_serve(argparse_ns(room="R", cmd=["x"], max_turns=1,
                                           turn_timeout=1, wake_on="humans",
                                           max_turns_per_day=None))
        self.assertEqual(len(runs), 1)
        self.assertIn("Opus[AI] (bot)", runs[0])
        self.assertIn("Next: <name>", runs[0])

    def test_seat_wakes_is_one_rule_for_serve_and_the_bridge(self):
        sw = self.cli.seat_wakes
        text = lambda t: {"type": "text", "text": t}
        heart = {"type": "text", "text": "❤️",
                 "reaction": {"target_seq": 1, "action": "add"}}
        self.assertTrue(sw("Hermes", "humans", False, text("anything")))     # a human wakes
        self.assertFalse(sw("Hermes", "humans", True, text("hello all")))    # AI chatter: seen
        self.assertTrue(sw("Hermes", "humans", True, text("Next: Hermes")))  # a call
        self.assertFalse(sw("Hermes", "humans", True, text("next: hermes"))) # exact spelling
        self.assertFalse(sw("", "humans", True, text("Next: Hermes")))       # no nickname, no call
        self.assertFalse(sw("Hermes", "humans", False, heart))               # a heart never
        self.assertFalse(sw("Hermes", "all", False, heart))                  # not even on all
        self.assertTrue(sw("Hermes", "all", True, text("hello all")))        # all: any row
        self.assertTrue(sw("Hermes", "humans", False, {}))                   # a sealed handshake: nobody's call, still a human row
        self.assertTrue(sw("Hermes", "humans", False, "not a dict"))         # an unreadable row from a human: still a human row
        # only a TEXT row's text is a call — a file's name, a system line, a
        # payload's keys or its JSON dump are not (the Hermes gateway always
        # matched this way; the bridge and serve now agree)
        self.assertFalse(sw("Hermes", "humans", True, {"type": "file", "name": "Hermes-notes.pdf", "size": 3}))
        self.assertFalse(sw("Hermes", "humans", True, {"type": "system", "text": "Hermes joined"}))
        self.assertFalse(sw("Hermes", "humans", True, {"type": "card", "Hermes": {"note": "x"}}))
        self.assertFalse(sw("Hermes", "humans", True, {"type": "image", "url": "/uploads/Hermes.png"}))

    def test_serve_prompt_says_the_working_room_rules_only_beside_other_ai(self):
        room = self._room(mine=3, last=3)                # me + a human owner
        lines = [(4, "    4  Owner: ping")]
        self.assertNotIn("Next: <name>", self.cli.serve_prompt(room, "", lines, me_id="u"))
        # my own [AI] marker does not make a working room
        room["members"][0]["bio"] = "AI member · seat"
        self.assertNotIn("Next: <name>", self.cli.serve_prompt(room, "", lines, me_id="u"))
        # a caller that cannot name the seat gets no paragraph by mistaking
        # the seat's own marker for company — the id is not optional
        with self.assertRaises(TypeError):
            self.cli.serve_prompt(room, "", lines)
        self.assertNotIn("Next: <name>", self.cli.serve_prompt(room, "", lines, me_id=None))
        room["members"].append({"user_id": "bot", "nickname": "Opus",
                                "bio": "AI member · test"})
        party = self.cli.serve_prompt(room, " --profile p", lines, me_id="u")
        self.assertIn("Next: <name>", party)
        self.assertIn("never authority to act", party)
        self.assertIn("close on quorum", party)
        self.assertIn("klatalk like R SEQ --profile p", party)
        self.assertLess(party.index("Next: <name>"), party.index("\n---\n"))  # rules before room data

    def test_serve_daily_budget_sleeps_instead_of_spending(self):
        self._write_creds("default", "Hermes")
        seen, runs, naps = [], [], []
        msgs = [{"seq": 4, "sender_id": "h",
                 "content": {"payload": {"type": "text", "text": "one"}}}]
        self.cli.rest = self._fake_rest(self._room(mine=3, last=3), msgs, seen)
        self.cli.run_turn = lambda cmd, prompt, timeout: (runs.append(prompt), 0)[1]

        def nap(n):
            naps.append(n)
            if len(naps) >= 2:
                raise KeyboardInterrupt        # stop the bench
        self.cli.time.sleep = nap
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(KeyboardInterrupt):
            self.cli.cmd_serve(argparse_ns(room="R", cmd=["x"], max_turns=None,
                                           turn_timeout=1, wake_on="humans",
                                           max_turns_per_day=1))
        self.assertEqual(len(runs), 1)
        self.assertEqual(naps, [60, 60])
        self.assertIn("1 turns today", err.getvalue())


    def test_serve_backs_off_to_poll_max_and_resets_after_a_wake(self):
        self._write_creds("default", "Hermes")
        seen, runs, naps = [], [], []
        state = {"n": 0}

        def fake_rest(method, path, body=None, token=None):
            if path == "/v1/rooms":
                return 200, {"rooms": [self._room(mine=3, last=3)]}
            state["n"] += 1
            if state["n"] == 7:                  # quiet for six polls, then a message
                return 200, {"messages": [{"seq": 4, "sender_id": "h",
                             "content": {"payload": {"type": "text", "text": "hi"}}}]}
            return 200, {"messages": []}
        self.cli.rest = fake_rest
        self.cli.run_turn = lambda cmd, prompt, timeout: (runs.append(prompt), 0)[1]
        self.cli.time.sleep = naps.append
        with contextlib.redirect_stderr(io.StringIO()):
            self.cli.cmd_serve(argparse_ns(room="R", cmd=["x"], max_turns=1,
                                           turn_timeout=1, poll_max=60))
        self.assertEqual(naps, [2, 4, 8, 16, 32, 60])   # doubling, capped at the latency promise
        self.assertEqual(len(runs), 1)


class TestServeService(Base):
    """`serve --install` writes the service with this shell's PATH and cwd
    — the two things agents got wrong writing a plist by hand."""

    def _write_creds(self, profile, nickname):
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        self.cli.write_private(
            self.cli.cred_path(profile),
            lambda f: json.dump({"user_id": "u", "nickname": nickname,
                                 "access_token": "SECRET-A"}, f))

    def test_service_argv_is_absolute_and_keeps_the_turn_after_dashdash(self):
        argv = self.cli.service_argv("room-1234-5678", "hermes", 600,
                                     ["codex", "exec", "-c", "x=1", "-"])
        self.assertTrue(os.path.isabs(argv[0]) and os.path.isabs(argv[1]))
        i = argv.index("--")
        self.assertEqual(argv[2:i], ["serve", "room-1234-5678", "--profile",
                                     "hermes", "--turn-timeout", "600"])
        self.assertEqual(argv[i + 1:], ["codex", "exec", "-c", "x=1", "-"])

    def test_launchd_plist_carries_path_and_escapes(self):
        os.environ["PATH"] = "/opt/nvm/bin:/usr/bin"
        env = self.cli.service_env()
        self.assertEqual(env["PATH"], "/opt/nvm/bin:/usr/bin")
        self.assertEqual(env["KLATALK_HOME"], self.home)
        plist = self.cli.launchd_plist("com.klatalk.serve.x", ["a", "b & c"],
                                       env, "/w", "/l.log")
        self.assertIn("<string>b &amp; c</string>", plist)
        self.assertIn("<key>PATH</key><string>/opt/nvm/bin:/usr/bin</string>", plist)
        self.assertIn("<key>KeepAlive</key><true/>", plist)
        self.assertIn("<key>StandardErrorPath</key><string>/l.log</string>", plist)

    def test_systemd_unit_quotes_exec_and_env(self):
        unit = self.cli.systemd_unit("lbl", ["/usr/bin/python3", "/p/klatalk",
                                             "serve", "R", "--", "sh", "-c", "a b"],
                                     {"PATH": "/x:/y"}, "/w", "/l.log")
        self.assertIn("ExecStart=/usr/bin/python3 /p/klatalk serve R -- sh -c 'a b'", unit)
        self.assertIn("Environment=PATH=/x:/y", unit)
        self.assertIn("Restart=always", unit)
        self.assertIn("StandardError=append:/l.log", unit)   # the proof line lands here

    @unittest.skipIf(os.name != "posix", "launchd is macOS; the service path needs getuid")
    def test_install_writes_plist_and_bootstraps(self):
        # a fake HOME keeps LaunchAgents out of the real account
        os.environ["HOME"] = self.tmp
        self._write_creds("default", "Hermes")
        room = {"id": "room-1234-5678", "name": "X", "encryption_mode": "plain",
                "last_seq": 3, "my_last_read_seq": 3, "members": []}
        self.cli.rest = lambda m, p, body=None, token=None: (200, {"rooms": [room]})
        calls = []

        class R:
            returncode = 0
        self.cli.subprocess.run = lambda cmd, **kw: (calls.append(cmd), R())[1]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.cli.cmd_serve(argparse_ns(room="room-1234-5678", cmd=["claude", "-p"],
                                           max_turns=None, turn_timeout=600,
                                           install="launchd", uninstall=None))
        path = os.path.join(self.tmp, "Library/LaunchAgents",
                            "com.klatalk.serve.room-123.default.plist")
        self.assertTrue(os.path.exists(path))
        self.assertIn("<string>claude</string>", open(path).read())
        self.assertEqual(calls[-1][:2], ["launchctl", "bootstrap"])
        self.assertIn("test message", out.getvalue())       # the round trip is the finish line
        # uninstall removes it again
        with contextlib.redirect_stdout(io.StringIO()):
            self.cli.cmd_serve(argparse_ns(room="room-1234-5678", cmd=[],
                                           max_turns=None, turn_timeout=600,
                                           install=None, uninstall="launchd"))
        self.assertFalse(os.path.exists(path))
        self.assertEqual(calls[-1][:2], ["launchctl", "bootout"])
        # a second uninstall (wrong profile, wrong room) is refused, never
        # reported as "removed"
        with self.assertRaises(SystemExit) as cm:
            self.cli.cmd_serve(argparse_ns(room="room-1234-5678", cmd=[],
                                           max_turns=None, turn_timeout=600,
                                           install=None, uninstall="launchd"))
        self.assertIn("no launchd service", str(cm.exception))

    def test_windows_launcher_quotes_once_and_loops(self):
        argv = ["C:\\py\\python.exe", "C:\\k\\klatalk", "serve", "R",
                "--", "cmd", "/c", "echo a & b 100%"]
        cmd = self.cli.windows_launcher("com.klatalk.serve.R.default", argv,
                                        {"PATH": "C:\\nvm;C:\\Windows", "KLATALK_HOME": "C:\\h"},
                                        "C:\\work", "C:\\h\\serve.log")
        self.assertIn("title com.klatalk.serve.R.default\r\n", cmd)
        self.assertIn('set "PATH=C:\\nvm;C:\\Windows"\r\n', cmd)
        self.assertIn('cd /d "C:\\work"\r\n', cmd)
        # the turn's `&` and `%` survive: quoted once, % doubled for cmd.exe
        self.assertIn('"echo a & b 100%%"', cmd)
        self.assertIn('>> "C:\\h\\serve.log" 2>&1\r\n', cmd)
        self.assertIn(":loop\r\n", cmd)
        self.assertIn("timeout /t 30 /nobreak >nul\r\ngoto loop\r\n", cmd)

    def test_install_schtasks_registers_and_runs_the_launcher(self):
        self._write_creds("default", "Hermes")
        room = {"id": "room-1234-5678", "name": "X", "encryption_mode": "plain",
                "last_seq": 3, "my_last_read_seq": 3, "members": []}
        self.cli.rest = lambda m, p, body=None, token=None: (200, {"rooms": [room]})
        calls = []

        class R:
            returncode = 0
        self.cli.subprocess.run = lambda cmd, **kw: (calls.append(cmd), R())[1]
        with contextlib.redirect_stdout(io.StringIO()):
            self.cli.cmd_serve(argparse_ns(room="room-1234-5678", cmd=["cmd", "/c", "echo"],
                                           max_turns=None, turn_timeout=600,
                                           install="schtasks", uninstall=None))
        launcher = os.path.join(self.home, "serve-room-123-default.cmd")
        self.assertTrue(os.path.exists(launcher))
        create = [c for c in calls if c[:2] == ["schtasks", "/Create"]][0]
        self.assertEqual(create[create.index("/TR") + 1], f'"{launcher}"')
        self.assertIn("ONLOGON", create)
        self.assertEqual(calls[-1][:2], ["schtasks", "/Run"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.cli.cmd_serve(argparse_ns(room="room-1234-5678", cmd=[],
                                           max_turns=None, turn_timeout=600,
                                           install=None, uninstall="schtasks"))
        self.assertFalse(os.path.exists(launcher))
        self.assertEqual([c[:2] for c in calls[-3:]],
                         [["schtasks", "/End"], ["taskkill", "/F"], ["schtasks", "/Delete"]])

    def test_service_argv_carries_wake_and_budget(self):
        argv = self.cli.service_argv("room-1234-5678", "p", 600, ["claude", "-p"],
                                     ["--wake-on", "humans", "--max-turns-per-day", "40"])
        i = argv.index("--")
        self.assertEqual(argv[i - 4:i], ["--wake-on", "humans", "--max-turns-per-day", "40"])

    @unittest.skipIf(os.name != "posix", "expanduser ignores HOME on Windows; the schtasks CI job covers it")
    def test_serve_list_shows_every_installed_seat_with_its_remove_line(self):
        os.environ["HOME"] = self.tmp
        la = os.path.join(self.tmp, "Library/LaunchAgents"); os.makedirs(la)
        open(os.path.join(la, "com.klatalk.serve.abcd1234.codex.plist"), "w").write("x")
        os.makedirs(self.home, exist_ok=True)
        open(os.path.join(self.home, "serve-ef567890-default.cmd"), "w").write("x")
        open(os.path.join(self.home, "serve-abcd1234-codex.log"), "w").write(
            "noise\n[serve] turn 3: seq 9..9\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.cli.cmd_serve(argparse_ns(room=None, cmd=[], list_seats=True))
        text = out.getvalue()
        self.assertIn("launchd  room abcd1234...  profile codex", text)
        self.assertIn("service: com.klatalk.serve.abcd1234.codex", text)  # the README's restart handle
        self.assertIn("[serve] turn 3: seq 9..9", text)
        self.assertIn("--profile codex --uninstall launchd", text)
        self.assertIn("schtasks room ef567890...  profile default", text)
        self.assertIn("(no log)", text)


class TestAttachments(Base):
    """send --image / --file: the phone's payload shapes, dimensions read
    from the bytes, the upload endpoint's refusals said plainly."""

    def _write_creds(self, profile, nickname):
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        self.cli.write_private(
            self.cli.cred_path(profile),
            lambda f: json.dump({"user_id": "u", "nickname": nickname,
                                 "access_token": "SECRET-A"}, f))

    def _png(self, w, h):
        import struct, zlib
        ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
        chunk = b"IHDR" + ihdr
        return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + chunk
                + struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF))

    def test_image_size_reads_png_gif_jpeg_webp_headers(self):
        import struct
        self.assertEqual(self.cli.image_size(self._png(1179, 2556)), (1179, 2556))
        self.assertEqual(self.cli.image_size(b"GIF89a" + struct.pack("<HH", 320, 200)), (320, 200))
        jpeg = (b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 10
                + b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", 480, 640) + b"\x03" + b"\x00" * 9)
        self.assertEqual(self.cli.image_size(jpeg), (640, 480))
        webpx = (b"RIFF" + b"\x00" * 4 + b"WEBP" + b"VP8X" + struct.pack("<I", 10) + b"\x00" * 4
                 + (299).to_bytes(3, "little") + (199).to_bytes(3, "little"))
        self.assertEqual(self.cli.image_size(webpx), (300, 200))
        self.assertEqual(self.cli.image_size(b"not an image"), (0, 0))

    def test_attachment_payload_validates_like_the_server(self):
        png = os.path.join(self.tmp, "a.png"); open(png, "wb").write(self._png(4, 3))
        ctype, ext, data, payload = self.cli.attachment_payload(png, "image")
        self.assertEqual((ctype, ext, payload), ("image/png", ".png", {"type": "image", "url": None, "w": 4, "h": 3}))
        doc = os.path.join(self.tmp, "r.pdf"); open(doc, "wb").write(b"%PDF-1.4 x")
        ctype, ext, data, payload = self.cli.attachment_payload(doc, "file")
        self.assertEqual(payload, {"type": "file", "url": None, "name": "r.pdf", "size": 10,
                                   "mime": "application/pdf", "text": "(파일) r.pdf"})
        bad = os.path.join(self.tmp, "x.exe"); open(bad, "wb").write(b"MZ")
        with self.assertRaises(self.cli.KlatalkError) as cm:
            self.cli.attachment_payload(bad, "file")
        self.assertIn("must be one of", str(cm.exception))
        fake = os.path.join(self.tmp, "f.png"); open(fake, "wb").write(b"PNG?")
        with self.assertRaises(self.cli.KlatalkError) as cm:
            self.cli.attachment_payload(fake, "image")
        self.assertIn("dimensions", str(cm.exception))

    def test_send_image_uploads_then_sends_the_phone_payload(self):
        self._write_creds("default", "Hermes")
        png = os.path.join(self.tmp, "a.png"); open(png, "wb").write(self._png(7, 5))
        room = {"id": "R", "name": "X", "encryption_mode": "plain", "members": []}
        self.cli.rest = lambda m, p, body=None, token=None: (200, {"rooms": [room]})
        ups, sends = [], []
        self.cli.upload_to_room = lambda creds, rid, ext, ctype, data: (
            ups.append((rid, ext, ctype, len(data))), "/uploads/R/x.png")[1]

        async def fake_send(creds, room_id, text, reply_to=None, payload=None, **kw):
            sends.append((room_id, text, reply_to, payload)); return 9
        self.cli.do_send = fake_send
        self.cli.cmd_send(argparse_ns(room="R", text=None, text_stdin=False, reply=3,
                                      image=png, file=None))
        self.assertEqual(ups, [("R", ".png", "image/png", len(self._png(7, 5)))])
        self.assertEqual(sends, [("R", None, 3, {"type": "image", "url": "/uploads/R/x.png", "w": 7, "h": 5})])

    def test_send_image_in_a_sealed_room_goes_through_sealed_send_with_a_note(self):
        self._write_creds("default", "Hermes")
        png = os.path.join(self.tmp, "a.png"); open(png, "wb").write(self._png(2, 2))
        room = {"id": "R", "name": "X", "encryption_mode": "mls10", "members": []}
        self.cli.rest = lambda m, p, body=None, token=None: (200, {"rooms": [room]})
        self.cli.upload_to_room = lambda *a: "/uploads/R/y.png"
        self.cli.sealed_preflight = lambda *a: None
        sealed = []
        async def fake_sealed(creds, profile, rid, payload, reply_to=None, read_through=None):
            sealed.append(payload); return 9
        self.cli.sealed_send_async = fake_sealed
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.cli.cmd_send(argparse_ns(room="R", text=None, text_stdin=False, reply=None,
                                          image=png, file=None))
        self.assertEqual(sealed, [{"type": "image", "url": "/uploads/R/y.png", "w": 2, "h": 2}])
        self.assertIn("stored on the server as uploaded", err.getvalue())

    def test_send_refuses_text_with_an_attachment(self):
        self._write_creds("default", "Hermes")
        png = os.path.join(self.tmp, "a.png"); open(png, "wb").write(self._png(2, 2))
        room = {"id": "R", "name": "X", "encryption_mode": "plain", "members": []}
        self.cli.rest = lambda m, p, body=None, token=None: (200, {"rooms": [room]})
        with self.assertRaises(SystemExit) as cm:
            self.cli.cmd_send(argparse_ns(room="R", text="hi", text_stdin=False, reply=None,
                                          image=png, file=None))
        self.assertIn("attachment alone", str(cm.exception))

    def test_upload_errors_are_named(self):
        self._write_creds("default", "Hermes")
        creds = {"access_token": "T"}

        def opener(code, body):
            class O:
                def open(self, req, timeout=0):
                    raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(body))
            return O()
        self.cli.OPENER = opener(429, b'{"error": "upload_quota"}')
        with self.assertRaises(self.cli.KlatalkError) as cm:
            self.cli.upload_to_room(creds, "R", "a.png", "image/png", b"x")
        self.assertIn("quota", str(cm.exception))
        self.cli.OPENER = opener(415, b'{"error": "unsupported_type"}')
        with self.assertRaises(self.cli.KlatalkError) as cm:
            self.cli.upload_to_room(creds, "R", "a.png", "image/png", b"x")
        self.assertIn("file type", str(cm.exception))

    def _opener(self, code, body):
        class O:
            def open(self_, req, timeout=0):
                if code == 200:
                    class R(io.BytesIO):
                        status = 200
                    return R(body)
                raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(body))
        return O()

    def test_truncated_headers_are_unknown_not_a_traceback(self):
        import struct
        for name, blob in [
                ("png", b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR\x00\x00\x00\x04"),
                ("gif", b"GIF89a\x01\x02"),
                ("vp8x", b"RIFF" + b"\x00" * 4 + b"WEBP" + b"VP8X" + b"\x00" * 4),
                ("vp8l", b"RIFF" + b"\x00" * 4 + b"WEBP" + b"VP8L" + b"\x00" * 4 + b"\x2f\x00"),
                ("vp8", b"RIFF" + b"\x00" * 4 + b"WEBP" + b"VP8 " + b"\x00" * 12),
                ("jpeg", b"\xff\xd8\xff\xc0\x00")]:
            with self.subTest(name):
                self.assertEqual(self.cli.image_size(blob), (0, 0))

    def test_webp_lossless_and_lossy_and_jpeg_fill_and_exif(self):
        import struct
        bits = (299) | (199 << 14)
        vp8l = (b"RIFF" + b"\x00" * 4 + b"WEBP" + b"VP8L" + struct.pack("<I", 5)
                + b"\x2f" + bits.to_bytes(4, "little"))
        self.assertEqual(self.cli.image_size(vp8l), (300, 200))
        vp8 = (b"RIFF" + b"\x00" * 4 + b"WEBP" + b"VP8 " + struct.pack("<I", 20)
               + b"\x00" * 3 + b"\x9d\x01\x2a" + struct.pack("<HH", 640, 480))
        self.assertEqual(self.cli.image_size(vp8), (640, 480))
        sof = (b"\xff\xc0" + struct.pack(">H", 17) + b"\x08"
               + struct.pack(">HH", 480, 640) + b"\x03" + b"\x00" * 9)
        app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 10
        self.assertEqual(self.cli.image_size(b"\xff\xd8" + app0 + b"\xff\xff\xff" + sof), (640, 480))
        # EXIF Orientation 6 → the phone shows it rotated: w/h swap
        tiff = (b"MM\x00\x2a" + struct.pack(">I", 8) + struct.pack(">H", 1)
                + struct.pack(">HHI", 0x0112, 3, 1) + struct.pack(">H", 6) + b"\x00\x00"
                + struct.pack(">I", 0))
        exif = b"Exif\x00\x00" + tiff
        app1 = b"\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif
        self.assertEqual(self.cli.image_size(b"\xff\xd8" + app1 + sof), (480, 640))
        # HEIC: ftyp + an ispe box
        heic = (struct.pack(">I", 24) + b"ftypheic" + b"\x00" * 12
                + struct.pack(">I", 20) + b"ispe" + b"\x00" * 4 + struct.pack(">II", 4032, 3024))
        self.assertEqual(self.cli.image_size(heic), (4032, 3024))

    def test_size_ceiling_and_special_files(self):
        big = os.path.join(self.tmp, "big.pdf")
        with open(big, "wb") as f:
            f.write(b"\x00" * self.cli.MAX_ATTACHMENT)
        self.assertEqual(self.cli.attachment_payload(big, "file")[3]["size"], self.cli.MAX_ATTACHMENT)
        with open(big, "ab") as f:
            f.write(b"\x00")
        with self.assertRaises(self.cli.KlatalkError):
            self.cli.attachment_payload(big, "file")
        link = os.path.join(self.tmp, "l.pdf"); os.symlink(big, link)
        with self.assertRaisesRegex(self.cli.KlatalkError, "regular file"):
            self.cli.attachment_payload(link, "file")
        os.makedirs(self.home, exist_ok=True)
        inside = os.path.join(self.home, "creds.txt"); open(inside, "wb").write(b"x")
        with self.assertRaisesRegex(self.cli.KlatalkError, "profile directory"):
            self.cli.attachment_payload(inside, "file")
        self.assertEqual(self.cli.FILE_TYPES[".csv"], "text/csv")
        self.assertEqual(self.cli.IMAGE_TYPES[".heic"], "image/heic")

    def test_upload_goes_to_the_room_endpoint_and_refuses_foreign_urls(self):
        seen = {}

        class O:
            def open(self_, req, timeout=0):
                seen["url"] = req.full_url; seen["auth"] = req.get_header("Authorization")
                seen["body"] = req.data
                class R(io.BytesIO):
                    status = 200
                return R(b'{"url": "/uploads/R/x.png"}')
        self.cli.OPENER = O()
        url = self.cli.upload_to_room({"access_token": "T"}, "R", ".png", "image/png", b"bytes")
        self.assertEqual(url, "/uploads/R/x.png")
        self.assertTrue(seen["url"].endswith("/v1/rooms/R/uploads"))
        self.assertEqual(seen["auth"], "Bearer T")
        self.assertIn(b'name="file"; filename="upload.png"', seen["body"])   # neutral name
        self.assertNotIn(b"T", seen["body"][:0])      # (the token rides the header, never the body — checked below)
        self.assertNotIn(b"Bearer", seen["body"])
        for bad in (b'{"url": "/uploads/../v1/me"}', b'{"url": "https://evil/uploads/R/x"}',
                    b'{"url": "/uploads/OTHER/x.png"}', b'{"url": "/uploads/R/x.png?next=1"}'):
            self.cli.OPENER = self._opener(200, bad)
            with self.assertRaises(self.cli.KlatalkError):
                self.cli.upload_to_room({"access_token": "T"}, "R", ".png", "image/png", b"x")
        self.cli.OPENER = self._opener(200, b"<html>oops</html>")
        with self.assertRaisesRegex(self.cli.KlatalkError, "non-JSON"):
            self.cli.upload_to_room({"access_token": "T"}, "R", ".png", "image/png", b"x")

    def test_blocked_sealed_room_uploads_nothing_and_unknown_room_is_named(self):
        self._write_creds("default", "Hermes")
        png = os.path.join(self.tmp, "a.png"); open(png, "wb").write(self._png(2, 2))
        room = {"id": "R", "name": "X", "encryption_mode": "mls10", "members": []}
        self.cli.rest = lambda m, p, body=None, token=None: (200, {"rooms": [room]})
        ups = []
        self.cli.upload_to_room = lambda *a: ups.append(a) or "/uploads/R/x.png"
        self.cli.mark_desync("default", "R", "test")
        with self.assertRaisesRegex(self.cli.KlatalkError, "nothing was uploaded"), contextlib.redirect_stderr(io.StringIO()):
            self.cli.cmd_send(argparse_ns(room="R", text=None, text_stdin=False, reply=None,
                                          image=png, file=None))
        self.assertEqual(ups, [])
        self.cli.rest = lambda m, p, body=None, token=None: (200, {"rooms": []})
        with self.assertRaisesRegex(SystemExit, "no room"):
            self.cli.cmd_send(argparse_ns(room="NOPE", text=None, text_stdin=False, reply=None,
                                          image=png, file=None))
        with self.assertRaisesRegex(SystemExit, "one attachment"):
            self.cli.cmd_send(argparse_ns(room="R", text=None, text_stdin=False, reply=None,
                                          image=png, file=png))

    def test_multipart_filename_cannot_inject_headers(self):
        _, body = self.cli.multipart_body("file", 'a"\r\nContent-Type: x\r\n\r\nP.txt', "text/plain", b"real")
        self.assertEqual(body.count(b"\r\nContent-Type:"), 1)     # one header line, ours
        self.assertEqual(body.count(b"\r\n\r\n"), 1)              # exactly one header/body break

    def test_post_fix_round_heic_meta_scan_reason_allowlist_path_controls(self):
        import struct
        def box(t, payload): return struct.pack(">I", 8 + len(payload)) + t + payload
        ftyp = box(b"ftyp", b"heic" + b"\x00" * 8)
        decoy = box(b"free", b"ispe" + bytes(4) + struct.pack(">II", 17, 19))
        tile = box(b"ispe", bytes(4) + struct.pack(">II", 512, 512))
        full = box(b"ispe", bytes(4) + struct.pack(">II", 3840, 2160))
        meta = box(b"meta", bytes(4) + box(b"iprp", box(b"ipco", tile + full)))
        self.assertEqual(self.cli.image_size(ftyp + decoy + meta), (3840, 2160))
        # rejection reasons: a plain token travels, anything else collapses
        async def run(reply):
            class WS:
                async def close(self): pass
            self.cli.ws_connect = lambda tok: _coro(WS())
            self.cli.ws_join = lambda ws, rid: _coro("room:R")
            self.cli.ws_push = lambda ws, topic, ev, body, ref: _coro(reply)
            try:
                await self.cli.do_send({"access_token": "T"}, "R", "hi")
            except self.cli.SendRejected as e:
                return str(e)
        for reply, want in [({"status": "error", "response": {"reason": "invalid_message"}}, "invalid_message"),
                            ({"status": "error", "response": {"reason": {"access_token": "S"}}}, "rejected"),
                            ({"status": "error", "response": {"reason": "\x1b[2Jx"}}, "rejected")]:
            self.assertEqual(asyncio.run(run(reply)), want)
        # paths: tab/newline raw or encoded never pass
        for bad in ("/uploads/a%0Ab", "/uploads/a%09b", "/uploads/a\nb", "/uploads/a\tb"):
            self.assertIsNone(self.cli.uploads_path(bad), bad)
        self.assertEqual(self.cli.uploads_path("/uploads/R/x.png"), "/uploads/R/x.png")
        # EXIF orientation beyond the 64th entry still counts
        entries = b"".join(struct.pack(">HHIHH", 0x0200 + k, 3, 1, 1, 0) for k in range(70))
        entries += struct.pack(">HHIHH", 0x0112, 3, 1, 6, 0)
        tiff = b"MM\x00\x2a" + struct.pack(">I", 8) + struct.pack(">H", 71) + entries + struct.pack(">I", 0)
        exif = b"Exif\x00\x00" + tiff
        app1 = b"\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif
        sof = (b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", 480, 640) + b"\x03" + b"\x00" * 9)
        self.assertEqual(self.cli.image_size(b"\xff\xd8" + app1 + sof), (480, 640))

    def test_upload_413_is_named(self):
        self.cli.OPENER = self._opener(413, b"")
        with self.assertRaisesRegex(self.cli.KlatalkError, "size ceiling"):
            self.cli.upload_to_room({"access_token": "T"}, "R", ".png", "image/png", b"x")


class TestCoreLibrary(Base):
    """core-v1.4 — the CLI as a library a gateway daemon embeds. The
    contract: helpers raise KlatalkError (never SystemExit), importing has
    no side effects, sealed state is locked across processes, reception
    and sending are callable from an event loop."""

    # -- exception mode ------------------------------------------------

    def test_sys_exit_lives_only_in_the_cli_layer(self):
        # A SystemExit escaping an asyncio task kills the whole daemon —
        # every other platform included. Only the command layer may exit.
        import ast
        allowed = {"main", "profile_name", "sealed_join", "serve_install",
                   "serve_uninstall"}
        tree = ast.parse(open(BIN, encoding="utf-8").read())
        offenders = []
        top = list(tree.body) + [n for c in tree.body if isinstance(c, ast.ClassDef)
                                 for n in c.body]        # methods run on import too
        for node in top:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("cmd_") or node.name in allowed:
                continue
            for n in ast.walk(node):
                is_exit = (isinstance(n, ast.Call)
                           and isinstance(n.func, ast.Attribute)
                           and n.func.attr == "exit"
                           and isinstance(n.func.value, ast.Name)
                           and n.func.value.id == "sys")
                is_raise = (isinstance(n, ast.Raise) and n.exc is not None
                            and "SystemExit" in ast.dump(n.exc))
                if is_exit or is_raise:
                    offenders.append(f"{node.name}:{n.lineno}")
        self.assertEqual(offenders, [])

    def test_http_failures_map_to_error_classes(self):
        c = self.cli
        cases = [(401, c.KlatalkAuth), (403, c.KlatalkMembership),
                 (404, c.KlatalkMembership), (429, c.KlatalkQuota),
                 (500, c.KlatalkTransient), (503, c.KlatalkTransient),
                 (422, c.KlatalkError)]
        for status, cls in cases:
            with self.subTest(status=status), self.assertRaises(cls):
                c.die_on(status, {"error": "x"})
        # every one of them is a KlatalkError — one except clause suffices
        for status, _ in cases:
            with self.assertRaises(c.KlatalkError):
                c.die_on(status, {"error": "x"})

    def test_unreachable_server_is_transient_not_exit(self):
        class O:
            def open(self, req, timeout=0):
                raise urllib.error.URLError("connection refused")
        self.cli.OPENER = O()
        with self.assertRaises(self.cli.KlatalkTransient):
            self.cli.rest("GET", "/v1/rooms")

    def test_socket_rpc_failures_raise_not_exit(self):
        class Silent:
            async def send(self, raw):
                pass

            async def recv(self):
                await asyncio.sleep(3600)

        class Refusing:
            async def send(self, raw):
                self.last = json.loads(raw)

            async def recv(self):
                f = self.last
                return json.dumps([f[0], f[1], f[2], "phx_reply",
                                   {"status": "error",
                                    "response": {"reason": "not_a_member"}}])
        self.cli.RPC_TIMEOUT = 0.05
        with self.assertRaises(self.cli.KlatalkTransient):
            asyncio.run(self.cli.ws_push(Silent(), "room:R", "read:mark",
                                         {"seq": 1}, "2"))
        with self.assertRaises(self.cli.KlatalkMembership) as cm:
            asyncio.run(self.cli.ws_join(Refusing(), "R"))
        self.assertIn("not_a_member", str(cm.exception))

    def test_missing_credentials_are_auth_errors(self):
        with self.assertRaises(self.cli.KlatalkAuth):
            self.cli.load_creds("nobody")

    # -- import without side effects ----------------------------------

    def test_import_is_silent_and_configure_rebinds(self):
        out, err = io.StringIO(), io.StringIO()
        os.environ["KLATALK_API"] = "http://plain.example"   # would warn in main()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                mod = load_cli(self.home)
        finally:
            os.environ.pop("KLATALK_API", None)
        self.assertEqual(out.getvalue() + err.getvalue(), "")
        self.assertEqual(mod.API, "http://plain.example")
        cfg = mod.configure(mod.ClientConfig(api="https://other.example/",
                                             home=self.tmp, mls_bin="/x/mls"))
        self.assertEqual(mod.API, "https://other.example")
        self.assertEqual(mod.API_HOST, "other.example")
        self.assertTrue(mod.WS.startswith("wss://other.example/"))
        self.assertEqual(mod.HOME, self.tmp)
        self.assertEqual(mod.MLS_BIN, "/x/mls")
        self.assertEqual(cfg.api, "https://other.example")
        # warn() is the one outlet the core speaks through — rebindable
        got = []
        mod.warn = got.append
        mod.warn("x")
        self.assertEqual(got, ["x"])

    # -- locks ----------------------------------------------------------

    @unittest.skipIf(os.name != "posix", "fcntl locks")
    def test_room_lock_is_reentrant_in_a_thread_and_exclusive_across_processes(self):
        import subprocess as sp
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        with self.cli.room_lock("default", "R"):
            with self.cli.room_lock("default", "R"):    # nested: no deadlock
                pass
        path = os.path.join(self.cli.mls_dir("default"), "lock-room-R")
        holder = sp.Popen([sys.executable, "-c",
                           "import fcntl,sys,time; f=open(sys.argv[1],'w');"
                           "fcntl.flock(f, fcntl.LOCK_EX); print('held', flush=True);"
                           "time.sleep(3)", path], stdout=sp.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            self.cli.LOCK_TIMEOUT = 0.3
            with self.assertRaises(self.cli.KlatalkBusy) as cm:
                with self.cli.room_lock("default", "R"):
                    pass
            self.assertIn("lock", str(cm.exception))
        finally:
            holder.kill()
            holder.wait()
        # released: the lock is ours again
        with self.cli.room_lock("default", "R"):
            pass

    # -- reception core -------------------------------------------------

    def _fake_ws(self, frames, sent):
        class FakeWS:
            def __init__(self):
                self.frames = list(frames)

            async def send(self, raw):
                sent.append(json.loads(raw))

            async def recv(self):
                f = sent[-1]
                return json.dumps([f[0], f[1], f[2], "phx_reply",
                                   {"status": "ok", "response": {}}])

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self.frames:
                    raise KeyboardInterrupt           # one lap, then out
                return json.dumps(self.frames.pop(0))

            async def close(self):
                pass

        async def connect(token):
            return FakeWS()
        return connect

    def test_listen_core_backfills_once_and_starts_at_the_read_mark(self):
        sent, got = [], []
        pages = []

        def fake_rest(method, path, body=None, token=None):
            if path == "/v1/rooms":
                return 200, {"rooms": [{"id": "R", "encryption_mode": "plain",
                                        "last_seq": 9, "my_last_read_seq": 4,
                                        "members": []}]}
            pages.append(path)
            return 200, {"messages": [{"seq": 5, "content": {"payload": {"type": "text", "text": "a"}}},
                                      {"seq": 6, "content": {"payload": {"type": "text", "text": "b"}}}]}
        self.cli.rest = fake_rest
        self.cli.ws_connect = self._fake_ws([
            [None, None, "room:R", "message:new",
             {"seq": 6, "content": {"payload": {"type": "text", "text": "b"}}}],  # raced the backfill
            [None, None, "room:R", "message:new",
             {"seq": 7, "content": {"payload": {"type": "text", "text": "c"}}}],
            [None, None, "room:R", "member:joined", {"user_id": "x"}],
        ], sent)
        with self.assertRaises(KeyboardInterrupt):
            asyncio.run(self.cli.listen_core({"access_token": "T"}, "default",
                                             "R", got.append))
        self.assertIn("after_seq=4", pages[0])              # cold start = read mark
        kinds = [(e["kind"], e.get("seq")) for e in got]
        self.assertEqual(kinds, [("joined", None), ("message", 5), ("message", 6),
                                 ("message", 7), ("frame", None)])
        self.assertEqual(got[1]["payload"], {"type": "text", "text": "a"})
        self.assertEqual(got[4]["event"], "member:joined")
        # reception never signs
        self.assertEqual([f for f in sent if f[3] == "read:mark"], [])

    def test_listen_core_accepts_a_coroutine_consumer(self):
        sent, got = [], []
        self.cli.rest = lambda m, p, body=None, token=None: (
            (200, {"rooms": [{"id": "R", "encryption_mode": "plain", "last_seq": 0,
                              "my_last_read_seq": 0, "members": []}]})
            if p == "/v1/rooms" else (200, {"messages": []}))
        self.cli.ws_connect = self._fake_ws(
            [[None, None, "room:R", "message:new", {"seq": 1, "content": {}}]], sent)

        async def on_event(ev):
            await asyncio.sleep(0)
            got.append(ev["kind"])
        with self.assertRaises(KeyboardInterrupt):
            asyncio.run(self.cli.listen_core({"access_token": "T"}, "default",
                                             "R", on_event))
        self.assertEqual(got, ["joined", "message"])

    def test_listen_core_stops_on_membership_loss_but_retries_transients(self):
        calls = {"n": 0}

        def flaky_rest(method, path, body=None, token=None):
            if path == "/v1/rooms":
                return 200, {"rooms": [{"id": "R", "encryption_mode": "plain",
                                        "last_seq": 0, "my_last_read_seq": 0,
                                        "members": []}]}
            calls["n"] += 1
            if calls["n"] == 1:
                raise self.cli.KlatalkTransient("503")
            raise self.cli.KlatalkMembership("HTTP 403: forbidden")
        self.cli.rest = flaky_rest
        self.cli.ws_connect = self._fake_ws([], [])
        got = []

        async def run():
            # the first lap backs off (1s) — shrink it by patching sleep
            self.cli.backoff_sleep = lambda d: asyncio.sleep(0)
            await self.cli.listen_core({"access_token": "T"}, "default",
                                       "R", got.append)
        with self.assertRaises(self.cli.KlatalkMembership):
            asyncio.run(run())
        self.assertEqual([e["kind"] for e in got], ["joined", "reconnect", "joined"])

    def test_events_after_pages_to_the_end(self):
        seen = []

        def fake_rest(method, path, body=None, token=None):
            seen.append(path)
            after = int(re.search(r"after_seq=(\d+)", path).group(1))
            if after == 0:
                return 200, {"messages": [{"seq": i, "content": {}} for i in range(1, 201)]}
            return 200, {"messages": [{"seq": 201, "content": {}}]}
        self.cli.rest = fake_rest
        evs = self.cli.events_after({"access_token": "T"}, "default", "R", 0, False)
        self.assertEqual(len(evs), 201)
        self.assertEqual(len(seen), 2)
        self.assertIn("after_seq=200", seen[1])

    # -- sending --------------------------------------------------------

    def test_read_through_is_the_only_thing_that_moves_the_read_mark(self):
        sent = []

        class FakeWS:
            async def send(self, raw):
                sent.append(json.loads(raw))

            async def recv(self):
                f = sent[-1]
                resp = {"seq": 11} if f[3] == "message:send" else {}
                return json.dumps([f[0], f[1], f[2], "phx_reply",
                                   {"status": "ok", "response": resp}])

            async def close(self):
                pass

        async def connect(token):
            return FakeWS()
        self.cli.ws_connect = connect
        creds = {"access_token": "T", "nickname": "n"}

        def marks():
            return [f[4]["seq"] for f in sent if f[3] == "read:mark"]
        self.assertEqual(asyncio.run(self.cli.do_send(creds, "R", "hi")), 11)
        self.assertEqual(marks(), [])                       # a daemon signs later
        asyncio.run(self.cli.do_send(creds, "R", "hi", read_through=7))
        self.assertEqual(marks(), [7])
        asyncio.run(self.cli.do_send(creds, "R", "hi",
                                     read_through=self.cli.READ_THROUGH_SENT))
        self.assertEqual(marks(), [7, 11])                  # the CLI convention

    def test_cmd_send_keeps_the_send_marks_read_convention(self):
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        self.cli.write_private(self.cli.cred_path("default"),
                               lambda f: json.dump({"user_id": "u", "nickname": "n",
                                                    "access_token": "T"}, f))
        room = {"id": "R", "name": "X", "encryption_mode": "plain", "members": []}
        self.cli.rest = lambda m, p, body=None, token=None: (200, {"rooms": [room]})
        calls = []

        async def fake_send(creds, room_id, text, **kw):
            calls.append(kw.get("read_through")); return 3
        self.cli.do_send = fake_send
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.cli.cmd_send(argparse_ns(room="R", text="hi", text_stdin=False,
                                          reply=None, image=None, file=None))
        self.assertEqual(calls, [self.cli.READ_THROUGH_SENT])
        self.assertIn("sent seq 3", out.getvalue())

    def _sealed_send(self, *a, **k):
        return asyncio.run(self.cli.sealed_send_async(*a, **k))

    def _age_outbox(self, seconds=600):
        # a journal from a crashed process, not one still on the wire
        p = self.cli.outbox_path("default", "R")
        t = time.time() - seconds
        os.utime(p, (t, t))

    def _sealed_stubs(self, sends):
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        self.cli._sealed_pump = lambda creds, profile, room_id: []
        self.cli.verify_roster = lambda creds, profile, room_id, quick=False: True
        self.cli.mls_op = lambda creds, profile, op, payload=None: (
            {"epoch": 4} if op == "group-epoch" else {"ct_b64": "Q1Q="})
        counter = {"n": 20}

        async def fake_send(creds, room_id, text, reply_to=None, payload=None,
                            content=None, client_msg_id=None, read_through=None):
            sends.append((client_msg_id, content, reply_to, read_through))
            if content.get("payload", {}).get("ct") == "REJECT":
                raise self.cli.SendRejected("invalid_message")
            counter["n"] += 1
            return counter["n"]
        self.cli.do_send = fake_send

    def test_sealed_send_returns_seq_and_journals_its_own_plaintext(self):
        sends = []
        self._sealed_stubs(sends)
        creds = {"user_id": "me", "device_id": "d1", "access_token": "T"}
        seq = self._sealed_send(creds, "default", "R", {"type": "text", "text": "hi"},
                                   reply_to=2, read_through=self.cli.READ_THROUGH_SENT)
        self.assertEqual(seq, 21)
        self.assertEqual(sends[0][1], {"v": 2, "alg": "mls10", "payload": {"ct": "Q1Q="}})
        self.assertEqual(sends[0][2:], (2, self.cli.READ_THROUGH_SENT))
        recs = self.cli.ledger_read("default", "R")
        self.assertEqual([(r["seq"], r["own"], r["payload"]["text"]) for r in recs],
                         [(21, True, "hi")])
        self.assertFalse(os.path.exists(self.cli.outbox_path("default", "R")))

    def test_sealed_send_settles_a_stale_outbox_then_sends_the_new_one(self):
        sends = []
        self._sealed_stubs(sends)
        creds = {"user_id": "me", "device_id": "d1", "access_token": "T"}
        self.cli.replace_private(self.cli.outbox_path("default", "R"), lambda f: f.write(
            json.dumps({"client_msg_id": "old-id", "epoch": 3, "reply_to": None,
                        "content": {"v": 2, "alg": "mls10", "payload": {"ct": "T0xE"}},
                        "payload": {"type": "text", "text": "old"}})))
        self._age_outbox()
        with contextlib.redirect_stderr(io.StringIO()):
            seq = self._sealed_send(creds, "default", "R",
                                       {"type": "text", "text": "new"})
        # old bytes first (same id, same ciphertext), then the new utterance —
        # in ONE call; nobody is told to "rerun"
        self.assertEqual([s[0] for s in sends][0], "old-id")
        self.assertEqual(len(sends), 2)
        self.assertEqual(seq, 22)
        texts = [r["payload"]["text"] for r in self.cli.ledger_read("default", "R")]
        self.assertEqual(texts, ["old", "new"])
        self.assertFalse(os.path.exists(self.cli.outbox_path("default", "R")))

    def test_sealed_send_clears_a_rejected_stale_journal_and_still_sends(self):
        sends = []
        self._sealed_stubs(sends)
        creds = {"user_id": "me", "device_id": "d1", "access_token": "T"}
        self.cli.replace_private(self.cli.outbox_path("default", "R"), lambda f: f.write(
            json.dumps({"client_msg_id": "old-id", "epoch": 3, "reply_to": None,
                        "content": {"v": 2, "alg": "mls10", "payload": {"ct": "REJECT"}},
                        "payload": {"type": "text", "text": "old"}})))
        self._age_outbox()
        with contextlib.redirect_stderr(io.StringIO()):
            seq = self._sealed_send(creds, "default", "R",
                                       {"type": "text", "text": "new"})
        self.assertEqual(seq, 21)
        texts = [r["payload"]["text"] for r in self.cli.ledger_read("default", "R")]
        self.assertEqual(texts, ["new"])

    def test_sealed_send_refuses_a_desynced_room_with_a_typed_error(self):
        sends = []
        self._sealed_stubs(sends)
        self.cli.mark_desync("default", "R", "test")
        with self.assertRaises(self.cli.KlatalkDesync):
            self._sealed_send({"user_id": "me", "device_id": "d1", "access_token": "T"},
                                 "default", "R", {"type": "text", "text": "x"})
        self.assertEqual(sends, [])

    def test_send_message_routes_by_room_kind(self):
        calls = []

        async def plain(creds, room_id, text, **kw):
            calls.append(("plain", room_id, kw.get("payload"))); return 1

        async def sealed(creds, profile, room_id, payload, reply_to=None, read_through=None):
            calls.append(("sealed", room_id, payload)); return 2
        self.cli.do_send, self.cli.sealed_send_async = plain, sealed
        creds = {"access_token": "T"}
        asyncio.run(self.cli.send_message(creds, "p", {"id": "A", "encryption_mode": "plain"}, text="hi"))
        asyncio.run(self.cli.send_message(creds, "p", {"id": "B", "encryption_mode": "mls10"}, text="hi"))
        self.assertEqual(calls, [("plain", "A", {"type": "text", "text": "hi"}),
                                 ("sealed", "B", {"type": "text", "text": "hi"})])
        self.cli.rest = lambda m, p, body=None, token=None: (200, {"rooms": []})
        with self.assertRaises(self.cli.KlatalkMembership):
            asyncio.run(self.cli.send_message(creds, "p", "NOPE", text="hi"))

    # -- attachments in --------------------------------------------------

    def test_fetch_upload_caps_bytes_while_streaming(self):
        reads = []

        class O:
            def open(self_, req, timeout=0):
                class R(io.BytesIO):
                    status = 200

                    def read(self, n=-1):
                        reads.append(n)
                        return super().read(n)
                return R(b"x" * 1000)
        self.cli.OPENER = O()
        creds = {"access_token": "T"}
        self.assertEqual(self.cli.fetch_upload(creds, "/uploads/R/a.png", 1000), b"x" * 1000)
        with self.assertRaises(self.cli.KlatalkQuota):
            self.cli.fetch_upload(creds, "/uploads/R/a.png", 999)
        self.assertTrue(all(n <= 1 << 16 for n in reads))     # never the whole body at once
        with self.assertRaises(self.cli.KlatalkUsage):
            self.cli.fetch_upload(creds, "https://evil/uploads/R/a.png", 10)
        with self.assertRaises(self.cli.KlatalkUsage):
            self.cli.fetch_upload(creds, "/uploads/R/a.png", 0)



class TestCoreFixRound(TestCoreLibrary):
    """core-v1.4 133 round 1 — each test pins one reviewer finding."""

    def _room_rest(self, messages_by_after, room=None, fail_first=None):
        calls = {"rooms": 0}
        room = room or {"id": "R", "encryption_mode": "plain", "last_seq": 4,
                        "my_last_read_seq": 4, "members": []}

        def fake_rest(method, path, body=None, token=None):
            if path == "/v1/rooms":
                calls["rooms"] += 1
                if fail_first and calls["rooms"] <= fail_first:
                    raise self.cli.KlatalkTransient("503")
                return 200, {"rooms": [room]}
            after = int(re.search(r"after_seq=(\d+)", path).group(1))
            page = messages_by_after(after)
            if isinstance(page, Exception):
                raise page
            return 200, {"messages": page}
        self.cli.rest = fake_rest
        return calls

    def _laps_ws(self, laps, sent):
        """A ws factory whose n-th connection yields laps[n] frames, then
        ends the loop with KeyboardInterrupt once the laps run out."""
        state = {"n": 0}
        outer = self

        class FakeWS:
            def __init__(self, frames):
                self.frames = list(frames)

            async def send(self, raw):
                sent.append(json.loads(raw))

            async def recv(self):
                f = sent[-1]
                return json.dumps([f[0], f[1], f[2], "phx_reply",
                                   {"status": "ok", "response": {}}])

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self.frames:
                    raise KeyboardInterrupt
                item = self.frames.pop(0)
                if isinstance(item, Exception):
                    raise item
                return json.dumps(item)

            async def close(self):
                pass

        async def connect(token):
            i = state["n"]; state["n"] += 1
            if i >= len(laps):
                raise KeyboardInterrupt
            return FakeWS(laps[i])
        self.cli.backoff_sleep = lambda d: asyncio.sleep(0)
        return connect

    @staticmethod
    def _msg(seq, text="t"):
        return {"seq": seq, "content": {"payload": {"type": "text", "text": text}}}

    def _frame(self, seq):
        return [None, None, "room:R", "message:new", self._msg(seq)]

    def test_consumer_failure_does_not_advance_the_cursor(self):
        # 4/6: a raising on_event used to leave `seen` past the row forever
        self._room_rest(lambda after: [self._msg(5), self._msg(6)] if after == 4
                        else [self._msg(6)] if after == 5 else [])
        sent, got, failed = [], [], {"once": False}
        self.cli.ws_connect = self._laps_ws([[], [self._frame(7)]], sent)

        def on_event(ev):
            if ev["kind"] == "message" and ev["seq"] == 6 and not failed["once"]:
                failed["once"] = True
                raise OSError("disk full")
            got.append((ev["kind"], ev.get("seq")))
        with self.assertRaises(KeyboardInterrupt):
            asyncio.run(self.cli.listen_core({"access_token": "T", "user_id": "me"},
                                             "default", "R", on_event))
        self.assertEqual(got, [("joined", None), ("message", 5), ("reconnect", None),
                               ("joined", None), ("message", 6), ("message", 7)])

    def test_plain_events_mark_our_own(self):
        me = "me-1234"
        ev = self.cli.normalize_plain({"seq": 1, "sender_id": me, "content": {}}, me)
        self.assertTrue(ev["own"])
        self.assertEqual(ev["sender_binding"], "ok")
        self.assertFalse(self.cli.normalize_plain({"seq": 1, "sender_id": "x", "content": {}}, me)["own"])
        self.assertFalse(self.cli.normalize_plain({"seq": 1, "sender_id": me, "content": {}})["own"])

    def test_sealed_events_carry_the_sender_binding(self):
        rec = {"seq": 3, "kind": "application", "sender_id": "u", "payload": {"type": "text", "text": "x"},
               "sender_binding_failed": True}
        self.assertEqual(self.cli.normalize_sealed(rec)["sender_binding"], "failed")
        rec = {"seq": 3, "kind": "application", "sender_id": "u", "payload": {}, "sender_binding": "unresolved"}
        self.assertEqual(self.cli.normalize_sealed(rec)["sender_binding"], "unresolved")
        self.assertEqual(self.cli.normalize_sealed({"seq": 4, "kind": "handshake", "added": ["d"]})["sender_binding"], "ok")

    def test_channel_close_backs_off_with_an_event(self):
        self._room_rest(lambda after: [])
        sent, got = [], []
        self.cli.ws_connect = self._laps_ws([[[None, None, "room:R", "phx_close", {}]], []], sent)
        with self.assertRaises(KeyboardInterrupt):
            asyncio.run(self.cli.listen_core({"access_token": "T"}, "default", "R",
                                             lambda ev: got.append((ev["kind"], ev.get("raw")))))
        kinds = [k for k, _ in got]
        self.assertEqual(kinds, ["joined", "reconnect", "joined"])
        self.assertIn("phx_close", got[1][1])

    def test_initial_transient_is_retried_inside(self):
        self._room_rest(lambda after: [], fail_first=2)
        sent, got = [], []
        self.cli.ws_connect = self._laps_ws([[]], sent)
        with self.assertRaises(KeyboardInterrupt):
            asyncio.run(self.cli.listen_core({"access_token": "T"}, "default", "R",
                                             lambda ev: got.append(ev["kind"])))
        self.assertEqual(got, ["reconnect", "reconnect", "joined"])

    def test_non_retryable_4xx_stops_the_listener(self):
        self._room_rest(lambda after: self.cli.KlatalkUsage("HTTP 422: bad"))
        sent, got = [], []
        self.cli.ws_connect = self._laps_ws([[], []], sent)
        with self.assertRaises(self.cli.KlatalkUsage):
            asyncio.run(self.cli.listen_core({"access_token": "T"}, "default", "R",
                                             lambda ev: got.append(ev["kind"])))
        self.assertEqual(got, ["joined"])          # no reconnect lap
        self.assertIs(self.cli.error_class(422), self.cli.KlatalkUsage)
        self.assertIs(self.cli.error_class(426), self.cli.KlatalkUsage)

    def test_lock_contention_is_transient_for_the_listener(self):
        self.assertTrue(issubclass(self.cli.KlatalkBusy, self.cli.KlatalkTransient))
        self.assertGreater(self.cli.LOCK_TIMEOUT, self.cli.MLS_OP_TIMEOUT)
        self._room_rest(lambda after: self.cli.KlatalkBusy("lock held"))
        sent, got = [], []
        self.cli.ws_connect = self._laps_ws([[], []], sent)
        with self.assertRaises(KeyboardInterrupt):
            asyncio.run(self.cli.listen_core({"access_token": "T"}, "default", "R",
                                             lambda ev: got.append(ev["kind"])))
        self.assertEqual(got, ["joined", "reconnect", "joined", "reconnect"])

    def test_reconnect_event_carries_only_the_type_of_a_foreign_error(self):
        self._room_rest(lambda after: [])
        sent, got = [], []
        self.cli.ws_connect = self._laps_ws([[RuntimeError("SECRET-BODY")], []], sent)
        with self.assertRaises(KeyboardInterrupt):
            asyncio.run(self.cli.listen_core({"access_token": "T"}, "default", "R",
                                             lambda ev: got.append(ev)))
        rec = [e for e in got if e["kind"] == "reconnect"][0]
        self.assertEqual(rec["raw"], "RuntimeError")

    def test_backfill_delivers_page_by_page(self):
        pages = {4: [self._msg(i) for i in range(5, 205)], 204: [self._msg(205)]}
        fetches = []
        self._room_rest(lambda after: (fetches.append(after), pages.get(after, []))[1])
        sent, got = [], []
        self.cli.ws_connect = self._laps_ws([[]], sent)
        first_page_before_second_fetch = {}

        def on_event(ev):
            got.append(ev.get("seq"))
            if ev.get("seq") == 5:
                first_page_before_second_fetch["v"] = (fetches == [4])
        with self.assertRaises(KeyboardInterrupt):
            asyncio.run(self.cli.listen_core({"access_token": "T"}, "default", "R", on_event))
        self.assertEqual([s for s in got if s], list(range(5, 206)))
        self.assertTrue(first_page_before_second_fetch.get("v"),
                        "row 5 was delivered only after the whole gap was buffered")

    def test_do_listen_hole_refill_keeps_the_cursor_at_the_received_max(self):
        inbox = os.path.join(self.tmp, "inbox.jsonl")
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        for seq in (1, 2, 3, 5, 6):
            self.cli.record(inbox, {"topic": "room:R", "event": "message:new", "payload": self._msg(seq)})
        self._room_rest(lambda after: [self._msg(4), self._msg(5), self._msg(6)] if after == 3 else [],
                        room={"id": "R", "encryption_mode": "plain", "last_seq": 6,
                              "my_last_read_seq": 0, "members": []})
        sent = []
        self.cli.ws_connect = self._laps_ws([[]], sent)
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                asyncio.run(self.cli.do_listen({"access_token": "T"}, "R", inbox, profile="default"))
        with open(inbox) as f:
            seqs = [json.loads(l)["payload"]["seq"] for l in f]
        self.assertEqual(seqs, [1, 2, 3, 5, 6, 4])       # the hole refilled once
        self.assertEqual(self.cli.listen_cursor_read("default", "R"), 6)

    def test_do_listen_sealed_writes_message_decrypted_rows(self):
        inbox = os.path.join(self.tmp, "inbox.jsonl")
        os.makedirs(self.cli.mls_dir("default"), exist_ok=True)
        self._room_rest(lambda after: [], room={"id": "R", "encryption_mode": "mls10",
                                                "last_seq": 2, "my_last_read_seq": 0, "members": []})
        rec = {"seq": 1, "kind": "application", "sender_id": "u", "payload": {"type": "text", "text": "hi"}}
        self.cli.sealed_pump = lambda creds, profile, room_id: []
        ledger = []                               # empty at cold start…
        self.cli.ledger_read = lambda profile, room_id: list(ledger)
        sent = []
        # …then a live frame lands and the pump (stubbed) has put the row in
        self.cli.ws_connect = self._laps_ws([[[None, None, "room:R", "message:new", {"seq": 1}]]], sent)
        real_pump = self.cli.sealed_pump
        self.cli.sealed_pump = lambda creds, profile, room_id: ledger.append(rec) if not ledger else None
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(KeyboardInterrupt):
                asyncio.run(self.cli.do_listen({"access_token": "T"}, "R", inbox, profile="default"))
        self.assertIn("sealed room", out.getvalue())
        with open(inbox) as f:
            rows = [json.loads(l) for l in f]
        self.assertEqual(rows, [{"topic": "room:R", "event": "message:decrypted", "payload": rec}])
        self.assertFalse(os.path.exists(self.cli.listen_cursor_path("default", "R")))

    def test_cold_start_busy_is_retried_not_deafening(self):
        # P0 (mini-review): a KlatalkBusy from cold_start_cursor left `seen`
        # None forever — every later lap died on `seq <= None`
        self._room_rest(lambda after: [self._msg(5)] if after == 4 else [],
                        room={"id": "R", "encryption_mode": "plain", "last_seq": 4,
                              "my_last_read_seq": 4, "members": []})
        calls = {"n": 0}
        real = self.cli.cold_start_cursor

        def flaky(room, profile, sealed):
            calls["n"] += 1
            if calls["n"] == 1:
                raise self.cli.KlatalkBusy("lock held")
            return real(room, profile, sealed)
        self.cli.cold_start_cursor = flaky
        sent, got = [], []
        self.cli.ws_connect = self._laps_ws([[]], sent)
        with self.assertRaises(KeyboardInterrupt):
            asyncio.run(self.cli.listen_core({"access_token": "T"}, "default", "R",
                                             lambda ev: got.append((ev["kind"], ev.get("seq")))))
        self.assertEqual(calls["n"], 2)
        self.assertEqual(got, [("reconnect", None), ("joined", None), ("message", 5)])

    def test_do_listen_marks_a_row_seen_only_once_the_inbox_holds_it(self):
        inbox = os.path.join(self.tmp, "inbox.jsonl")
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        self._room_rest(lambda after: [self._msg(5), self._msg(6)] if after == 4
                        else [self._msg(6)] if after == 5 else [])
        sent = []
        self.cli.ws_connect = self._laps_ws([[], [self._frame(7)]], sent)
        real_record, failed = self.cli.record, {"once": False}

        def flaky_record(path, rec):
            if rec.get("payload", {}).get("seq") == 6 and not failed["once"]:
                failed["once"] = True
                raise OSError("disk full")
            real_record(path, rec)
        self.cli.record = flaky_record
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                asyncio.run(self.cli.do_listen({"access_token": "T"}, "R", inbox, profile="default"))
        with open(inbox) as f:
            seqs = [json.loads(l)["payload"]["seq"] for l in f]
        self.assertEqual(seqs, [5, 6, 7])
        self.assertEqual(self.cli.listen_cursor_read("default", "R"), 7)

    def test_pin_merge_never_overwrites_a_newer_pin(self):
        # two rooms verify concurrently; A decided against a snapshot with
        # no pin for X while B pinned X — A must not replace B's pin
        os.makedirs(self.cli.mls_dir("default"), exist_ok=True)
        creds = {"device_id": "me", "access_token": "T"}
        leaves = [{"identity": "me", "signature_key_b64": "TUU="},
                  {"identity": "X", "signature_key_b64": "QkJCQg=="}]
        self.cli.mls_op = lambda c, p, op, payload=None: (
            {"members": leaves} if op == "list-members" else {"public_key_b64": "TUU="})
        self.cli.get_room = lambda c, r, strict=True: {"id": r, "members": [{"user_id": "u"}]}
        self.cli.device_key_map = lambda c, room: {"X": {"user_id": "u", "public_key": "QkJCQg==", "generation": 1}}
        real_load = self.cli.load_pins
        state = {"raced": False}

        def load_pins(profile):
            pins = real_load(profile)
            if not state["raced"] and not pins:
                # B lands between A's snapshot and A's merge
                state["raced"] = True
                self.cli.save_pins(profile, {"X": {"user_id": "u", "key": "QUFBQQ==", "generation": 1}})
            return pins
        self.cli.load_pins = load_pins
        with contextlib.redirect_stderr(io.StringIO()):
            ok = self.cli.verify_roster(creds, "default", "A", quick=True)
        self.assertFalse(ok)                                   # raced → unresolved, fail closed
        self.assertEqual(real_load("default")["X"]["key"], "QUFBQQ==")   # B's pin survived

    def test_corrupt_outbox_is_named_not_busy(self):
        os.makedirs(self.cli.mls_dir("default"), exist_ok=True)
        with open(self.cli.outbox_path("default", "R"), "w") as f:
            f.write("{half")
        with self.assertRaises(self.cli.KlatalkUsage):
            self.cli._outbox_load("default", "R")
        with open(self.cli.outbox_path("default", "R"), "w") as f:
            f.write("{}")
        with self.assertRaises(self.cli.KlatalkUsage):
            self.cli._outbox_load("default", "R")

    def test_send_gate_entries_are_reclaimed(self):
        async def run():
            async with self.cli._send_gate("p", "R"):
                self.assertEqual(len(self.cli._send_gates), 1)
            self.assertEqual(len(self.cli._send_gates), 0)
        asyncio.run(run())

    # -- sealed sends --------------------------------------------------

    def test_outbox_survives_an_unknown_outcome(self):
        sends = []
        self._sealed_stubs(sends)

        async def lost(*a, **k):
            raise self.cli.KlatalkTransient("no reply")
        self.cli.do_send = lost
        creds = {"user_id": "me", "device_id": "d1", "access_token": "T"}
        with self.assertRaises(self.cli.KlatalkTransient):
            self._sealed_send(creds, "default", "R", {"type": "text", "text": "x"})
        journal, age = self.cli._outbox_load("default", "R")
        self.assertIsNotNone(journal)
        self.assertEqual(journal["payload"]["text"], "x")
        # and a second sender right after sees it as in flight, not stale
        self._sealed_stubs(sends)
        with self.assertRaises(self.cli.KlatalkBusy):
            self._sealed_send(creds, "default", "R", {"type": "text", "text": "y"})

    def test_outbox_clear_respects_ownership(self):
        os.makedirs(self.cli.mls_dir("default"), exist_ok=True)
        self.cli.replace_private(self.cli.outbox_path("default", "R"),
                                 lambda f: f.write(json.dumps({"client_msg_id": "b", "content": {}})))
        self.assertFalse(self.cli._outbox_clear("default", "R", "a"))
        self.assertTrue(os.path.exists(self.cli.outbox_path("default", "R")))
        self.assertTrue(self.cli._outbox_clear("default", "R", "b"))
        self.assertFalse(os.path.exists(self.cli.outbox_path("default", "R")))

    def test_concurrent_sealed_sends_in_one_process_are_serialised(self):
        sends = []
        self._sealed_stubs(sends)
        gate = asyncio.Event()
        real_send = self.cli.do_send

        async def slow_send(*a, **k):
            await gate.wait()
            return await real_send(*a, **k)
        self.cli.do_send = slow_send
        creds = {"user_id": "me", "device_id": "d1", "access_token": "T"}

        async def run():
            t1 = asyncio.create_task(self.cli.sealed_send_async(creds, "default", "R", {"type": "text", "text": "one"}))
            t2 = asyncio.create_task(self.cli.sealed_send_async(creds, "default", "R", {"type": "text", "text": "two"}))
            await asyncio.sleep(0.2)
            gate.set()
            return await asyncio.gather(t1, t2)
        seqs = asyncio.run(run())
        self.assertEqual(sorted(seqs), [21, 22])
        self.assertEqual([r["payload"]["text"] for r in self.cli.ledger_read("default", "R")], ["one", "two"])
        self.assertFalse(os.path.exists(self.cli.outbox_path("default", "R")))

    def test_roster_ladder_runs_outside_the_room_lock(self):
        sends = []
        self._sealed_stubs(sends)
        seen = []
        lock_path = os.path.join(self.cli.mls_dir("default"), "lock-room-R")

        def verify(creds, profile, room_id, quick=False):
            held = getattr(self.cli._lock_depth, "held", {}) or {}
            seen.append((quick, held.get(lock_path, 0) > 0))
            return True
        self.cli.verify_roster = verify
        self._sealed_send({"user_id": "me", "device_id": "d1", "access_token": "T"},
                          "default", "R", {"type": "text", "text": "x"})
        self.assertEqual(seen, [(False, False), (True, True)])   # ladder unlocked, re-check locked

    # -- hygiene and platform -------------------------------------------

    def test_mls_op_garbage_does_not_echo_helper_output(self):
        os.makedirs(os.path.dirname(self.cli.MLS_BIN), exist_ok=True)
        open(self.cli.MLS_BIN, "w").close()

        class R:
            returncode, stdout, stderr = 0, '{"receipts":[{"plaintext_b64":"U0VDUkVU"}]}x', "e"
        self.cli.subprocess.run = lambda *a, **k: R()
        self.cli._MLS_VERSION_SHOWN = True
        with self.assertRaises(self.cli.KlatalkMls) as cm:
            self.cli.mls_op({"device_id": "d"}, "default", "ingest", {})
        self.assertNotIn("U0VDUkVU", str(cm.exception))

    def test_windows_has_no_fcntl_and_the_lock_is_a_noop(self):
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        self.cli._fcntl = None
        with self.cli.room_lock("default", "R"):
            with self.cli.room_lock("default", "R"):
                pass
        self.assertEqual(self.cli._lock_depth.held.get(
            os.path.join(self.cli.mls_dir("default"), "lock-room-R")), 0)

    def test_attachment_404_is_not_membership_loss(self):
        class O:
            def open(self_, req, timeout=0):
                raise urllib.error.HTTPError(req.full_url, 404, "x", {}, io.BytesIO(b"{}"))
        self.cli.OPENER = O()
        with self.assertRaises(self.cli.KlatalkError) as cm:
            self.cli.fetch_upload({"access_token": "T"}, "/uploads/R/a.png", 10)
        self.assertNotIsInstance(cm.exception, self.cli.KlatalkMembership)

    def test_cmd_read_prints_both_confirmations(self):
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        self.cli.write_private(self.cli.cred_path("default"),
                               lambda f: json.dump({"access_token": "T"}, f))
        answers = iter([7, None])

        async def do_read(creds, room_id, seq):
            return next(answers)
        self.cli.do_read = do_read
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.cli.cmd_read(argparse_ns(room="R", seq=7))
            self.cli.cmd_read(argparse_ns(room="R", seq=7))
        self.assertEqual(out.getvalue().splitlines(),
                         ["read 7 (server confirmed 7)",
                          "read 7 (no change — already read up to that point)"])




class TestBridge(Base):
    """v1.5 `klatalk bridge` — the core as a child process for hosts that
    cannot import it (OpenClaw is Node). Events out, commands in, one JSON
    object per line; the wake rule, the send budget and the read mark's
    monotonicity live here so every host shares them."""

    def _bridge(self, rooms=("R1",), owner="OWNER", wake_on="humans", budget=0):
        c = self.cli
        creds = {"access_token": "tok", "user_id": "ME", "nickname": "Bot"}
        b = c.Bridge(creds, "p", list(rooms), owner, wake_on, budget)
        b.out = []

        async def emit(obj):
            b.out.append(obj)
        b.emit = emit
        b.cache["R1"] = {"id": "R1", "name": "Bench", "encryption_mode": "plain",
                         "members": [{"user_id": "OWNER", "nickname": "Own"},
                                     {"user_id": "H", "nickname": "Human"},
                                     {"user_id": "A", "nickname": "Other",
                                      "bio": "AI member · x"}]}
        return b

    @staticmethod
    def _msg(sid, text, **k):
        ev = {"kind": "message", "seq": k.pop("seq", 1), "sender_id": sid,
              "payload": {"type": "text", "text": text},
              "sender_binding": k.pop("binding", "ok"), "inserted_at": "2026-08-23T00:00:00Z"}
        ev.update(k)
        return ev

    def test_failure_kinds_are_a_closed_vocabulary(self):
        c = self.cli
        cases = [(c.SendRejected("r"), "rejected"), (c.KlatalkMembership("m"), "forbidden"),
                 (c.KlatalkQuota("q"), "rate_limited"), (c.KlatalkBusy("b"), "transient"),
                 (c.KlatalkTransient("t"), "transient"), (c.KlatalkDesync("d"), "blocked"),
                 (c.KlatalkBlocked("x"), "blocked"), (c.KlatalkMls("h"), "mls"),
                 (c.KlatalkAuth("a"), "auth"), (c.KlatalkUsage("u"), "usage"),
                 (ValueError("secret detail"), "unknown")]
        for e, kind in cases:
            self.assertEqual(c.bridge_kind(e), kind, repr(e))

    def test_line_is_the_text_or_a_marker(self):
        c = self.cli
        self.assertNotIn("\x07", c.bridge_line({"type": "text", "text": "hi\x07there"}))
        self.assertEqual(c.bridge_line({"type": "image", "url": "/uploads/R/x.jpg"}), "(image)")
        self.assertEqual(c.bridge_line({"type": "file", "name": "a.pdf", "size": 12}),
                         "(file) a.pdf 12")
        self.assertEqual(c.bridge_line({"type": "text", "text": "",
                                        "reaction": {"action": "like", "target_seq": 4}}),
                         "(reaction like on #4)")

    def test_wake_rule_humans_and_named_ai_only(self):
        b = self._bridge()
        run = lambda ev: asyncio.run(b.on_event("R1", ev))
        run(self._msg("H", "hi"))
        self.assertEqual((b.out[-1]["ev"], b.out[-1]["wake"], b.out[-1]["owner"],
                          b.out[-1]["nick"], b.out[-1]["is_ai"]),
                         ("message", True, False, "Human", False))
        run(self._msg("A", "hi"))
        self.assertEqual((b.out[-1]["wake"], b.out[-1]["is_ai"]), (False, True))
        run(self._msg("A", "Bot, hi"))
        self.assertTrue(b.out[-1]["wake"])
        run(self._msg("OWNER", "do it"))
        self.assertTrue(b.out[-1]["owner"])
        # the label and the crypto disagree: never the owner's voice
        run(self._msg("OWNER", "do it", binding="failed"))
        self.assertEqual((b.out[-1]["owner"], b.out[-1]["sender_binding"]), (False, "failed"))
        n = len(b.out)
        run(self._msg("ME", "echo", own=True))
        run(self._msg("H", "gone", deleted=True))
        run(self.cli.normalize_plain({"seq": 9, "sender_id": "H",
                                      "content": {"payload": {"type": "system", "text": "x"}}}))
        self.assertEqual(len(b.out), n)           # own, deleted and system rows emit nothing
        b2 = self._bridge(wake_on="all")
        asyncio.run(b2.on_event("R1", self._msg("A", "hi")))
        self.assertTrue(b2.out[-1]["wake"])
        # a reaction is the room's quiet register — never a wake, even on "all"
        ev = self._msg("H", "❤️", seq=7)
        ev["payload"]["reaction"] = {"action": "like", "target_seq": 3}
        asyncio.run(b2.on_event("R1", ev))
        self.assertEqual((b2.out[-1]["wake"], b2.out[-1]["text"]), (False, "(reaction like on #3)"))

    def test_budget_charges_woken_rows_per_room_per_day(self):
        b = self._bridge(budget=1)
        run = lambda ev: asyncio.run(b.on_event("R1", ev))
        run(self._msg("A", "unnamed"))            # no wake, no charge
        run(self._msg("H", "one"))
        self.assertEqual((b.out[-1]["wake"], b.out[-1]["budget_spent"]), (True, False))
        run(self._msg("H", "two"))
        self.assertEqual((b.out[-1]["wake"], b.out[-1]["budget_spent"]), (False, True))
        b.turns["R1"] = [time.time() - 86401]     # yesterday's wake has expired
        run(self._msg("H", "three"))
        self.assertTrue(b.out[-1]["wake"])
        # a budget a member can spend never silences the owner
        run(self._msg("H", "four"))
        self.assertFalse(b.out[-1]["wake"])
        run(self._msg("OWNER", "still here"))
        self.assertEqual((b.out[-1]["wake"], b.out[-1]["owner"], b.out[-1]["budget_spent"]),
                         (True, True, False))

    def test_lifecycle_events_and_removal_stop_the_room(self):
        b = self._bridge()
        run = lambda ev: asyncio.run(b.on_event("R1", ev))
        run({"kind": "joined", "sealed": True})
        # the live boundary rides on joined: rows at or below it are backlog
        self.assertEqual(b.out[-1], {"ev": "joined", "room": "R1", "sealed": True, "last_seq": 0})
        b.cache["R1"]["last_seq"] = 41
        run({"kind": "joined", "sealed": False})
        self.assertEqual(b.out[-1]["last_seq"], 41)
        # a row that raises inside the handler is dropped with an error event,
        # never re-delivered forever (v1.5 133)
        b.roster = lambda rid: (_ for _ in ()).throw(RuntimeError("boom"))
        run(self._msg("H", "hi", seq=9))
        self.assertEqual(b.out[-1], {"ev": "error", "room": "R1", "seq": 9, "why": "RuntimeError"})
        del b.roster
        run({"kind": "message", "seq": 10, "sender_id": "H", "payload": "not an object"})
        self.assertEqual(b.out[-1]["seq"], 9)            # nothing emitted for a non-object payload
        run({"kind": "reconnect", "delay": 4, "raw": "socket closed"})
        self.assertEqual(b.out[-1]["ev"], "reconnect")
        run({"kind": "desync"}); run({"kind": "desync"})
        self.assertEqual([o["ev"] for o in b.out].count("desync"), 1)
        # a roster refresh that fails keeps the last roster (a known AI stays an AI)
        c = self.cli
        c.get_room = lambda creds, rid, strict=True: (_ for _ in ()).throw(RuntimeError("down"))
        run({"kind": "frame", "event": "member:added", "raw": {"user_id": "X"}})
        self.assertIn("R1", b.cache)
        run({"kind": "frame", "event": "member:removed", "raw": {"user_id": "ME"}})
        self.assertIn("R1", b.stopped)
        self.assertEqual(b.out[-1]["ev"], "stopped")

    def test_commands_refuse_foreign_rooms_and_keep_reads_monotonic(self):
        b = self._bridge()
        c = self.cli
        reads = []

        async def do_read(creds, room, seq):
            reads.append(seq)
            return seq
        c.do_read = do_read
        h = lambda req: asyncio.run(b.handle(json.dumps(req) if isinstance(req, dict) else req))
        h({"id": "1", "cmd": "send", "room": "R9", "text": "x"})
        self.assertEqual((b.out[-1]["id"], b.out[-1]["ok"], b.out[-1]["kind"]), ("1", False, "usage"))
        h({"id": "2", "cmd": "read", "room": "R1", "seq": 5})
        h({"id": "3", "cmd": "read", "room": "R1", "seq": 3})
        self.assertEqual(reads, [5])
        self.assertEqual((b.out[-1]["ok"], b.out[-1]["skipped"], b.out[-1]["last_read_seq"]),
                         (True, True, 5))
        h("not json")
        self.assertEqual(b.out[-1]["kind"], "usage")
        h({"id": "4", "cmd": "dance", "room": "R1"})
        self.assertEqual(b.out[-1]["kind"], "usage")
        h({"id": "5", "cmd": "roster", "room": "R1"})
        self.assertEqual(b.out[-1]["name"], "Bench")
        self.assertEqual([m["is_ai"] for m in b.out[-1]["members"]], [False, False, True])

    def test_send_runs_through_the_core_and_maps_failures(self):
        b = self._bridge()
        c = self.cli
        calls = []

        async def send_message(creds, profile, room, payload=None, *, text=None,
                               reply_to=None, read_through=None):
            calls.append((room["id"], payload, text, reply_to, read_through))
            if text == "boom":
                raise c.KlatalkMembership("kicked")
            if text == "crash":
                raise RuntimeError("secret detail")
            return 42
        c.send_message = send_message
        h = lambda req: asyncio.run(b.handle(json.dumps(req)))
        h({"id": "1", "cmd": "send", "room": "R1", "text": "hi", "reply_to": 7})
        self.assertEqual(b.out[-1], {"id": "1", "ok": True, "seq": 42})
        self.assertEqual(calls[-1], ("R1", None, "hi", 7, None))   # never signs read
        h({"id": "2", "cmd": "send", "room": "R1", "text": "boom"})
        self.assertEqual((b.out[-1]["kind"], b.out[-1]["why"]), ("forbidden", "kicked"))
        h({"id": "3", "cmd": "send", "room": "R1", "text": "crash"})
        self.assertEqual((b.out[-1]["kind"], b.out[-1]["why"]), ("unknown", "RuntimeError"))
        h({"id": "4", "cmd": "send", "room": "R1"})
        self.assertEqual(b.out[-1]["kind"], "usage")
        h({"id": "5", "cmd": "send", "room": "R1", "text": "x", "reply_to": "7"})
        self.assertEqual(b.out[-1]["kind"], "usage")
        # a reaction is the app's text+sidecar shape, never signs read
        h({"id": "6", "cmd": "react", "room": "R1", "seq": 35})
        self.assertEqual(b.out[-1], {"id": "6", "ok": True, "seq": 42})
        self.assertEqual(calls[-1][1], {"type": "text", "text": "❤️",
                                        "reaction": {"target_seq": 35, "action": "add"}})
        self.assertIsNone(calls[-1][4])
        h({"id": "7", "cmd": "react", "room": "R1", "seq": 35, "action": "unlike"})
        self.assertEqual(b.out[-1]["kind"], "usage")
        # leaving stops the room for good and says so
        left = []
        c.leave_room = lambda creds, profile, rid: left.append(rid) or False
        h({"id": "8", "cmd": "leave", "room": "R1"})
        self.assertEqual(left, ["R1"])
        self.assertIn("R1", b.stopped)
        self.assertEqual(b.out[-2], {"ev": "stopped", "room": "R1", "why": "left"})
        self.assertEqual(b.out[-1], {"id": "8", "ok": True, "left": "R1", "sealed": False})

    def test_fetch_writes_a_fresh_private_file_only(self):
        b = self._bridge()
        c = self.cli
        c.fetch_upload = lambda creds, path, mb: b"img"
        out = os.path.join(self.tmp, "m.jpg")
        h = lambda req: asyncio.run(b.handle(json.dumps(req)))
        h({"id": "1", "cmd": "fetch", "room": "R1", "url": "/uploads/R1/a.jpg",
           "max_bytes": 10, "out": out})
        self.assertEqual((b.out[-1]["ok"], b.out[-1]["bytes"]), (True, 3))
        if os.name != "nt":                        # Windows has no POSIX modes
            self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o600)
        h({"id": "2", "cmd": "fetch", "room": "R1", "url": "/uploads/R1/a.jpg",
           "max_bytes": 10, "out": out})
        self.assertFalse(b.out[-1]["ok"])          # never over an existing file
        h({"id": "3", "cmd": "fetch", "room": "R1", "url": "/uploads/R1/a.jpg",
           "max_bytes": 10, "out": "rel.jpg"})
        self.assertEqual(b.out[-1]["kind"], "usage")
        # another room's attachment is not this room's to pull (sec audit, 1/6 + verified)
        h({"id": "4", "cmd": "fetch", "room": "R1", "url": "/uploads/R2/a.jpg",
           "max_bytes": 10, "out": os.path.join(self.tmp, "n.jpg")})
        self.assertEqual(b.out[-1]["kind"], "usage")
        h({"id": "5", "cmd": "fetch", "url": "/uploads/R1/a.jpg", "max_bytes": 10,
           "out": os.path.join(self.tmp, "o.jpg")})
        self.assertEqual(b.out[-1]["kind"], "usage")   # no room, no fetch

    def test_stdin_eof_ends_the_bridge_and_auth_loss_exits_2(self):
        c = self.cli

        async def listen_forever(creds, profile, room_id, on_event, cursor=None):
            await on_event({"kind": "joined", "sealed": False})
            await asyncio.Event().wait()
        c.listen_core = listen_forever
        c.get_room = lambda creds, rid, strict=True: {"id": rid, "name": "Bench", "members": []}
        b = self._bridge()
        real = sys.stdin
        sys.stdin = io.StringIO("")               # the parent closed the pipe
        try:
            rc = asyncio.run(b.serve())
        finally:
            sys.stdin = real
        self.assertEqual(rc, 0)
        self.assertEqual([o["ev"] for o in b.out][:2], ["hello", "joined"])
        self.assertEqual(b.out[0]["version"], c.__version__)

        def no_auth(creds, rid, strict=True):
            raise c.KlatalkAuth("token rejected")
        c.get_room = no_auth
        b2 = self._bridge()
        r, w = os.pipe()                           # a parent that never speaks
        sys.stdin = os.fdopen(r)
        try:
            rc = asyncio.run(b2.serve())
        finally:
            sys.stdin = real
            os.close(w)
        self.assertEqual(rc, 2)
        self.assertEqual(b2.out[-1], {"ev": "fatal", "why": "token rejected"})

    def test_bridge_refuses_without_file_locks(self):
        c = self.cli
        c._fcntl = None
        ns = argparse_ns(profile="p", rooms="R1", owner="", wake_on="humans",
                         max_turns_per_day=None)
        with self.assertRaises(c.KlatalkUsage):
            asyncio.run(c.bridge_main(ns))



class TestCorePin(unittest.TestCase):
    """Each plugin directory ships the SHA-256 of the bin/klatalk it was
    released with (core.sha256) and verifies the installed CLI against it
    before running a line of it — the plugin install pins its own
    directory, not the separately installed core. The two must not drift."""

    def test_every_plugin_pins_the_current_core(self):
        import hashlib
        root = os.path.dirname(os.path.dirname(BIN))
        with open(BIN, "rb") as f:
            want = hashlib.sha256(f.read()).hexdigest()
        for plugin in ("hermes", "openclaw"):
            path = os.path.join(root, "plugins", plugin, "klatalk", "core.sha256")
            with open(path, encoding="utf-8") as f:
                got = f.read().split()[0]
            self.assertEqual(got, want, f"{path} is stale — run tools/pin-core.py")


for _n in [n for n in dir(TestCoreLibrary) if n.startswith("test_")]:
    setattr(TestCoreFixRound, _n, None)   # helpers are inherited, tests are not

if __name__ == "__main__":
    unittest.main()
