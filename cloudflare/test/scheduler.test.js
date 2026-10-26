import test from "node:test";
import assert from "node:assert/strict";
import { database } from "../test-support/database.js";
import { beijingTime, schedulerTick, schedulerStatus, dispatchDailyWorkflow } from "../src/scheduler.js";
import { commitRaceState, raceState, raceHealthResponse } from "../src/races.js";
import worker from "../src/index.js";

const at = time => new Date(`2026-10-27T${time}:00+08:00`);
function env() { return { DB: database(), SCHEDULER_ENABLED: "true", ADMIN_TOKEN: "admin",
  GITHUB_DISPATCH_TOKEN: "dispatch-secret", FEISHU_WEBHOOK_URL: "https://example.com/old",
  FEISHU_ALERTS_WEBHOOK_URL: "https://example.com/new", NOTIFICATION_QUEUE: { async send() {} } }; }
const accepted = async () => new Response(null, { status: 204 });
async function events(e) { return (await e.DB.prepare("SELECT * FROM monitor_events ORDER BY id").all()).results; }

test("Beijing midnight and 09:00 are independent of the UTC date; paused or early ticks never dispatch", async () => {
  assert.deepEqual(beijingTime(new Date("2026-10-26T16:00:00Z")), { date: "2026-10-27", time: "00:00", minutes: 0 });
  const e = env(); let calls = 0;
  const fetcher = async () => { calls++; return accepted(); };
  e.SCHEDULER_ENABLED = "false";
  assert.equal((await schedulerTick(e, at("09:00"), fetcher)).status, "disabled");
  e.SCHEDULER_ENABLED = "true";
  assert.equal((await schedulerTick(e, at("08:55"), fetcher)).status, "not_due");
  assert.equal((await schedulerTick(e, at("09:00"), fetcher)).status, "accepted");
  assert.equal(calls, 1);
  assert.equal((await events(e)).length, 0);
});

test("workflow dispatch only posts to the fixed repository, with no redirects or leaked upstream error", async () => {
  const e = env();
  await dispatchDailyWorkflow(e, async (url, options) => {
    assert.equal(url, "https://api.github.com/repos/biblioth/race-monitor/actions/workflows/race-monitor.yml/dispatches");
    assert.equal(options.redirect, "manual");
    assert.equal(options.headers.authorization, "Bearer dispatch-secret");
    assert.deepEqual(JSON.parse(options.body), { ref: "main", inputs: { trigger: "cloudflare" } });
    return accepted();
  });
  await assert.rejects(dispatchDailyWorkflow(e, async () => { throw new Error("private dispatch-secret"); }), /^Error: GitHub dispatch request failed or timed out$/);
  await assert.rejects(dispatchDailyWorkflow(e, async () => new Response("private dispatch-secret", { status: 403 })), /^Error: GitHub dispatch HTTP 403$/);
  await assert.rejects(dispatchDailyWorkflow(e, async () => new Response(null, { status: 302, headers: { location: "https://other.example/" } })), /^Error: GitHub dispatch HTTP 302$/);
  delete e.GITHUB_DISPATCH_TOKEN;
  await assert.rejects(dispatchDailyWorkflow(e), /credential is missing/);
});

test("overlapping cron invocations claim one dispatch, retry after 30 minutes, and stop at six attempts", async () => {
  const e = env(); let calls = 0;
  const fetcher = async () => { calls++; return accepted(); };
  await Promise.all([schedulerTick(e, at("09:00"), fetcher), schedulerTick(e, at("09:00"), fetcher)]);
  await schedulerTick(e, at("09:05"), fetcher);
  assert.equal(calls, 1);
  for (const time of ["09:30", "10:00", "10:30", "11:00", "11:30", "12:00", "18:00"]) {
    await schedulerTick(e, at(time), fetcher);
  }
  assert.equal(calls, 6);
  assert.equal((await schedulerStatus(e, at("18:00"))).attempts, 6);
  assert.equal((await events(e)).length, 1);
});

test("an active collector is left alone; its expired lease can be compensated", async () => {
  const e = env();
  await commitRaceState(e, { version: 0, state: { run: { started_at: at("08:58").toISOString() } }, events: [] });
  let calls = 0;
  const fetcher = async () => { calls++; return accepted(); };
  assert.equal((await schedulerTick(e, at("09:00"), fetcher)).status, "running");
  await schedulerTick(e, at("09:25"), fetcher);
  assert.equal(calls, 1);
});

test("a normal completed day creates no dispatch, alert, recovery message or race-state mutation", async () => {
  const e = env();
  const state = { last_run_date: "2026-10-27", last_completed_at: at("09:04").toISOString(), known: { untouched: {} } };
  await commitRaceState(e, { version: 0, state, events: [] });
  for (const time of ["09:05", "09:45", "12:00", "23:55"]) {
    assert.equal((await schedulerTick(e, at(time), async () => assert.fail("must not dispatch"))).status, "completed");
  }
  assert.equal((await events(e)).length, 0);
  assert.deepEqual(await raceState(e.DB), { version: 1, state });
});

