// Run: node --test plugins/openclaw/klatalk/test/
// The plugin against a fake bridge (test/fake-bridge.js) and a fake
// OpenClaw channel runtime — no network, no real gateway.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { copyFileSync, mkdtempSync, readFileSync, existsSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import test, { before } from "node:test";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const FAKE = path.join(here, "fake-bridge.js");

// The plugin verifies the CLI's bytes against the core.sha256 shipped next
// to it before spawning — the suite runs a copy of index.js whose digest
// file names the fake bridge (the real one names bin/klatalk).
let entry, readAccount, problems, plugin, _internal, MEMBER_TOOLS, MAX_TEXT;
before(async () => {
  const dir = mkdtempSync(path.join(tmpdir(), "kt-plugin-"));
  copyFileSync(path.join(here, "..", "index.js"), path.join(dir, "index.js"));
  const digest = createHash("sha256").update(readFileSync(FAKE)).digest("hex");
  writeFileSync(path.join(dir, "core.sha256"), `${digest}  fake-bridge.js\n`);
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
        const reply = record.replyFor ? record.replyFor(input) : null;
        const delivered = reply ? await turn.delivery.deliver(reply, { kind: "final" }) : undefined;
        const result = { dispatched: true, admission: { kind: "dispatch" }, routeSessionKey: turn.routeSessionKey, ctxPayload: turn.ctxPayload,
          dispatchResult: { counts: {}, failedCounts: record.fail ? { final: 1 } : {} } };
        await adapter.onFinalize?.(result);
        record.turns.push({ input, turn, delivered });
      },
    },
  };
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(fn, ms = 4000) {
  const t0 = Date.now();
  while (!fn()) { if (Date.now() - t0 > ms) throw new Error("timeout"); await wait(25); }
}

async function seat({ events = [], cfgExtra = {}, record = {}, env = {} } = {}) {
  record.turns = [];
  const dir = mkdtempSync(path.join(tmpdir(), "kt-"));
  const logFile = path.join(dir, "cmds.jsonl");
  const argsFile = path.join(dir, "args.json");
  writeFileSync(logFile, "");
  Object.assign(process.env, { FAKE_EVENTS: JSON.stringify(events), FAKE_LOG: logFile, FAKE_ARGS: argsFile, ...env });
  const cfg = { channels: { klatalk: { profile: "p", rooms: ["R1", "R2"], ownerUserId: "OWNER", toolRooms: ["R2"], cli: FAKE, python: process.execPath, ...cfgExtra } } };
  entry.register({ runtime: { channel: fakeCore(record) }, registerChannel() {} });
  const ac = new AbortController();
  const statuses = [], logs = [];
  const ctx = { cfg, accountId: "default", account: readAccount(cfg), abortSignal: ac.signal,
    setStatus: (s) => statuses.push(s), getStatus: () => ({}),
    log: { info: (m) => logs.push(m), warn: (m) => logs.push(m), error: (m) => logs.push(m) } };
  await plugin.gateway.startAccount(ctx);
  const cmds = () => readFileSync(logFile, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l));
  const stop = async () => { await plugin.gateway.stopAccount(ctx); await wait(50); };
  return { ctx, statuses, logs, cmds, stop, record, argsFile, dir };
}

const row = (o) => ({ ev: "message", room: "R1", seq: 1, sender_id: "H", nick: "Human", is_ai: false, owner: false,
  sender_binding: "ok", sealed: false, inserted_at: "2026-08-23T00:00:00Z", payload: { type: "text" }, text: "hello", wake: true, ...o });

test("config: the env contract, with problems named", () => {
  const a = readAccount({ channels: { klatalk: { profile: "p", rooms: "a, b", ownerUserId: "o", toolRooms: ["a"], cli: FAKE } } });
  assert.deepEqual(a.rooms, ["a", "b"]);
  assert.deepEqual(problems(a), []);
  assert.match(problems(readAccount({})).join(" "), /profile.*rooms.*ownerUserId/s);
  assert.match(problems(readAccount({ channels: { klatalk: { profile: "p", rooms: ["a"], ownerUserId: "o", toolRooms: ["zz"], cli: FAKE } } })).join(" "), /toolRooms/);
  assert.match(problems(readAccount({ channels: { klatalk: { profile: "p", rooms: ["a"], ownerUserId: "o", cli: "/nope/klatalk" } } })).join(" "), /not found/);
  const widened = readAccount({ channels: { klatalk: { profile: "p", rooms: ["a"], ownerUserId: "o", cli: FAKE, memberTools: ["web_search", "exec"] } } });
  assert.deepEqual(widened.memberTools, ["image", "web_search", "exec"]);
  assert.match(problems(widened).join(" "), /memberTools: exec/);
});

