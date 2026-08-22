"""Hermes platform adapter tests — run inside a Hermes checkout's venv:

    cd tests && HERMES_HOME=/tmp/hh PYTHONPATH=/path/to/hermes-agent \
      /path/to/hermes-agent/venv/bin/python -m unittest test_hermes_adapter

Skipped when Hermes is not importable (CI runs the CLI tests only).
The core is the real bin/klatalk, loaded the way the adapter loads it;
only its network-touching functions are stubbed per test.
"""
import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PLUGIN_PARENT = os.path.join(REPO, "plugins", "hermes")   # contains klatalk/
sys.path.insert(0, PLUGIN_PARENT)
os.environ.setdefault("KLATALK_CLI", os.path.join(REPO, "bin", "klatalk"))

try:
    from gateway.platform_registry import PlatformEntry, platform_registry
    from gateway.platforms.base import ProcessingOutcome
    from gateway.config import PlatformConfig
    HERMES = True
except Exception:                                  # no Hermes on this machine
    HERMES = False


class FakeCtx:
    def __init__(self):
        self.kwargs = None

    def register_platform(self, name, label, adapter_factory, check_fn,
                          validate_config=None, required_env=None,
                          install_hint="", **entry_kwargs):
        self.kwargs = dict(name=name, label=label, **entry_kwargs)
        entry = PlatformEntry(name=name, label=label, adapter_factory=adapter_factory,
                              check_fn=check_fn, validate_config=validate_config,
                              required_env=required_env or [], install_hint=install_hint,
                              source="plugin", **entry_kwargs)
        platform_registry.register(entry)
        return entry


@unittest.skipUnless(HERMES, "Hermes gateway not importable")
class AdapterBase(unittest.TestCase):
    ROOM = "room-aaaa-1111"
    OWNER = "owner-user-id-0001"
    ME = "agent-user-id-0002"
    OTHER = "other-user-id-0003"
    AI = "ai-user-id-0004"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ.update({
            "KLATALK_PROFILE": "seat", "KLATALK_ROOMS": self.ROOM,
            "KLATALK_OWNER_ID": self.OWNER, "KLATALK_ALLOW_ALL_USERS": "true",
            "KLATALK_HOME": self.tmp, "KLATALK_TOOL_ROOMS": "",
            "KLATALK_MAX_TURNS_PER_DAY": "", "KLATALK_HOME_CHANNEL": "",
        })
        os.environ.pop("KLATALK_API", None)
        import klatalk.adapter as A
        A._core = None
        self.A = A
        self.ctx = FakeCtx()
        A.register(self.ctx)
        seed = A._env_enablement()
        self.cfg = PlatformConfig(enabled=True, extra=seed)
        self.adapter = A.KlatalkAdapter(self.cfg)
        self.core = A.load_core()
        self.adapter.core = self.core
        self.adapter.creds = {"user_id": self.ME, "device_id": "d", "nickname": "Seat",
                              "access_token": "T"}
        self.room = {"id": self.ROOM, "name": "bench", "encryption_mode": "plain",
                     "last_seq": 5, "my_last_read_seq": 5,
                     "members": [{"user_id": self.OWNER, "nickname": "경훈", "bio": ""},
                                 {"user_id": self.ME, "nickname": "Seat", "bio": "AI member"},
                                 {"user_id": self.OTHER, "nickname": "Guest", "bio": ""},
                                 {"user_id": self.AI, "nickname": "Bot", "bio": "AI member · x"}]}
        self.adapter._rooms[self.ROOM] = self.room
        self.adapter._joined[self.ROOM] = asyncio.Event()
        self.handled = []

        async def capture(event):
            self.handled.append(event)
        self.adapter.handle_message = capture

    def run_async(self, coro):
        return asyncio.run(coro)

    def message(self, sender, text, seq=6, **extra):
        ev = {"kind": "message", "seq": seq, "sender_id": sender, "reply_to_seq": None,
              "payload": {"type": "text", "text": text}, "sealed": False, "own": False,
              "deleted": False, "inserted_at": "2026-08-22T12:00:00Z", "raw": {}}
        ev.update(extra)
        return ev