test("09:45 sends one persistent overdue event to both robots; a late completion sends one recovery", async () => {
  const e = env();
  await schedulerTick(e, at("09:00"), accepted);
  await schedulerTick(e, at("09:30"), accepted);
  assert.equal((await events(e)).length, 0);
  await schedulerTick(e, at("09:45"), accepted);
  await schedulerTick(e, at("09:50"), accepted);
  let rows = await events(e);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].event_type, "race_schedule_overdue");
  assert.match(JSON.parse(rows[0].payload_json).message, /已尝试触发 2 次/);
  assert.equal((await e.DB.prepare("SELECT COUNT(*) AS n FROM monitor_deliveries").first()).n, 2);
  await commitRaceState(e, { version: 0, state: { last_run_date: "2026-10-27", last_completed_at: at("09:53").toISOString() }, events: [] });
  await schedulerTick(e, at("09:55"), async () => assert.fail("already completed"));
  await schedulerTick(e, at("10:00"), async () => assert.fail("already completed"));
  rows = await events(e);
  assert.deepEqual(rows.map(r => r.event_type), ["race_schedule_overdue", "race_schedule_recovered"]);
  assert.match(JSON.parse(rows[1].payload_json).message, /09:53/);
  assert.equal((await e.DB.prepare("SELECT COUNT(*) AS n FROM monitor_deliveries").first()).n, 4);
  assert.equal((await schedulerStatus(e, at("10:00"))).overdue_at, at("09:45").toISOString());
});

test("dispatch errors are recorded safely and produce overdue alerts without marking the race day completed", async () => {
  const e = env();
  await schedulerTick(e, at("09:45"), async () => new Response("dispatch-secret", { status: 401 }));
  const status = await schedulerStatus(e, at("09:45"));
  assert.equal(status.last_dispatch_status, "failed");
  assert.equal(status.last_dispatch_error, "GitHub dispatch HTTP 401");
  assert.ok(!JSON.stringify(status).includes("dispatch-secret"));
  assert.equal((await raceState(e.DB)).state.last_run_date, undefined);
  assert.equal((await events(e)).length, 1);
});

test("an outbox failure cannot consume the overdue alert", async () => {
  const e = env(); const batch = e.DB.batch;
  e.DB.batch = async () => { throw new Error("D1 unavailable"); };
  await assert.rejects(schedulerTick(e, at("09:45"), accepted), /D1 unavailable/);
  assert.equal((await schedulerStatus(e, at("09:45"))).overdue_at, null);
  e.DB.batch = batch;
  await schedulerTick(e, at("09:50"), accepted);
  assert.equal((await events(e)).length, 1);
});

test("completion during dispatch prevents a stale overdue alert", async () => {
  const e = env();
  await schedulerTick(e, at("09:45"), async () => {
    await commitRaceState(e, { version: 0, state: { last_run_date: "2026-10-27", last_completed_at: at("09:45").toISOString() }, events: [] });
    return accepted();
  });
  assert.equal((await events(e)).length, 0);
  assert.equal((await schedulerStatus(e, at("09:45"))).overdue_at, null);
});

test("daily schedule health fails at the morning deadline even when yesterday's result is less than 26 hours old", async () => {
  const e = env(); const realNow = Date.now;
  try {
    Date.now = () => at("09:40").getTime();
    await commitRaceState(e, { version: 0, state: { last_run_date: "2026-10-26", last_completed_at: "2026-10-26T13:00:00+08:00" }, events: [] });
    assert.equal((await raceHealthResponse(e)).status, 200);
    Date.now = () => at("09:45").getTime();
    const response = await raceHealthResponse(e);
    assert.equal(response.status, 503);
    assert.equal((await response.json()).scheduleOk, false);
  } finally { Date.now = realNow; }
});

test("manual dispatch requires the independent admin credential and cannot specify another workflow", async () => {
  const e = env();
  const request = token => new Request("https://worker.test/races/scheduler/dispatch", { method: "POST",
    headers: token ? { authorization: `Bearer ${token}` } : {}, body: JSON.stringify({ workflow: "other.yml" }) });
  assert.equal((await worker.fetch(request(), e)).status, 401);
  const oldFetch = globalThis.fetch;
  globalThis.fetch = async url => { assert.match(url, /race-monitor.yml\/dispatches$/); return accepted(); };
  try { assert.equal((await worker.fetch(request("admin"), e)).status, 200); }
  finally { globalThis.fetch = oldFetch; }
});

test("cron runs the queue sweeper even when scheduler storage fails", async () => {
  const e = env();
  await commitRaceState(e, { version: 0, state: {}, events: [{ key: "races:test:sweep", type: "race_digest", payload: { message: "test" } }] });
  const originalPrepare = e.DB.prepare; let sent = 0;
  e.NOTIFICATION_QUEUE.send = async () => { sent++; };
  e.DB.prepare = sql => { if (sql.startsWith("INSERT INTO race_scheduler_days")) throw new Error("scheduler failure"); return originalPrepare(sql); };
  let task;
  await worker.scheduled({}, e, { waitUntil(promise) { task = promise; } });
  await assert.rejects(task, /scheduler failure/);
  assert.equal(sent, 2);
});
