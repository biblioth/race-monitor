function parseJson(value, fallback = {}) {
  try {
    return JSON.parse(value || "");
  } catch {
    return fallback;
  }
}

export async function raceState(db) {
  const row = await db.prepare("SELECT version, state_json FROM race_monitor_state WHERE id=1").first();
  return { version: Number(row?.version || 0), state: row ? JSON.parse(row.state_json) : {} };
}

export async function pendingDeliveries(db, staleBefore, limit = 20) {
  const result = await db
    .prepare(
      `SELECT d.id FROM monitor_deliveries d
       WHERE d.status != 'delivered'
         AND (d.queued_at IS NULL OR d.queued_at < ?)
       ORDER BY d.id LIMIT ?`,
    )
    .bind(staleBefore, limit)
    .all();
  return result.results || [];
}

export async function markQueued(db, deliveryId, queuedAt) {
  await db
    .prepare(
      `UPDATE monitor_deliveries SET status='queued', queued_at=?, updated_at=? WHERE id=?`,
    )
    .bind(queuedAt, queuedAt, deliveryId)
    .run();
}

export async function getDelivery(db, deliveryId) {
  const row = await db
    .prepare(
      `SELECT d.*, e.event_type, e.payload_json
       FROM monitor_deliveries d
       JOIN monitor_events e ON e.id=d.event_id
       WHERE d.id=?`,
    )
    .bind(deliveryId)
    .first();
  if (!row) return null;
  return {
    id: row.id,
    channel: row.channel,
    status: row.status,
    attempts: Number(row.attempts || 0),
    event: { type: row.event_type, payload: parseJson(row.payload_json) },
  };
}

export async function markDelivered(db, deliveryId, deliveredAt) {
  await db
    .prepare(
      `UPDATE monitor_deliveries SET status='delivered', attempts=attempts+1,
       delivered_at=?, last_error=NULL, updated_at=? WHERE id=?`,
    )
    .bind(deliveredAt, deliveredAt, deliveryId)
    .run();
}

export async function markDeliveryFailed(db, deliveryId, error, failedAt) {
  await db
    .prepare(
      `UPDATE monitor_deliveries SET status='pending', attempts=attempts+1,
       last_error=?, updated_at=? WHERE id=?`,
    )
    .bind(String(error).slice(0, 1000), failedAt, deliveryId)
    .run();
}