class TestRegistrationAndConfig(AdapterBase):
    def test_registration_contract(self):
        kw = self.ctx.kwargs
        self.assertEqual(kw["name"], "klatalk")
        self.assertEqual(kw["allow_all_env"], "KLATALK_ALLOW_ALL_USERS")
        self.assertFalse(kw["allow_update_command"])
        self.assertEqual(kw["max_message_length"], 4000)
        self.assertIs(kw["standalone_sender_fn"], self.A._standalone_send)
        self.assertIs(kw["validate_target_ref_fn"], self.A._validate_target_ref)
        self.assertEqual(kw["cron_deliver_env_var"], "KLATALK_HOME_CHANNEL")
        self.assertTrue(platform_registry.is_registered("klatalk"))

    def test_required_settings_are_named_not_defaulted(self):
        for key in ("KLATALK_PROFILE", "KLATALK_ROOMS", "KLATALK_OWNER_ID",
                    "KLATALK_ALLOW_ALL_USERS"):
            saved = os.environ.pop(key)
            try:
                problems = self.A.Settings().problems()
                self.assertTrue(any(key in p for p in problems), (key, problems))
            finally:
                os.environ[key] = saved
        os.environ["KLATALK_HOME_CHANNEL"] = "not-a-room"
        self.assertTrue(any("HOME_CHANNEL" in p for p in self.A.Settings().problems()))
        os.environ["KLATALK_HOME_CHANNEL"] = ""
        os.environ["KLATALK_TOOL_ROOMS"] = "other-room"
        self.assertTrue(any("TOOL_ROOMS" in p for p in self.A.Settings().problems()))

    def test_env_enablement_seeds_session_guards(self):
        seed = self.A._env_enablement()
        self.assertIs(seed["group_sessions_per_user"], False)
        self.assertIs(seed["thread_sessions_per_user"], False)
        self.assertEqual(seed["rooms"], [self.ROOM])
        self.assertEqual(seed["owner_id"], self.OWNER)
        self.assertFalse(self.adapter.config.gateway_restart_notification)
        self.assertFalse(self.adapter.config.typing_indicator)

    def test_connect_refuses_a_per_member_session_shape(self):
        class Runner:
            class config:
                group_sessions_per_user = True
        self.adapter.gateway_runner = Runner()
        ok = self.run_async(self.adapter.connect())
        self.assertFalse(ok)
        self.assertTrue(self.adapter.has_fatal_error)
        self.assertIn("group_sessions_per_user", self.adapter._fatal_error_message)

    def test_connect_refuses_unseeded_extra(self):
        class Runner:
            class config:
                group_sessions_per_user = False
        adapter = self.A.KlatalkAdapter(PlatformConfig(enabled=True, extra={}))
        adapter.gateway_runner = Runner()
        self.assertFalse(self.run_async(adapter.connect()))
        self.assertIn("seeded", adapter._fatal_error_message)


