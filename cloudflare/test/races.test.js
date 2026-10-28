import test from "node:test";
import assert from "node:assert/strict";
import { database } from "../test-support/database.js";
import worker from "../src/index.js";
import { raceState, commitRaceState, raceHealthResponse, raceSourceResponse } from "../src/races.js";
import { renderEvent, sendEvent } from "../src/notifications.js";

const event = { key: "races:digest:sample", type: "race_digest", payload: { message: "🏃 新增活动\n分类：越野跑UTMB\n官方原文：https://utmb.world/" } };
function env() { return { DB: database(), FEISHU_WEBHOOK_URL: "https://example.com/bot", ADMIN_TOKEN: "test", NOTIFICATION_QUEUE: { async send() {} } }; }

test("race state and Feishu outbox are committed atomically; retry cannot duplicate delivery", async () => {
  const e = env();
  assert.deepEqual(await raceState(e.DB), { version: 0, state: {} });
  assert.equal((await commitRaceState(e, { version: 0, state: { known: { one: {} } }, events: [event] })).version, 1);
  assert.deepEqual(await commitRaceState(e, { version: 0, state: {}, events: [{ ...event, key: "races:digest:losing-writer" }] }), { conflict: true });
  assert.equal(Object.keys((await raceState(e.DB)).state.known).length, 1);
  await commitRaceState(e, { version: 1, state: { known: { one: {} } }, events: [event] });
  assert.equal((await e.DB.prepare("SELECT COUNT(*) AS n FROM monitor_events").first()).n, 1);
  const deliveries = await e.DB.prepare("SELECT channel FROM monitor_deliveries").all();
  assert.deepEqual(deliveries.results.map(x => x.channel), ["feishu"]);
});

test("missing Feishu configuration must not consume a discovery", async () => {
  const e = env();
  delete e.FEISHU_WEBHOOK_URL;
  await assert.rejects(commitRaceState(e, { version: 0, state: {}, events: [event] }), /Feishu/);
  assert.equal((await raceState(e.DB)).version, 0);
});

test("race endpoints require the dedicated admin credential", async () => {
  for (const path of ["/races/state", "/races/commit", "/races/source"]) {
    const r = await worker.fetch(new Request("https://worker.test" + path, { method: path.endsWith("commit") ? "POST" : "GET" }), env());
    assert.equal(r.status, 401);
  }
});

test("public-source fallback only fetches fixed URLs and never forwards admin credentials", async () => {
  let options;
  const response = await raceSourceResponse(new Request("https://worker.test/races/source?name=hk_home", { headers: { authorization: "Bearer private", cookie: "private" } }), async (url, opts) => {
    assert.equal(url, "https://hk100ultra.com/"); options = opts; return new Response("<html>race</html>");
  });
  assert.equal(response.status, 200);
  assert.equal(options.headers.authorization, undefined);
  assert.equal(options.headers.cookie, undefined);
  assert.equal((await raceSourceResponse(new Request("https://worker.test/races/source?name=http://127.0.0.1"))).status, 400);
});

test("a malformed race payload cannot create an event or mutate D1", async () => {
  const e = env();
  const request = new Request("https://worker.test/races/commit", { method: "POST", headers: { authorization: "Bearer test" }, body: JSON.stringify({ version: 0, state: {}, events: [{ ...event, type: "availability" }] }) });
  assert.equal((await worker.fetch(request, e)).status, 400);
  assert.equal((await raceState(e.DB)).version, 0);
});

test("daily health does not inherit the old three-minute freshness threshold", async () => {
  const e = env();
  const state = { last_completed_at: new Date(Date.now() - 23 * 3600000).toISOString(), sources: { official: { ok: true } }, known: {} };
  await commitRaceState(e, { version: 0, state, events: [] });
  assert.equal((await raceHealthResponse(e)).status, 200);
  await commitRaceState(e, { version: 1, state: { ...state, last_completed_at: new Date(Date.now() - 27 * 3600000).toISOString() }, events: [] });
  assert.equal((await raceHealthResponse(e)).status, 503);
});

test("Feishu keeps the exact Chinese category and does not add old reminder sections", async () => {
  const message = renderEvent(event).message;
  assert.match(message, /分类：越野跑UTMB/);
  assert.doesNotMatch(message, /报名变化|即将截止/);
  let posted;
  await sendEvent(env(), event, "feishu", async (url, options) => { posted = { url, payload: JSON.parse(options.body) }; return Response.json({ code: 0 }); });
  assert.equal(posted.url, "https://example.com/bot");
  assert.equal(posted.payload.content.text, message);
});

test("HTTP and Feishu business failures remain retryable", async () => {
  await assert.rejects(sendEvent(env(), event, "feishu", async () => Response.json({ code: 19024 })), /rejected/);
  await assert.rejects(sendEvent(env(), event, "feishu", async () => new Response("", { status: 503 })), /503/);
  await assert.rejects(sendEvent(env(), event, "feishu", async () => Response.json({})), /missing status/);
});

