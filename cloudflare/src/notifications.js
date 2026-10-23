import { renderRaceMessage } from "./race-messages.js";

export const renderEvent = renderRaceMessage;

export function configuredChannels(env) {
  const channels = [];
  if (env.FEISHU_WEBHOOK_URL) channels.push("feishu");
  if (env.FEISHU_ALERTS_WEBHOOK_URL && env.FEISHU_ALERTS_WEBHOOK_URL !== env.FEISHU_WEBHOOK_URL) channels.push("feishu:alerts");
  return channels;
}

async function feishuSignature(secret, timestamp) {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(`${timestamp}\n${secret}`),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign("HMAC", key, new Uint8Array());
  return btoa(String.fromCharCode(...new Uint8Array(signature)));
}

async function sendFeishu(env, message, fetcher) {
  const payload = { msg_type: "text", content: { text: message } };
  if (env.FEISHU_WEBHOOK_SECRET) {
    payload.timestamp = String(Math.floor(Date.now() / 1000));
    payload.sign = await feishuSignature(env.FEISHU_WEBHOOK_SECRET, payload.timestamp);
  }
  const response = await fetcher(env.FEISHU_WEBHOOK_URL, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new Error(`Feishu returned HTTP ${response.status}`);
  const result = await response.json();
  const code = result.code ?? result.StatusCode;
  if (![0, "0"].includes(code)) throw new Error(`Feishu rejected the message: ${code ?? "missing status"}`);
}

export async function sendEvent(env, event, channel, fetcher = fetch) {
  if (!["feishu", "feishu:alerts"].includes(channel)) throw new Error("Unsupported notification channel");
  const target = channel === "feishu:alerts" ? { FEISHU_WEBHOOK_URL: env.FEISHU_ALERTS_WEBHOOK_URL, FEISHU_WEBHOOK_SECRET: env.FEISHU_ALERTS_WEBHOOK_SECRET } : env;
  if (!target.FEISHU_WEBHOOK_URL) throw new Error("Feishu is not configured");
  return sendFeishu(target, renderRaceMessage(event).message, fetcher);
}