class TestInbound(AdapterBase):
    def test_owner_and_member_events_are_marked_and_gated(self):
        async def do_read(creds, room_id, seq):
            return seq
        self.core.do_read = do_read
        self.run_async(self.adapter._on_event(self.ROOM, self.message(self.OWNER, "해줘", seq=6)))
        self.run_async(self.adapter.on_processing_complete(self.handled[0], ProcessingOutcome.SUCCESS))
        self.run_async(self.adapter._on_event(self.ROOM, self.message(self.OTHER, "/stop", seq=7)))
        owner, member = self.handled
        self.assertTrue(owner.text.startswith("[owner] 해줘"))
        self.assertTrue(owner.allow_gateway_control)
        self.assertTrue(member.text.startswith("[member] /stop"))
        self.assertFalse(member.allow_gateway_control)
        self.assertFalse(member.is_command())          # a member's /stop is data
        self.assertEqual(member.user_name, f"Guest·{self.OTHER[:8]}")
        self.assertEqual(owner.source.chat_type, "group")
        self.assertEqual(owner.source.chat_id, self.ROOM)
        self.assertEqual(self.adapter._inflight[self.ROOM], 7)

    def test_own_and_system_and_deleted_rows_do_not_wake(self):
        self.run_async(self.adapter._on_event(self.ROOM, self.message(self.ME, "me", seq=6)))
        self.run_async(self.adapter._on_event(self.ROOM, self.message(self.OTHER, "x", seq=7, kind="system")))
        self.run_async(self.adapter._on_event(self.ROOM, self.message(self.OTHER, "x", seq=8, deleted=True)))
        self.assertEqual(self.handled, [])
        self.assertNotIn(self.ROOM, self.adapter._inflight)

    def test_an_ai_member_wakes_only_by_name(self):
        self.run_async(self.adapter._on_event(self.ROOM, self.message(self.AI, "hello all", seq=6)))
        self.assertEqual(self.handled, [])
        self.run_async(self.adapter._on_event(self.ROOM, self.message(self.AI, "Seat, 어때?", seq=7)))
        self.assertEqual(len(self.handled), 1)

    def test_daily_budget_keeps_messages_unread(self):
        self.adapter.settings.max_turns_per_day = 1
        self.run_async(self.adapter._on_event(self.ROOM, self.message(self.OWNER, "a", seq=6)))
        self.adapter._busy.clear()                              # turn 1 done (read stubbed out)
        self.run_async(self.adapter._on_event(self.ROOM, self.message(self.OWNER, "b", seq=7)))
        self.assertEqual(len(self.handled), 1)
        self.assertEqual(self.adapter._inflight[self.ROOM], 6)    # 7 was never handed over

    def test_read_mark_follows_success_only_and_the_inflight_max(self):
        marks = []

        async def do_read(creds, room_id, seq):
            marks.append((room_id, seq)); return seq
        self.core.do_read = do_read
        self.run_async(self.adapter._on_event(self.ROOM, self.message(self.OWNER, "a", seq=6)))
        self.adapter._busy.clear()                              # pretend turn 1 already ended…
        self.run_async(self.adapter._on_event(self.ROOM, self.message(self.OWNER, "b", seq=7)))
        first = self.handled[0]                       # …but its completion hook fires late
        self.run_async(self.adapter.on_processing_complete(first, ProcessingOutcome.FAILURE))
        self.assertEqual(marks, [])
        self.assertEqual(self.adapter._inflight[self.ROOM], 7)
        self.run_async(self.adapter.on_processing_complete(first, ProcessingOutcome.SUCCESS))
        self.assertEqual(marks, [(self.ROOM, 7)])       # through the max, not the event's seq
        self.assertNotIn(self.ROOM, self.adapter._inflight)

    def test_failed_read_mark_is_kept_for_the_next_success(self):
        async def do_read(creds, room_id, seq):
            raise self.core.KlatalkTransient("down")
        self.core.do_read = do_read
        self.run_async(self.adapter._on_event(self.ROOM, self.message(self.OWNER, "a", seq=6)))
        self.run_async(self.adapter.on_processing_complete(self.handled[0], ProcessingOutcome.SUCCESS))
        self.assertEqual(self.adapter._inflight[self.ROOM], 6)

    def test_self_removal_stops_the_room(self):
        done = asyncio.Event()

        async def loop():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                done.set(); raise

        async def run():
            self.adapter._tasks[self.ROOM] = asyncio.create_task(loop())
            await asyncio.sleep(0)
            await self.adapter._on_event(self.ROOM, {"kind": "frame", "event": "member:removed",
                                                     "raw": {"user_id": self.ME}})
            await asyncio.wait_for(done.wait(), 2)
        self.run_async(run())
        self.assertIn(self.ROOM, self.adapter._stopped)

    def test_image_payload_is_fetched_capped_and_cached(self):
        fetched = []

        def fetch_upload(creds, url, max_bytes):
            fetched.append((url, max_bytes)); return b"\x89PNG fake"
        self.core.fetch_upload = fetch_upload
        self.A.cache_image_from_bytes = lambda data, ext: f"/cache/x{ext}"
        ev = self.message(self.OWNER, "", seq=6, payload={"type": "image", "url": "/uploads/r/a.png", "w": 1, "h": 1})
        self.run_async(self.adapter._on_event(self.ROOM, ev))
        self.assertEqual(self.handled[0].media_urls, ["/cache/x.png"])
        self.assertEqual(fetched[0][0], "/uploads/r/a.png")
        self.assertGreater(fetched[0][1], 0)

    def test_room_loop_classifies_core_errors(self):
        async def gone(*a, **k):
            raise self.core.KlatalkMembership("HTTP 403: forbidden")
        self.core.listen_core = gone
        self.core.get_room = lambda creds, rid: self.room
        self.run_async(self.adapter._room_loop(self.ROOM))
        self.assertIn(self.ROOM, self.adapter._stopped)
        self.assertFalse(self.adapter.has_fatal_error)

        async def auth(*a, **k):
            raise self.core.KlatalkAuth("HTTP 401: token")
        self.core.listen_core = auth
        self.adapter._stopped.discard(self.ROOM)
        self.run_async(self.adapter._room_loop(self.ROOM))
        self.assertTrue(self.adapter.has_fatal_error)

    def test_rows_landing_mid_turn_are_queued_and_merged_after_it(self):
        marks = []

        async def do_read(creds, room_id, seq):
            marks.append(seq); return seq
        self.core.do_read = do_read
        self.run_async(self.adapter._on_event(self.ROOM, self.message(self.OWNER, "first", seq=6)))
        self.assertEqual(len(self.handled), 1)                  # turn 1 opened
        self.run_async(self.adapter._on_event(self.ROOM, self.message(self.OTHER, "second", seq=7)))
        self.run_async(self.adapter._on_event(self.ROOM, self.message(self.OWNER, "third", seq=8)))
        self.assertEqual(len(self.handled), 1)                  # held, not a busy notice
        async def complete_and_settle():
            # the follow-up waits until Hermes drops the session from
            # _active_sessions — simulate a busy session that clears shortly
            key = self.adapter._session_key_for(self.handled[0])
            self.adapter._active_sessions[key] = object()
            await self.adapter.on_processing_complete(self.handled[0], ProcessingOutcome.SUCCESS)
            self.assertEqual(len(self.handled), 1)              # not yet — session still active
            await asyncio.sleep(0.3)
            self.assertEqual(len(self.handled), 1)
            self.adapter._active_sessions.pop(key)
            await asyncio.gather(*self.adapter._followups)
        self.run_async(complete_and_settle())
        self.assertEqual(marks, [6])                             # signed through what turn 1 saw
        self.assertEqual(len(self.handled), 2)                  # turn 2 = the two held rows, merged
        merged = self.handled[1]
        self.assertIn("[member] Guest·", merged.text)
        self.assertIn("[owner] 경훈·", merged.text)
        self.assertFalse(merged.allow_gateway_control)          # a member's line is in it
        self.assertEqual(merged.metadata["klatalk_merged"], 2)
        self.assertEqual(self.adapter._inflight[self.ROOM], 8)
        self.run_async(self.adapter.on_processing_complete(merged, ProcessingOutcome.FAILURE))
        self.assertNotIn(self.ROOM, self.adapter._busy)         # a failed turn frees the room too

    def test_sender_binding_failure_demotes_the_row(self):
        ev = self.message(self.OWNER, "do it", seq=6, sender_binding="failed")
        self.run_async(self.adapter._on_event(self.ROOM, ev))
        e = self.handled[0]
        self.assertTrue(e.text.startswith("[member · sender failed]"))
        self.assertFalse(e.allow_gateway_control)

    def test_private_notices_stay_out_of_the_room(self):
        seed = self.A._env_enablement()
        self.assertEqual(seed["notice_delivery"], "private")
        r = self.run_async(self.adapter.send_private_notice(self.ROOM, self.OWNER, "No home channel"))
        self.assertTrue(r.success)


