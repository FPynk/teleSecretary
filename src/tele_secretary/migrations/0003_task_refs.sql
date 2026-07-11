CREATE TABLE task_refs (
    task_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    task_ref TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES task_items(item_id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    UNIQUE (user_id, task_ref)
);

INSERT INTO task_refs (task_id, user_id, task_ref)
SELECT
    ranked_tasks.id,
    ranked_tasks.user_id,
    'T' || (
        COALESCE(ref_sequences.next_value, 1)
        + ranked_tasks.user_task_index
        - 1
    )
FROM (
    SELECT
        items.id,
        items.user_id,
        ROW_NUMBER() OVER (
            PARTITION BY items.user_id
            ORDER BY items.created_at, items.id
        ) AS user_task_index
    FROM items
    JOIN task_items ON task_items.item_id = items.id
) AS ranked_tasks
LEFT JOIN ref_sequences
    ON ref_sequences.user_id = ranked_tasks.user_id
    AND ref_sequences.ref_type = 'task';

INSERT INTO ref_sequences (user_id, ref_type, next_value)
SELECT
    user_id,
    'task',
    MAX(CAST(SUBSTR(task_ref, 2) AS INTEGER)) + 1
FROM task_refs
GROUP BY user_id
ON CONFLICT (user_id, ref_type) DO UPDATE SET
    next_value = MAX(ref_sequences.next_value, excluded.next_value);

CREATE INDEX idx_task_refs_user_id
ON task_refs (user_id);

CREATE TRIGGER trg_task_refs_require_matching_owner
BEFORE INSERT ON task_refs
FOR EACH ROW
WHEN (
    SELECT user_id
    FROM items
    WHERE id = NEW.task_id
) IS NOT NEW.user_id
BEGIN
    SELECT RAISE(ABORT, 'task_refs.user_id must match the task owner');
END;
