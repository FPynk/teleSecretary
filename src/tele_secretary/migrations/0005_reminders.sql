CREATE TABLE IF NOT EXISTS reminders (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    delivery_channel TEXT NOT NULL DEFAULT 'telegram',
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_attempted_at TEXT,
    sent_at TEXT,
    failure_reason TEXT,
    cancelled_at TEXT,
    expired_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (item_id) REFERENCES task_items(item_id) ON DELETE CASCADE,
    CHECK (status IN (
        'pending',
        'processing',
        'sent',
        'failed',
        'cancelled',
        'expired'
    )),
    CHECK (delivery_channel IN ('telegram')),
    CHECK (retry_count >= 0),
    CHECK (length(trim(scheduled_at)) > 0),
    CHECK (length(trim(created_at)) > 0),
    CHECK (length(trim(updated_at)) > 0),
    CHECK (failure_reason IS NULL OR length(trim(failure_reason)) > 0),
    CHECK ((status = 'sent') = (sent_at IS NOT NULL)),
    CHECK ((status = 'cancelled') = (cancelled_at IS NOT NULL)),
    CHECK ((status = 'expired') = (expired_at IS NOT NULL)),
    CHECK (
        status <> 'failed'
        OR (last_attempted_at IS NOT NULL AND failure_reason IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_reminders_pending_schedule
ON reminders (scheduled_at, id)
WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_reminders_item_status_schedule
ON reminders (item_id, status, scheduled_at, id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_reminders_unique_active_schedule
ON reminders (item_id, scheduled_at, delivery_channel)
WHERE status IN ('pending', 'processing');
