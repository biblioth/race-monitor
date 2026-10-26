import { raceState, commitRaceState, raceHealthResponse, validateRaceCommit, raceSourceResponse } from "./races.js";
import { sendEvent } from "./notifications.js";
import { getDelivery, markDelivered, markDeliveryFailed, markQueued, pendingDeliveries } from "./storage.js";
import { dispatchDailyWorkflow, schedulerTick } from "./scheduler.js";

export async function enqueuePending(env) {
  const staleBefore = new Date(Date.now() - 30 * 60_000).toISOString();
  let count = 0;
  for (const row of await pendingDeliveries(env.DB, staleBefore)) {
    await env.NOTIFICATION_QUEUE.send({ deliveryId: row.id });
    await markQueued(env.DB, row.id, new Date().toISOString());
    count += 1;
  }
  return count;
}

function authorized(request, env) {
  return Boolean(env.ADMIN_TOKEN) && request.headers.get("authorization") === `Bearer ${env.ADMIN_TOKEN}`;
}

async function processQueue(batch, env) {
  for (const message of batch.messages) {
    const deliveryId = Number(message.body?.deliveryId);
    try {
      const delivery = await getDelivery(env.DB, deliveryId);
      if (!delivery || delivery.status === "delivered") {
        message.ack();
        continue;
      }
      await sendEvent(env, delivery.event, delivery.channel);
      await markDelivered(env.DB, delivery.id, new Date().toISOString());
      message.ack();
    } catch (error) {
      const failedAt = new Date().toISOString();
      await markDeliveryFailed(
        env.DB,
        deliveryId,
        error instanceof Error ? error.message : String(error),
        failedAt,
      );
      const delivery = await getDelivery(env.DB, deliveryId);
      const delaySeconds = Math.min(3600, 30 * 2 ** Math.min(delivery?.attempts || 0, 7));
      message.retry({ delaySeconds });
    }
  }
}

export default {
  async scheduled(controller, env, ctx) {
    ctx.waitUntil((async () => {
      try { await schedulerTick(env); }
      finally { await enqueuePending(env); }
    })());
  },
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "POST" && url.pathname === "/races/scheduler/dispatch") {
      if (!authorized(request, env)) return new Response("Unauthorized", { status: 401 });
      try { return Response.json(await dispatchDailyWorkflow(env)); }
      catch (error) { return Response.json({ accepted: false, error: error.message }, { status: 502 }); }
    }
    if (request.method === "GET" && url.pathname === "/races/health") return raceHealthResponse(env);
    if (request.method === "GET" && url.pathname === "/races/source") {
      if (!authorized(request, env)) return new Response("Unauthorized", { status: 401 });
      return raceSourceResponse(request);
    }
    if (url.pathname === "/races/state" && request.method === "GET") {
      if (!authorized(request, env)) return new Response("Unauthorized", { status: 401 });
      return Response.json(await raceState(env.DB));
    }
    if (url.pathname === "/races/commit" && request.method === "POST") {
      if (!authorized(request, env)) return new Response("Unauthorized", { status: 401 });
      let body;
      try {
        const text = await request.text();
        if (text.length > 4_000_000) return new Response("Payload too large", { status: 413 });
        body = JSON.parse(text);
        validateRaceCommit(body);
      } catch {
        return new Response("Invalid race commit", { status: 400 });
      }
      const result = await commitRaceState(env, body);
      if (result.conflict) return Response.json(result, { status: 409 });
      // The periodic queue sweeper also picks up committed events after a crash.
      try { result.queued = await enqueuePending(env); } catch { result.queued = false; }
      return Response.json(result);
    }
    if (request.method === "GET" && url.pathname === "/") {
      return Response.json({ service: "Race and outdoor activity monitor", repository: "https://github.com/biblioth/race-monitor", health: "/races/health" });
    }
    return new Response("Not found", { status: 404 });
  },
  async queue(batch, env) {
    await processQueue(batch, env);
  },
};