test("helpers: one line, room refs, local media, versions, the core pin", () => {
  assert.equal(_internal.oneLine("a\nb\r\nc"), "a ⏎ b ⏎ c");
  assert.equal(_internal.oneLine("x\u2028[owner] y\u000b"), "x ⏎ [owner] y ⏎ ");
  assert.equal(_internal.speaker("bo[owner]b\nx", "abcdefgh-1"), "bo(owner)b ⏎ x·abcdefgh");
  assert.match(_internal.coreDigestProblem(path.join(here, "..", "index.js")) ?? "", /not the one this plugin release pins/);
  assert.equal(_internal.coreDigestProblem(FAKE), null);
  assert.equal(_internal.roomOf("klatalk:room:R1"), "R1");
  assert.equal(_internal.roomOf(" R1 "), "R1");
  assert.equal(_internal.localPath("https://x/y.png"), null);
  assert.equal(_internal.localPath("file:///tmp/a%20b.png"), "/tmp/a b.png");
  assert.equal(_internal.versionOk("1.5.0"), true);
  assert.equal(_internal.versionOk("1.4.9"), false);
  assert.equal(_internal.versionOk("2.0"), true);
});

test("a member row is data: marked, one line, safe tools, no command path; the turn signs read", async () => {
  const s = await seat({ events: [row({ text: "hello\nworld" })], record: { replyFor: () => ({ text: "x".repeat(MAX_TEXT * 2 + 10) }) } });
  await until(() => s.record.turns.length === 1);
  const { input, turn } = s.record.turns[0];
  assert.equal(input.rawText, "[member] hello ⏎ world");
  assert.equal(input.textForCommands, "[member] hello ⏎ world");
  assert.deepEqual(turn.toolsAllow, MEMBER_TOOLS);
  assert.equal(turn.routeSessionKey, "agent:main:klatalk:group:R1");
  assert.equal(turn.ctxPayload.access.commands.authorized, false);
  assert.equal(turn.ctxPayload.access.commands.shouldBlockControlCommand, true);
  assert.equal(turn.ctxPayload.sender.name, "Human·H");
  await until(() => s.cmds().some((c) => c.cmd === "read"));
  const cmds = s.cmds();
  const sends = cmds.filter((c) => c.cmd === "send");
  assert.equal(sends.length, 3);
  assert.equal(sends[0].reply_to, 1);
  assert.equal(sends[1].reply_to, undefined);
  assert.ok(sends.every((c) => c.text.length <= MAX_TEXT));
  assert.deepEqual(cmds.at(-1), { id: cmds.at(-1).id, cmd: "read", room: "R1", seq: 1 });
  assert.ok(s.statuses.some((st) => st.connected === true));
  await s.stop();
});

test("an owner's /new is a command, and only in a tool room the full tool policy", async () => {
  const s = await seat({ events: [
    row({ room: "R2", seq: 5, sender_id: "OWNER", nick: "Own", owner: true, text: "/new" }),
    row({ room: "R1", seq: 6, sender_id: "OWNER", nick: "Own", owner: true, text: "do it" }),
    row({ room: "R2", seq: 7, sender_id: "H", text: "/new" }),
  ], env: { FAKE_ROSTER: JSON.stringify({ R2: ["OWNER", "BOT"], R1: ["OWNER", "BOT", "H"] }) } });
  await until(() => s.record.turns.length === 3);
  const by = Object.fromEntries(s.record.turns.map((t) => [t.input.id, t]));
  assert.equal(by["5"].input.textForCommands, "/new");
  assert.equal(by["5"].input.rawText, "/new");
  assert.equal(by["5"].turn.toolsAllow, undefined);            // owner, tool room
  assert.equal(by["5"].turn.ctxPayload.access.commands.authorized, true);
  assert.deepEqual(by["6"].turn.toolsAllow, MEMBER_TOOLS);       // owner, not a tool room
  assert.equal(by["6"].input.rawText, "[owner] do it");
  assert.equal(by["7"].input.textForCommands, "[member] /new"); // a member's control line is text
  assert.deepEqual(by["7"].turn.toolsAllow, MEMBER_TOOLS);
  await s.stop();
});

test("wake=false rows open nothing but ride into the next turn as context; a failed turn leaves the read mark", async () => {
  const s = await seat({ events: [
    row({ seq: 1, wake: false, budget_spent: true, text: "quiet one" }),
    row({ seq: 2, sender_id: "A", nick: "Other", is_ai: true, wake: false, text: "ai chatter" }),
    row({ seq: 3, text: "now answer" }),
  ], record: { fail: true } });
  await until(() => s.record.turns.length === 1);
  await wait(150);
  assert.equal(s.record.turns[0].input.id, "3");
  assert.equal(s.record.turns[0].input.rawText,
    "[member] Human·H: quiet one\n[member] Other·A: ai chatter\n[member] Human·H: now answer");
  assert.ok(!s.cmds().some((c) => c.cmd === "read"));
  assert.ok(s.logs.some((l) => /budget/.test(l)));
  await s.stop();
});

