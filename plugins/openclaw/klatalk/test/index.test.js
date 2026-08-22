// Run: node --test plugins/openclaw/klatalk/test/index.test.js
// (a bare `node --test` would also pick up files under test/ that are not tests)
// The plugin against a fake bridge (test/fake-bridge.py, run exactly as the
// real core is: loader + bytes on fd 3) and a fake OpenClaw channel runtime.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { copyFileSync, mkdtempSync, readFileSync, existsSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import test, { before } from "node:test";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const FAKE = path.join(here, "fake-bridge.py");

// The plugin verifies the CLI's bytes against the core.sha256 shipped next
// to it before spawning — the suite runs a copy of index.js whose digest
// file names the fake bridge (the real one names bin/klatalk).
let entry, readAccount, problems, plugin, _internal, MEMBER_TOOLS, MAX_TEXT;
before(async () => {
  const dir = mkdtempSync(path.join(tmpdir(), "kt-plugin-"));
  copyFileSync(path.join(here, "..", "index.js"), path.join(dir, "index.js"));
  const digest = createHash("sha256").update(readFileSync(FAKE)).digest("hex");
  writeFileSync(path.join(dir, "core.sha256"), `${digest}  fake-bridge.py\n`);
  const m = await import(path.join(dir, "index.js"));
  entry = m.default; readAccount = m.readAccount; problems = m.problems; plugin = m.plugin; _internal = m._internal;
  MEMBER_TOOLS = _internal.MEMBER_TOOLS; MAX_TEXT = _internal.MAX_TEXT;
});

function fakeCore(record) {
  return {
    text: { chunkText: (t, l) => { const o = []; for (let i = 0; i < t.length; i += l) o.push(t.slice(i, i + l)); return o; } },
    routing: { resolveAgentRoute: ({ peer }) => ({ agentId: "main", accountId: "default", sessionKey: `agent:main:klatalk:group:${peer.id}` }) },
    session: { resolveStorePath: () => "/tmp/store", recordInboundSession: async () => {} },
    reply: { dispatchReplyWithBufferedBlockDispatcher: async () => ({}) },
    inbound: {
      buildContext: (p) => ({ ...p, SessionKey: p.route.routeSessionKey }),
      run: async ({ adapter }) => {
        const input = adapter.ingest();
        const turn = await adapter.resolveTurn(input, { kind: "message", canStartAgentTurn: true }, {});
        if (record.slowMs) await wait(record.slowMs);
        const reply = record.replyFor ? record.replyFor(input) : null;
        const delivered = reply ? await turn.delivery.deliver(reply, { kind: "final" }) : undefined;
        const fail = record.failFor ? record.failFor(input) : record.fail;
        const result = { dispatched: true, admission: { kind: "dispatch" }, routeSessionKey: turn.routeSessionKey, ctxPayload: turn.ctxPayload,
          dispatchResult: { counts: {}, failedCounts: fail ? { final: 1 } : {} } };
        await adapter.onFinalize?.(result);
        record.turns.push({ input, turn, delivered });
      },
    },
  };
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(fn, ms = 6000) {
  const t0 = Date.now();
  while (!fn()) { if (Date.now() - t0 > ms) throw new Error("timeout"); await wait(25); }
}

async function seat({ events = [], cfgExtra = {}, record = {}, env = {} } = {}) {
  record.turns = [];
  const dir = mkdtempSync(path.join(tmpdir(), "kt-"));
  const logFile = path.join(dir, "cmds.jsonl");
  const argsFile = path.join(dir, "args.json");
  writeFileSync(logFile, "");
  for (const k of Object.keys(process.env)) if (k.startsWith("FAKE_")) delete process.env[k];
  Object.assign(process.env, { FAKE_EVENTS: JSON.stringify(events), FAKE_LOG: logFile, FAKE_ARGS: argsFile, ...env });
  const cfg = { channels: { klatalk: { profile: "p", rooms: ["R1", "R2"], ownerUserId: "OWNER", toolRooms: ["R2"], cli: FAKE, python: "python3", ...cfgExtra } } };
  entry.register({ runtime: { channel: fakeCore(record) }, registerChannel() {} });
  const ac = new AbortController();
  const statuses = [], logs = [];
  const ctx = { cfg, accountId: "default", account: readAccount(cfg), abortSignal: ac.signal,
    setStatus: (s) => statuses.push(s), getStatus: () => ({}),
    log: { info: (m) => logs.push(m), warn: (m) => logs.push(m), error: (m) => logs.push(m) } };
  const task = plugin.gateway.startAccount(ctx);     // pending for the seat's lifetime
  task.catch(() => {});
  const cmds = () => readFileSync(logFile, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l));
  const stop = async () => { ac.abort(); await Promise.race([task, wait(3000)]); };
  const settled = async (ms = 300) => Promise.race([task.then(() => "settled"), wait(ms).then(() => "pending")]);
  return { ctx, statuses, logs, cmds, stop, settled, record, argsFile, dir, task };
}