class TestOutbound(AdapterBase):
    def test_send_chunks_quotes_once_and_never_moves_the_read_mark(self):
        sent = []

        async def send_message(creds, profile, room, payload=None, *, text=None, reply_to=None, read_through=None):
            sent.append((text, reply_to, read_through)); return 40 + len(sent)
        self.core.send_message = send_message
        r = self.run_async(self.adapter.send(self.ROOM, "x" * 9000, reply_to="6"))
        self.assertTrue(r.success)
        self.assertEqual(r.message_id, str(40 + len(sent)))
        self.assertGreaterEqual(len(sent), 3)
        self.assertEqual([s[1] for s in sent][0], 6)
        self.assertTrue(all(s[1] is None for s in sent[1:]))
        self.assertTrue(all(s[2] is None for s in sent))
        self.assertTrue(all(len(s[0]) <= 4000 for s in sent))

    def test_send_errors_are_typed_not_raised(self):
        async def boom(*a, **k):
            raise self.core.KlatalkMembership("HTTP 403: forbidden")
        self.core.send_message = boom
        r = self.run_async(self.adapter.send(self.ROOM, "hi"))
        self.assertFalse(r.success)
        self.assertEqual(r.error_kind, "forbidden")
        r = self.run_async(self.adapter.send("not-my-room", "hi"))
        self.assertFalse(r.success)

    def test_nothing_escapes_as_system_exit(self):
        async def die(*a, **k):
            raise SystemExit("a stray exit")            # must never reach the loop
        self.core.send_message = die
        with self.assertRaises(SystemExit):
            self.run_async(self.adapter.send(self.ROOM, "hi"))
        # …which is exactly why the core may not raise it: the AST test in
        # tests/test_klatalk.py is the real guard. Document the contract here.

    def test_toolsets_follow_room_and_sender(self):
        class Src:
            def __init__(self, chat_id, user_id):
                self.chat_id, self.user_id = chat_id, user_id
        self.adapter.settings.tool_rooms = {self.ROOM}
        self.assertEqual(self.adapter.toolsets_for_source(Src(self.ROOM, self.OWNER)), ["hermes-cli"])
        self.assertEqual(self.adapter.toolsets_for_source(Src(self.ROOM, self.OTHER)), ["safe"])
        self.adapter.settings.tool_rooms = set()
        self.assertEqual(self.adapter.toolsets_for_source(Src(self.ROOM, self.OWNER)), ["safe"])

    def test_delivery_targets_are_the_home_channel_only(self):
        # default home channel = the first room (no "/sethome" notice in the room)
        self.assertEqual(self.A.Settings().home_channel, self.ROOM)
        self.assertIsNone(self.A._validate_target_ref(self.ROOM))
        self.assertIsNotNone(self.A._validate_target_ref("other"))
        os.environ["KLATALK_ROOMS"] = f"{self.ROOM},second-room"
        os.environ["KLATALK_HOME_CHANNEL"] = "second-room"
        self.assertIsNone(self.A._validate_target_ref("second-room"))
        self.assertIsNotNone(self.A._validate_target_ref(self.ROOM))
        os.environ["KLATALK_ROOMS"] = self.ROOM
        os.environ["KLATALK_HOME_CHANNEL"] = self.ROOM
        res = self.run_async(self.A._standalone_send(self.cfg, "other", "hi"))
        self.assertIn("error", res)
        res = self.run_async(self.A._standalone_send(self.cfg, self.ROOM, "hi", media_files=["/x"]))
        self.assertIn("error", res)


if __name__ == "__main__":
    unittest.main()