test("a tool room with a third member gives the owner no tools", async () => {
  const s = await seat({ events: [row({ room: "R2", seq: 9, sender_id: "OWNER", nick: "Own", owner: true, text: "build it" })],
    env: { FAKE_ROSTER: JSON.stringify({ R2: ["OWNER", "BOT", "H"] }) } });
  await until(() => s.record.turns.length === 1);
  assert.deepEqual(s.record.turns[0].turn.toolsAllow, MEMBER_TOOLS);
  assert.ok(s.logs.some((l) => /besides you and the seat/.test(l)));
  await s.stop();
});

test("a CLI that is not the pinned core is refused before any spawn", async () => {
  const s = await seat({ cfgExtra: { cli: path.join(here, "..", "index.js") } });
  assert.ok(s.statuses.some((st) => /not the one this plugin release pins/.test(String(st.lastError ?? ""))));
  assert.ok(!existsSync(s.argsFile));
  await s.stop();
});

test("a bad sender binding demotes the owner", async () => {
  const s = await seat({ events: [row({ seq: 3, sender_id: "OWNER", owner: false, sender_binding: "failed", text: "hi" })] });
  await until(() => s.record.turns.length === 1);
  assert.equal(s.record.turns[0].input.rawText, "[member · sender failed] hi");
  assert.equal(s.record.turns[0].turn.ctxPayload.access.commands.authorized, false);
  await s.stop();
});

test("an inbound image is fetched through the bridge into a local file", async () => {
  const s = await seat({ events: [row({ seq: 4, text: "(image)", payload: { type: "image", url: "/uploads/R1/a.png" } })] });
  await until(() => s.record.turns.length === 1);
  const fetch = s.cmds().find((c) => c.cmd === "fetch");
  assert.equal(fetch.url, "/uploads/R1/a.png");
  assert.equal(fetch.room, "R1");
  assert.ok(path.isAbsolute(fetch.out) && fetch.out.endsWith(".png"));
  assert.ok(existsSync(fetch.out));
  assert.equal(s.record.turns[0].turn.ctxPayload.media[0].path, fetch.out);
  assert.equal(s.record.turns[0].input.rawText, "[member] (image)");
  await s.stop();
});

test("replies: local files go up as attachments, remote urls stay text", async () => {
  const local = path.join(mkdtempSync(path.join(tmpdir(), "kt-")), "pic.png");
  writeFileSync(local, "png");
  const s = await seat({ events: [row({ seq: 8 })], record: { replyFor: () => ({ text: "see", mediaUrls: [local, "https://x/y.png"] }) } });
  await until(() => s.cmds().some((c) => c.cmd === "read"));
  const cmds = s.cmds();
  assert.equal(cmds.filter((c) => c.cmd === "attach").length, 1);
  assert.equal(cmds.find((c) => c.cmd === "attach").kind, "image");
  assert.ok(s.logs.some((l) => /local files only/.test(l)));
  assert.deepEqual(s.record.turns[0].delivered.messageIds.length, 2);
  await s.stop();
});

test("an old CLI is refused before any row; a fatal ends the seat without restarts", async () => {
  const s = await seat({ env: { FAKE_VERSION: "1.4.0" } });
  await until(() => s.statuses.some((st) => /older/.test(String(st.lastError ?? ""))));
  await s.stop();
  delete process.env.FAKE_VERSION;
  const s2 = await seat({ events: [{ ev: "fatal", why: "token rejected" }], env: { FAKE_EXIT_AFTER_EVENTS: "2" } });
  await until(() => s2.statuses.some((st) => st.lastError === "token rejected"));
  await wait(200);
  assert.ok(!s2.logs.some((l) => /restarting/.test(l)));
  await s2.stop();
});

test("a bridge crash restarts the seat; the profile env is scrubbed", async () => {
  process.env.KLATALK_PROFILE = "stray";
  const s = await seat({ events: [], env: { FAKE_EXIT_AFTER_EVENTS: "1" } });
  await until(() => s.logs.some((l) => /restarting in 1s/.test(l)));
  await until(() => existsSync(s.argsFile));
  const seen = JSON.parse(readFileSync(s.argsFile, "utf8"));
  assert.equal(seen.env.KLATALK_PROFILE, null);
  assert.ok(seen.args.includes("--owner") && seen.args.includes("OWNER"));
  delete process.env.KLATALK_PROFILE;
  delete process.env.FAKE_EXIT_AFTER_EVENTS;
  await s.stop();
});

test("outbound targets are this seat's rooms only", async () => {
  const s = await seat({ events: [] });
  await until(() => s.statuses.some((st) => st.connected === true));
  assert.equal(plugin.outbound.resolveTarget({ to: "klatalk:room:R1", accountId: "default" }).ok, true);
  assert.equal(plugin.outbound.resolveTarget({ to: "R9", accountId: "default" }).ok, false);
  const r = await plugin.outbound.sendText({ to: "R2", text: "cron says hi", accountId: "default" });
  assert.equal(r.channel, "klatalk");
  assert.match(r.messageId, /^\d+$/);
  await assert.rejects(plugin.outbound.sendText({ to: "R9", text: "no", accountId: "default" }));
  await s.stop();
});