const row = (o) => ({ ev: "message", room: "R1", seq: 1, sender_id: "H", nick: "Human", is_ai: false, owner: false,
  sender_binding: "ok", sealed: false, inserted_at: "2026-08-23T00:00:00Z", payload: { type: "text" }, text: "hello", wake: true, ...o });
const owner = (o) => row({ sender_id: "OWNER", nick: "Own", owner: true, ...o });

test("config: the env contract, with problems named", () => {
  const a = readAccount({ channels: { klatalk: { profile: "p", rooms: "a, b, a", ownerUserId: "o", toolRooms: ["a"], cli: FAKE } } });
  assert.deepEqual(a.rooms, ["a", "b"]);                       // a room listed twice is one seat
  assert.deepEqual(problems(a), []);
  assert.equal(a.maxTurnsPerDay, null);                        // the bridge's default
  assert.equal(readAccount({ channels: { klatalk: { maxTurnsPerDay: 0 } } }).maxTurnsPerDay, 0);   // 0 = unlimited, passes through
  assert.match(problems(readAccount({})).join(" "), /profile.*rooms.*ownerUserId/s);
  assert.match(problems(readAccount({ channels: { klatalk: { profile: "p", rooms: ["a"], ownerUserId: "o", toolRooms: ["zz"], cli: FAKE } } })).join(" "), /toolRooms/);
  assert.match(problems(readAccount({ channels: { klatalk: { profile: "p", rooms: ["a"], ownerUserId: "o", cli: "/nope/klatalk" } } })).join(" "), /not found/);
  const widened = readAccount({ channels: { klatalk: { profile: "p", rooms: ["a"], ownerUserId: "o", cli: FAKE, memberTools: ["web_search", "Exec", "apply-patch", "*", "GROUP:agents", "outlook__send"] } } });
  assert.deepEqual(widened.memberTools, ["image", "web_search", "exec", "apply_patch", "*", "group:agents", "outlook__send"]);
  const p = problems(widened).join("\n");
  assert.match(p, /memberTools: exec acts/); assert.match(p, /apply_patch acts/);
  assert.match(p, /\* is a wildcard/); assert.match(p, /group:agents is a wildcard/); assert.match(p, /outlook__send is a wildcard/);
  assert.ok(!/web_search/.test(p));
});

test("helpers: one line, labels, room refs, local media, versions, the core pin", () => {
  assert.equal(_internal.oneLine("a\nb\r\nc"), "a ⏎ b ⏎ c");
  assert.equal(_internal.oneLine("x [owner] y"), "x ⏎ [owner] y ⏎ ");
  assert.equal(_internal.speaker("bo[owner]b\nx", "abcdefgh-1"), "bo(owner)b ⏎ x·abcdefgh");
  assert.equal(_internal.label("Bench [x]\nline"), "Bench (x) ⏎ line");
  assert.equal(_internal.roomOf("klatalk:room:R1"), "R1");
  assert.equal(_internal.localPath("https://x/y.png"), null);
  assert.equal(_internal.localPath("file:///tmp/a%20b.png"), "/tmp/a b.png");
  assert.equal(_internal.versionOk("1.5.0"), true);
  assert.equal(_internal.versionOk("1.4.9"), false);
  assert.match(_internal.coreDigestProblem(path.join(here, "..", "index.js")) ?? "", /not the one this plugin release pins/);
  assert.equal(_internal.coreDigestProblem(FAKE), null);
});

