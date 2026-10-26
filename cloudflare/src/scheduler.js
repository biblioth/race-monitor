import { raceState } from "./storage.js";
import { configuredChannels } from "./notifications.js";

const MINUTE = 60_000;
const START = 9 * 60;
const DEADLINE = START + 45;
const RETRY = 30 * MINUTE;
const MAX_ATTEMPTS = 6;
const WORKFLOW = "https://api.github.com/repos/biblioth/race-monitor/actions/workflows/race-monitor.yml/dispatches";
const ACTIONS = "https://github.com/biblioth/race-monitor/actions/workflows/race-monitor.yml";

export function beijingTime(now) {
  const local = new Date(now.getTime() + 8 * 60 * MINUTE);
  return { date: local.toISOString().slice(0, 10), minutes: local.getUTCHours() * 60 + local.getUTCMinutes(),
    time: local.toISOString().slice(11, 16) };
}

export function schedulerEnabled(env) {
  return env.SCHEDULER_ENABLED === "true";
}

export async function dispatchDailyWorkflow(env, fetcher = fetch) {
  if (!env.GITHUB_DISPATCH_TOKEN) throw new Error("GitHub dispatch credential is missing");
  let response;
  try {
    response = await fetcher(WORKFLOW, {
      // Workers supports manual/follow, but not the standard "error" mode.
      // Manual plus the status check below refuses redirects without forwarding credentials.
      method: "POST", redirect: "manual", signal: AbortSignal.timeout(20_000),
      headers: { authorization: `Bearer ${env.GITHUB_DISPATCH_TOKEN}`, accept: "application/vnd.github+json",
        "content-type": "application/json", "user-agent": "race-monitor-cloudflare-scheduler",
        "x-github-api-version": "2022-11-28" },
      body: JSON.stringify({ ref: "main", inputs: { trigger: "cloudflare" } }),
    });
  } catch {
    // Never persist a fetch exception or upstream body: they can contain credentials.
    throw new Error("GitHub dispatch request failed or timed out");
  }
  if (![200, 204].includes(response.status)) throw new Error(`GitHub dispatch HTTP ${response.status}`);
  return { accepted: true };
}

async function scheduleEvent(env, date, kind, message, at) {
  const channels = configuredChannels(env);
  if (!channels.length) throw new Error("Feishu is not configured");
  const key = `races:schedule:${kind}:${date}`;
  const column = kind === "overdue" ? "overdue_at" : "recovered_at";
  const completed = `EXISTS (SELECT 1 FROM race_monitor_state WHERE id=1 AND json_extract(state_json,'$.last_run_date')=?)`;
  const statements = [env.DB.prepare(`INSERT OR IGNORE INTO monitor_events(idempotency_key,event_type,payload_json,created_at)
    SELECT ?,?,?,? WHERE ${kind === "overdue" ? "NOT " : ""}${completed}`)
    .bind(key, `race_schedule_${kind}`, JSON.stringify({ message }), at, date)];
  for (const channel of channels) {
    statements.push(env.DB.prepare(`INSERT OR IGNORE INTO monitor_deliveries(event_id,channel,status,created_at,updated_at)
      SELECT id,?,'pending',?,? FROM monitor_events WHERE idempotency_key=?`).bind(channel, at, at, key));
  }
  statements.push(env.DB.prepare(`UPDATE race_scheduler_days SET ${column}=COALESCE(${column},?)
    WHERE run_date=? AND EXISTS (SELECT 1 FROM monitor_events WHERE idempotency_key=?)`).bind(at, date, key));
  await env.DB.batch(statements);
}

export async function schedulerStatus(env, now = new Date()) {
  const { date } = beijingTime(now);
  const day = await env.DB.prepare(`SELECT attempts,last_dispatch_at,last_dispatch_status,last_dispatch_error,
    overdue_at,recovered_at,last_checked_at FROM race_scheduler_days WHERE run_date=?`).bind(date).first();
  return { enabled: schedulerEnabled(env), configured: Boolean(env.GITHUB_DISPATCH_TOKEN), date, plannedTime: "09:00", deadline: "09:45", timezone: "Asia/Shanghai",
    maxAttempts: MAX_ATTEMPTS, ...(day || { attempts: 0 }) };
}

export async function schedulerTick(env, now = new Date(), fetcher = fetch) {
  if (!schedulerEnabled(env)) return { status: "disabled" };
  const { date, minutes, time } = beijingTime(now);
  const at = now.toISOString();
  await env.DB.prepare(`INSERT INTO race_scheduler_days(run_date,last_checked_at) VALUES (?,?)
    ON CONFLICT(run_date) DO UPDATE SET last_checked_at=excluded.last_checked_at`).bind(date, at).run();
  const { state } = await raceState(env.DB);
  let day = await env.DB.prepare("SELECT * FROM race_scheduler_days WHERE run_date=?").bind(date).first();
  if (state.last_run_date === date) {
    if (day.overdue_at && !day.recovered_at) {
      const completed = state.last_completed_at ? beijingTime(new Date(state.last_completed_at)).time : time;
      await scheduleEvent(env, date, "recovered", `✅ 赛事监控已补跑完成｜${date}\n完成时间：${completed}（北京时间）\n新增活动按原规则推送；无新增保持静默。`, at);
    }
    return { status: "completed", date };
  }
  if (minutes < START) return { status: "not_due", date };
  const started = Date.parse(state.run?.started_at || "");
  const running = Number.isFinite(started) && now.getTime() - started >= 0 && now.getTime() - started < 25 * MINUTE;
  if (!running) {
    const lease = crypto.randomUUID();
    // One atomic claim across overlapping cron invocations. Even an ambiguous
    // dispatch response is throttled, and the collector's daily CAS is the final guard.
    const claimed = await env.DB.prepare(`UPDATE race_scheduler_days SET attempts=attempts+1,
      last_dispatch_at=?,dispatch_lease=?,last_dispatch_status='dispatching',last_dispatch_error=NULL
      WHERE run_date=? AND attempts<? AND (last_dispatch_at IS NULL OR last_dispatch_at<=?)`)
      .bind(at, lease, date, MAX_ATTEMPTS, new Date(now.getTime() - RETRY).toISOString()).run();
    if (claimed.meta?.changes) {
      let status = "accepted", error = null;
      try { await dispatchDailyWorkflow(env, fetcher); }
      catch (e) { status = "failed"; error = e.message; }
      await env.DB.prepare(`UPDATE race_scheduler_days SET last_dispatch_status=?,last_dispatch_error=?
        WHERE run_date=? AND dispatch_lease=?`).bind(status, error, date, lease).run();
    }
  }
  day = await env.DB.prepare("SELECT * FROM race_scheduler_days WHERE run_date=?").bind(date).first();
  if (minutes >= DEADLINE && !day.overdue_at) {
    const detail = day.last_dispatch_error ? `\n最近触发：${day.last_dispatch_error}` : "";
    await scheduleEvent(env, date, "overdue", `⚠️ 赛事监控未按时完成｜${date}\n计划：09:00（北京时间）\n截至 ${time} 仍未完成，已尝试触发 ${day.attempts} 次。${detail}\n系统继续自动补偿，新增活动可能延迟。\n运行记录：${ACTIONS}`, at);
  }
  return { status: running ? "running" : day.last_dispatch_status || "waiting", date, attempts: day.attempts };
}
