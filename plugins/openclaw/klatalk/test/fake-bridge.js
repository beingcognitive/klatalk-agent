// A stand-in for `klatalk bridge`: the same wire (one JSON object per
// line), scripted events from FAKE_EVENTS, every command appended to
// FAKE_LOG. Run by the plugin under test as `<python> <cli> bridge …`
// with python=node and cli=this file.
import { appendFileSync, writeFileSync } from "node:fs";
import { createInterface } from "node:readline";

const args = process.argv.slice(2);
const rooms = (args[args.indexOf("--rooms") + 1] || "").split(",");
const out = (o) => process.stdout.write(JSON.stringify(o) + "\n");
const logCmd = (o) => { if (process.env.FAKE_LOG) appendFileSync(process.env.FAKE_LOG, JSON.stringify(o) + "\n"); };
if (process.env.FAKE_ARGS) writeFileSync(process.env.FAKE_ARGS, JSON.stringify({ args, env: { KLATALK_PROFILE: process.env.KLATALK_PROFILE ?? null, KLATALK_HOME: process.env.KLATALK_HOME ?? null } }));

out({ ev: "hello", version: process.env.FAKE_VERSION ?? "1.5.0", profile: "p", user_id: "BOT", nickname: "Bot", rooms });
for (const r of rooms) out({ ev: "joined", room: r, sealed: false });

let seq = 100;
const rl = createInterface({ input: process.stdin });
rl.on("line", (line) => {
  const req = JSON.parse(line);
  logCmd(req);
  const { id, cmd } = req;
  if (cmd === "send") out({ id, ok: true, seq: ++seq });
  else if (cmd === "read") out({ id, ok: true, last_read_seq: req.seq });
  else if (cmd === "roster") {
    const roster = JSON.parse(process.env.FAKE_ROSTER ?? "{}")[req.room] ?? [];
    out({ id, ok: true, name: "Bench", sealed: false, members: roster.map((u) => ({ user_id: u, nick: u, is_ai: u === "BOT" })) });
  }
  else if (cmd === "fetch") { writeFileSync(req.out, "img"); out({ id, ok: true, path: req.out, bytes: 3 }); }
  else if (cmd === "attach") out({ id, ok: true, seq: ++seq });
  else out({ id, ok: false, kind: "usage", why: "unknown command" });
});
rl.on("close", () => process.exit(0));
setTimeout(() => {
  for (const ev of JSON.parse(process.env.FAKE_EVENTS ?? "[]")) out(ev);
  if (process.env.FAKE_EXIT_AFTER_EVENTS) setTimeout(() => process.exit(Number(process.env.FAKE_EXIT_AFTER_EVENTS)), 20);
}, 30);