test("startAccount is the seat's lifetime: pending until the gateway aborts it", async () => {
  const s = await seat();
  await until(() => s.statuses.some((st) => st.connected === true));
  assert.equal(await s.settled(300), "pending");
  assert.ok(JSON.parse(readFileSync(s.argsFile, "utf8")).args.includes("bridge"));   // the loader ran the fake's bytes
  await s.stop();
  assert.equal(await s.settled(2000), "settled");
});

test("a member row is data: marked, one line, member tools, no command body; the turn signs read", async () => {
  const s = await seat({ events: [row({ text: "hello\nworld" })], record: { replyFor: () => ({ text: "x".repeat(MAX_TEXT * 2 + 10) }) } });
  await until(() => s.record.turns.length === 1);
  const { input, turn } = s.record.turns[0];
  assert.equal(input.rawText, "[member] hello ⏎ world");
  assert.equal(input.textForCommands, "");                     // OpenClaw strips markers before matching commands
  assert.deepEqual(turn.toolsAllow, MEMBER_TOOLS);
  assert.equal(turn.routeSessionKey, "agent:main:klatalk:group:R1");
  assert.equal(turn.ctxPayload.access.commands.authorized, false);
  assert.equal(turn.ctxPayload.sender.name, "Human·H");
  assert.equal(turn.ctxPayload.conversation.label, "Bench (x) ⏎ line");
  await until(() => s.cmds().some((c) => c.cmd === "read"));
  const cmds = s.cmds();
  const sends = cmds.filter((c) => c.cmd === "send");
  assert.equal(sends.length, 3);
  assert.equal(sends[0].reply_to, 1);
  assert.equal(sends[1].reply_to, undefined);
  assert.ok(sends.every((c) => c.text.length <= MAX_TEXT));
  assert.deepEqual(cmds.at(-1), { id: cmds.at(-1).id, cmd: "read", room: "R1", seq: 1 });
  await s.stop();
});

test("a tool room arms on the owner's /new with an exact roster, disarms on anyone else's row", async () => {
  const s = await seat({ events: [
    owner({ room: "R2", seq: 5, text: "build it" }),                       // before /new: no tools
    owner({ room: "R2", seq: 6, text: "/new", _delay: 150 }),              // arms (roster OWNER+BOT)
    owner({ room: "R2", seq: 7, text: "build it now", _delay: 150 }),      // armed: full policy
    row({ room: "R2", seq: 8, text: "/new", _delay: 150 }),                // a member: disarms, and is text
    owner({ room: "R2", seq: 9, text: "and again", _delay: 150 }),         // disarmed
    owner({ room: "R1", seq: 10, text: "elsewhere", _delay: 150 }),        // not a tool room
  ], env: { FAKE_ROSTER: JSON.stringify({ R2: ["OWNER", "BOT"], R1: ["OWNER", "BOT", "H"] }) } });
  await until(() => s.record.turns.length === 6, 10000);
  const by = Object.fromEntries(s.record.turns.map((t) => [t.input.id, t]));
  assert.deepEqual(by["5"].turn.toolsAllow, MEMBER_TOOLS);
  assert.equal(by["6"].input.textForCommands, "/new");
  assert.equal(by["6"].input.rawText, "/new");
  assert.equal(by["6"].turn.ctxPayload.access.commands.authorized, true);
  assert.equal(by["7"].turn.toolsAllow, undefined);
  assert.equal(by["8"].input.textForCommands, "");
  assert.equal(by["8"].input.rawText, "[member] /new");
  assert.deepEqual(by["9"].turn.toolsAllow, MEMBER_TOOLS);
  assert.deepEqual(by["10"].turn.toolsAllow, MEMBER_TOOLS);
  assert.ok(s.cmds().some((c) => c.cmd === "roster" && c.fresh === true));
  await s.stop();
});