test("the standalone commit endpoint queues once and delivered queue replays are acknowledged", async () => {
  const e = env();
  const sent = [];
  e.NOTIFICATION_QUEUE.send = async body => sent.push(body);
  const request = () => new Request("https://worker.test/races/commit", { method: "POST", headers: { authorization: "Bearer test" }, body: JSON.stringify({ version: 0, state: { known: { one: {} } }, events: [event] }) });
  assert.equal((await worker.fetch(request(), e)).status, 200);
  assert.equal(sent.length, 1);
  assert.equal((await worker.fetch(request(), e)).status, 409);
  const previousFetch = globalThis.fetch;
  let posts = 0, acknowledged = 0;
  globalThis.fetch = async () => { posts++; return Response.json({ code: 0 }); };
  try {
    const message = { body: sent[0], ack() { acknowledged++; }, retry() { assert.fail("unexpected retry"); } };
    await worker.queue({ messages: [message] }, e);
    await worker.queue({ messages: [message] }, e);
    assert.equal(posts, 1);
    assert.equal(acknowledged, 2);
    assert.equal((await e.DB.prepare("SELECT status FROM monitor_deliveries").first()).status, "delivered");
  } finally { globalThis.fetch = previousFetch; }
});

test("a failed standalone Feishu delivery remains pending and retries", async () => {
  const e = env();
  await commitRaceState(e, { version: 0, state: {}, events: [event] });
  const delivery = await e.DB.prepare("SELECT id FROM monitor_deliveries").first();
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response("unavailable", { status: 503 });
  let retry;
  try {
    await worker.queue({ messages: [{ body: { deliveryId: delivery.id }, ack() { assert.fail("must not acknowledge failure"); }, retry(options) { retry = options; } }] }, e);
    assert.ok(retry.delaySeconds > 0);
    const row = await e.DB.prepare("SELECT status, attempts FROM monitor_deliveries").first();
    assert.equal(row.status, "pending");
    assert.equal(row.attempts, 1);
  } finally { globalThis.fetch = previousFetch; }
});

test("the standalone service exposes only race routes and Feishu delivery", async () => {
  const e = env();
  for (const path of ["/check", "/chairman/check", "/ocean/check", "/weekly-report"]) {
    assert.equal((await worker.fetch(new Request("https://worker.test" + path, { method: "POST", headers: { authorization: "Bearer test" } }), e)).status, 404);
  }
  await assert.rejects(sendEvent(e, event, "pushplus"), /Unsupported/);
  assert.equal((await worker.fetch(new Request("https://worker.test/"), e)).status, 200);
});

test("both robots receive independent deliveries and one failure never resends the successful robot", async () => {
  const e = env();
  e.FEISHU_ALERTS_WEBHOOK_URL = "https://example.com/new-group";
  const sent = [];
  e.NOTIFICATION_QUEUE.send = async body => sent.push(body);
  await commitRaceState(e, { version: 0, state: { known: { one: {} } }, events: [event] });
  const { enqueuePending } = await import("../src/index.js");
  assert.equal(await enqueuePending(e), 2);
  const previousFetch = globalThis.fetch;
  const calls = [];
  let recovered = false, retries = 0;
  globalThis.fetch = async url => { calls.push(url); return Response.json({ code: url.endsWith("new-group") && !recovered ? 19024 : 0 }); };
  const message = body => ({ body, ack() {}, retry() { retries++; } });
  try {
    await worker.queue({ messages: sent.map(message) }, e);
    const rows = (await e.DB.prepare("SELECT id,channel,status FROM monitor_deliveries ORDER BY id").all()).results;
    assert.equal(rows.find(r => r.channel === "feishu").status, "delivered");
    assert.equal(rows.find(r => r.channel === "feishu:alerts").status, "pending");
    assert.equal(retries, 1);
    recovered = true;
    await worker.queue({ messages: [message({ deliveryId: rows.find(r => r.channel === "feishu:alerts").id })] }, e);
    await worker.queue({ messages: sent.map(message) }, e);
    assert.equal(calls.filter(u => u === e.FEISHU_WEBHOOK_URL).length, 1);
    assert.equal(calls.filter(u => u === e.FEISHU_ALERTS_WEBHOOK_URL).length, 2);
    assert.equal((await e.DB.prepare("SELECT COUNT(*) AS n FROM monitor_deliveries WHERE status='delivered'").first()).n, 2);
  } finally { globalThis.fetch = previousFetch; }
});

