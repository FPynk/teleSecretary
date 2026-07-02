CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    item_type TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    source TEXT NOT NULL,
    raw_input_text TEXT,
    parse_status TEXT NOT NULL DEFAULT 'not_applicable',
    parse_confidence REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    CHECK (item_type IN ('task', 'note')),
    CHECK (length(trim(title)) > 0),
    CHECK (status IN ('active', 'completed', 'archived', 'deleted')),
    CHECK (source IN (
        'telegram_command',
        'telegram_nl',
        'manual_entry',
        'system_generated',
        'test_fixture'
    )),
    CHECK (parse_status IN (
        'not_applicable',
        'parsed',
        'fallback',
        'needs_clarification',
        'failed'
    )),
    CHECK (parse_confidence IS NULL OR (
        parse_confidence >= 0.0 AND parse_confidence <= 1.0
    )),
    CHECK ((status = 'deleted') = (deleted_at IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS categories (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    archived_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    CHECK (length(trim(name)) > 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_categories_active_name_per_user
ON categories (user_id, name)
WHERE archived_at IS NULL;

CREATE TABLE IF NOT EXISTS tags (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    CHECK (length(trim(name)) > 0),
    UNIQUE (user_id, name)
);

CREATE TABLE IF NOT EXISTS task_items (
    item_id TEXT PRIMARY KEY,
    description TEXT,
    category_id TEXT,
    deadline_at TEXT,
    deadline_type TEXT,
    planned_start_at TEXT,
    planned_end_at TEXT,
    estimated_minutes INTEGER,
    urgency TEXT,
    completed_at TEXT,
    FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE,
    FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE SET NULL,
    CHECK (deadline_type IS NULL OR deadline_type IN ('hard', 'soft')),
    CHECK (urgency IS NULL OR urgency IN (
        'low',
        'medium',
        'high',
        'top_priority'
    )),
    CHECK (deadline_at IS NOT NULL OR deadline_type IS NULL),
    CHECK (
        planned_start_at IS NULL
        OR planned_end_at IS NULL
        OR planned_end_at >= planned_start_at
    ),
    CHECK (estimated_minutes IS NULL OR estimated_minutes > 0)
);

CREATE TABLE IF NOT EXISTS note_items (
    item_id TEXT PRIMARY KEY,
    body TEXT,
    FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS item_tags (
    item_id TEXT NOT NULL,
    tag_id TEXT NOT NULL,
    PRIMARY KEY (item_id, tag_id),
    FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS completion_logs (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    source TEXT NOT NULL,
    FOREIGN KEY (item_id) REFERENCES task_items(item_id) ON DELETE CASCADE,
    CHECK (event_type IN ('completed', 'reopened')),
    CHECK (source IN (
        'telegram_command',
        'telegram_nl',
        'manual_entry',
        'system_generated',
        'test_fixture'
    ))
);

CREATE INDEX IF NOT EXISTS idx_items_user_id
ON items (user_id);

CREATE INDEX IF NOT EXISTS idx_items_user_status
ON items (user_id, status);

CREATE INDEX IF NOT EXISTS idx_items_user_type_status
ON items (user_id, item_type, status);

CREATE INDEX IF NOT EXISTS idx_items_created_at
ON items (created_at);

CREATE INDEX IF NOT EXISTS idx_task_items_category_id
ON task_items (category_id);

CREATE INDEX IF NOT EXISTS idx_task_items_deadline_at
ON task_items (deadline_at);

CREATE INDEX IF NOT EXISTS idx_task_items_planned_start_at
ON task_items (planned_start_at);

CREATE INDEX IF NOT EXISTS idx_task_items_planned_end_at
ON task_items (planned_end_at);

CREATE INDEX IF NOT EXISTS idx_task_items_urgency
ON task_items (urgency);

CREATE INDEX IF NOT EXISTS idx_task_items_completed_at
ON task_items (completed_at);

CREATE INDEX IF NOT EXISTS idx_categories_user_id
ON categories (user_id);

CREATE INDEX IF NOT EXISTS idx_tags_user_id
ON tags (user_id);

CREATE INDEX IF NOT EXISTS idx_item_tags_tag_id
ON item_tags (tag_id);

CREATE INDEX IF NOT EXISTS idx_completion_logs_item_id
ON completion_logs (item_id);

CREATE INDEX IF NOT EXISTS idx_completion_logs_occurred_at
ON completion_logs (occurred_at);

CREATE TRIGGER IF NOT EXISTS trg_task_items_require_task_item
BEFORE INSERT ON task_items
FOR EACH ROW
WHEN (SELECT item_type FROM items WHERE id = NEW.item_id) IS NOT 'task'
BEGIN
    SELECT RAISE(ABORT, 'task_items.item_id must reference a task item');
END;

CREATE TRIGGER IF NOT EXISTS trg_note_items_require_note_item
BEFORE INSERT ON note_items
FOR EACH ROW
WHEN (SELECT item_type FROM items WHERE id = NEW.item_id) IS NOT 'note'
BEGIN
    SELECT RAISE(ABORT, 'note_items.item_id must reference a note item');
END;

CREATE TRIGGER IF NOT EXISTS trg_items_prevent_item_type_change
BEFORE UPDATE OF item_type ON items
FOR EACH ROW
WHEN NEW.item_type <> OLD.item_type
BEGIN
    SELECT RAISE(ABORT, 'items.item_type cannot be changed');
END;