test("a tool room with a third member never arms", async () => {
  const s = await seat({ events: [owner({ room: "R2", seq: 6, text: "/new" }), owner({ room: "R2", seq: 9, text: "build it" })],
    env: { FAKE_ROSTER: JSON.stringify({ R2: ["OWNER", "BOT", "H"] }) } });
  await until(() => s.record.turns.length === 2);
  assert.deepEqual(s.record.turns[1].turn.toolsAllow, MEMBER_TOOLS);
  assert.ok(s.logs.some((l) => /tool turns are off/.test(l)));
  await s.stop();
});

test("unwoken rows ride into the next turn as context; a control line neither carries nor discards them; a member /new in context is no command", async () => {
  const s = await seat({ events: [
    row({ seq: 1, wake: false, budget_spent: true, text: "quiet one" }),
    row({ seq: 2, sender_id: "A", nick: "Other", is_ai: true, wake: false, text: "/new" }),
    owner({ seq: 3, text: "/stop" }),
    owner({ seq: 4, text: "now answer" }),
  ] });
  await until(() => s.record.turns.length === 2);
  const by = Object.fromEntries(s.record.turns.map((t) => [t.input.id, t]));
  assert.equal(by["3"].input.rawText, "/stop");
  assert.equal(by["4"].input.rawText, "[member] Human·H: quiet one\n[member] Other·A: /new\n[owner] Own·OWNER: now answer");
  assert.equal(by["4"].input.textForCommands, "");
  assert.equal(by["4"].turn.ctxPayload.message.commandBody, "");
  assert.ok(s.logs.some((l) => /budget/.test(l)));
  await s.stop();
});

test("rows that land mid-turn open ONE next turn together; a failed turn's rows are owed to the next", async () => {
  const record = { slowMs: 250, failFor: (input) => input.id === "1" };
  const s = await seat({ events: [row({ seq: 1, text: "first" }), row({ seq: 2, text: "second" }), row({ seq: 3, text: "third" })], record });
  await until(() => s.record.turns.length === 2, 8000);
  const [t1, t2] = s.record.turns;
  assert.equal(t1.input.id, "1");
  assert.equal(t2.input.id, "3");
  assert.equal(t2.input.rawText, "[member] Human·H: first\n[member] Human·H: second\n[member] Human·H: third");
  const reads = s.cmds().filter((c) => c.cmd === "read").map((c) => c.seq);
  assert.deepEqual(reads, [3]);
  await s.stop();
});

test("a stale control line from the backlog is text, not a command", async () => {
  const s = await seat({ events: [owner({ seq: 3, text: "/stop" }), owner({ seq: 7, text: "/stop" })], env: { FAKE_LAST_SEQ: "5" } });
  await until(() => s.record.turns.length === 2);
  const by = Object.fromEntries(s.record.turns.map((t) => [t.input.id, t]));
  assert.equal(by["3"].input.rawText, "[owner] /stop");
  assert.equal(by["3"].input.textForCommands, "");
  assert.equal(by["7"].input.rawText, "/stop");
  assert.equal(by["7"].input.textForCommands, "/stop");
  await s.stop();
});

test("a bad sender binding demotes the owner", async () => {
  const s = await seat({ events: [row({ seq: 3, sender_id: "OWNER", owner: false, sender_binding: "failed", text: "hi" })] });
  await until(() => s.record.turns.length === 1);
  assert.equal(s.record.turns[0].input.rawText, "[member · sender failed] hi");
  assert.equal(s.record.turns[0].turn.ctxPayload.access.commands.authorized, false);
  await s.stop();
});