test("each robot uses its own URL and signing secret; duplicate URLs do not double-send", async () => {
  const { configuredChannels } = await import("../src/notifications.js");
  const { createHmac } = await import("node:crypto");
  const e = { ...env(), FEISHU_WEBHOOK_SECRET: "old-secret", FEISHU_ALERTS_WEBHOOK_URL: "https://example.com/new-group", FEISHU_ALERTS_WEBHOOK_SECRET: "new-secret" };
  assert.deepEqual(configuredChannels(e), ["feishu", "feishu:alerts"]);
  for (const channel of configuredChannels(e)) {
    await sendEvent(e, event, channel, async (url, options) => {
      const payload = JSON.parse(options.body);
      const extra = channel === "feishu:alerts";
      assert.equal(url, extra ? e.FEISHU_ALERTS_WEBHOOK_URL : e.FEISHU_WEBHOOK_URL);
      const secret = extra ? e.FEISHU_ALERTS_WEBHOOK_SECRET : e.FEISHU_WEBHOOK_SECRET;
      assert.equal(payload.sign, createHmac("sha256", `${payload.timestamp}\n${secret}`).update("").digest("base64"));
      return Response.json({ code: 0 });
    });
  }
  assert.deepEqual(configuredChannels({ ...e, FEISHU_ALERTS_WEBHOOK_URL: e.FEISHU_WEBHOOK_URL }), ["feishu"]);
});

test("enabling a second robot never backfills historical events", async () => {
  const e = env();
  await commitRaceState(e, { version: 0, state: {}, events: [event] });
  e.FEISHU_ALERTS_WEBHOOK_URL = "https://example.com/new-group";
  await commitRaceState(e, { version: 1, state: {}, events: [] });
  assert.equal((await e.DB.prepare("SELECT COUNT(*) AS n FROM monitor_deliveries").first()).n, 1);
  await commitRaceState(e, { version: 2, state: {}, events: [{ ...event, key: "races:digest:new-after-second-robot" }] });
  assert.equal((await e.DB.prepare("SELECT COUNT(*) AS n FROM monitor_deliveries").first()).n, 3);
});

test("a no-new daily receipt reaches both robots once through the existing transactional outbox", async () => {
  const e = env();
  e.FEISHU_ALERTS_WEBHOOK_URL = "https://example.com/new-group";
  const receipt = { key: "races:no-new:20261027", type: "race_no_new", payload: {
    message: "🏃 赛事监控｜今日无新增\n2026-10-27 09:05（北京时间）\n检查完成，数据源正常。" } };
  for (const version of [0, 1]) {
    const response = await worker.fetch(new Request("https://worker.test/races/commit", { method: "POST",
      headers: { authorization: "Bearer test" }, body: JSON.stringify({ version, state: {}, events: [receipt] }) }), e);
    assert.equal(response.status, 200);
  }
  const rows = (await e.DB.prepare("SELECT id,channel FROM monitor_deliveries ORDER BY id").all()).results;
  assert.deepEqual(rows.map(r => r.channel), ["feishu", "feishu:alerts"]);
  const previousFetch = globalThis.fetch; const posts = [];
  globalThis.fetch = async (url, options) => {
    const payload = JSON.parse(options.body);
    assert.equal(payload.content.text, receipt.payload.message);
    posts.push(url);
    return Response.json({ code: 0 });
  };
  const messages = rows.map(row => ({ body: { deliveryId: row.id }, ack() {}, retry() { assert.fail("must succeed"); } }));
  try {
    await worker.queue({ messages }, e);
    await worker.queue({ messages }, e);
    assert.deepEqual(posts, [e.FEISHU_WEBHOOK_URL, e.FEISHU_ALERTS_WEBHOOK_URL]);
  } finally { globalThis.fetch = previousFetch; }
});

test("removing the original bot leaves daily receipts and discoveries addressed only to the remaining bot", async () => {
  const e = env();
  delete e.FEISHU_WEBHOOK_URL;
  e.FEISHU_ALERTS_WEBHOOK_URL = "https://example.com/remaining";
  const receipt = { key: "races:no-new:20261028", type: "race_no_new", payload: { message: "今日无新增" } };
  await commitRaceState(e, { version: 0, state: { last_completed_at: new Date().toISOString() }, events: [event, receipt] });
  const rows = (await e.DB.prepare("SELECT id,channel FROM monitor_deliveries ORDER BY id").all()).results;
  assert.deepEqual(rows.map(r => r.channel), ["feishu:alerts", "feishu:alerts"]);
  const health = await (await raceHealthResponse(e)).json();
  assert.deepEqual(health.channels, ["feishu:alerts"]);
  const previousFetch = globalThis.fetch; const calls = [];
  globalThis.fetch = async url => { calls.push(url); return Response.json({ code: 0 }); };
  try {
    await worker.queue({ messages: rows.map(row => ({ body: { deliveryId: row.id }, ack() {}, retry() { assert.fail("must succeed"); } })) }, e);
    assert.deepEqual(calls, [e.FEISHU_ALERTS_WEBHOOK_URL, e.FEISHU_ALERTS_WEBHOOK_URL]);
  } finally { globalThis.fetch = previousFetch; }
});