test("an inbound image is fetched through the bridge into the seat's own directory and removed after the turn", async () => {
  const s = await seat({ events: [row({ seq: 4, text: "(image)", payload: { type: "image", url: "/uploads/R1/a.png" } })] });
  await until(() => s.record.turns.length === 1);
  const fetch = s.cmds().find((c) => c.cmd === "fetch");
  assert.equal(fetch.url, "/uploads/R1/a.png");
  assert.equal(fetch.room, "R1");
  assert.ok(path.isAbsolute(fetch.out) && fetch.out.endsWith(".png"));
  assert.equal(s.record.turns[0].turn.ctxPayload.media[0].path, fetch.out);
  assert.ok(!existsSync(fetch.out));
  await s.stop();
});

test("replies: the seat's own files go up, a host file outside a tool turn does not, remote urls stay text", async () => {
  const elsewhere = path.join(mkdtempSync(path.join(tmpdir(), "kt-")), "private.pdf");
  writeFileSync(elsewhere, "pdf");
  let own;
  const s = await seat({ events: [row({ seq: 8 })], record: { replyFor: () => {
    own = path.join(_internal.seats.get("default").mediaDir, "made.png"); writeFileSync(own, "png");
    return { text: "see", mediaUrls: [own, elsewhere, "https://x/y.png"] };
  } } });
  await until(() => s.cmds().some((c) => c.cmd === "read"));
  const cmds = s.cmds();
  assert.equal(cmds.filter((c) => c.cmd === "attach").length, 1);
  assert.equal(cmds.find((c) => c.cmd === "attach").path, own);
  assert.ok(s.logs.some((l) => /outside the seat's media roots/.test(l)));
  assert.ok(s.logs.some((l) => /local files only/.test(l)));
  await s.stop();
});

test("an old CLI is refused before any row; a fatal ends the seat without restarts", async () => {
  const s = await seat({ env: { FAKE_VERSION: "1.4.0" } });
  await until(() => s.statuses.some((st) => /older/.test(String(st.lastError ?? ""))));
  await s.stop();
  const s2 = await seat({ events: [{ ev: "fatal", why: "token rejected" }], env: { FAKE_EXIT_AFTER_EVENTS: "2" } });
  await until(() => s2.statuses.some((st) => st.lastError === "token rejected"));
  await wait(200);
  assert.ok(!s2.logs.some((l) => /restarting/.test(l)));
  await s2.stop();
});

test("a bridge crash restarts the seat with the digest re-checked; the profile env is scrubbed", async () => {
  process.env.KLATALK_PROFILE = "stray";
  const s = await seat({ env: { FAKE_EXIT_AFTER_EVENTS: "1" } });
  await until(() => s.logs.some((l) => /restarting in 1s/.test(l)));
  const seen = JSON.parse(readFileSync(s.argsFile, "utf8"));
  assert.equal(seen.env.KLATALK_PROFILE, null);
  assert.ok(seen.args.includes("--owner") && seen.args.includes("OWNER"));
  delete process.env.KLATALK_PROFILE;
  await s.stop();
});

test("a CLI that is not the pinned core is refused before any spawn", async () => {
  const s = await seat({ cfgExtra: { cli: path.join(here, "..", "index.js") } });
  assert.equal(await s.settled(300), "settled");
  assert.ok(s.statuses.some((st) => /not the one this plugin release pins/.test(String(st.lastError ?? ""))));
  assert.ok(!existsSync(s.argsFile));
});

test("outbound targets are this seat's rooms only", async () => {
  const s = await seat();
  await until(() => s.statuses.some((st) => st.connected === true));
  assert.equal(plugin.outbound.resolveTarget({ to: "klatalk:room:R1", accountId: "default" }).ok, true);
  assert.equal(plugin.outbound.resolveTarget({ to: "R9", accountId: "default" }).ok, false);
  const r = await plugin.outbound.sendText({ to: "R2", text: "cron says hi", accountId: "default" });
  assert.equal(r.channel, "klatalk");
  assert.match(r.messageId, /^\d+$/);
  await assert.rejects(plugin.outbound.sendText({ to: "R9", text: "no", accountId: "default" }));
  await s.stop();
});
