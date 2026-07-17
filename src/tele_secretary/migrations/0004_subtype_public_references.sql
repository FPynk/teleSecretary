ALTER TABLE items
ADD COLUMN pub_ref TEXT;

UPDATE items
SET pub_ref = (
    SELECT task_refs.task_ref
    FROM task_refs
    WHERE task_refs.task_id = items.id
)
WHERE item_type = 'task';

WITH ranked_notes AS (
    SELECT
        items.id,
        items.user_id,
        ROW_NUMBER() OVER (
            PARTITION BY items.user_id
            ORDER BY items.created_at, items.id
        ) AS user_note_index,
        COALESCE(ref_sequences.next_value, 1) AS first_reference_number
    FROM items
    LEFT JOIN ref_sequences
        ON ref_sequences.user_id = items.user_id
        AND ref_sequences.ref_type = 'note'
    WHERE items.item_type = 'note'
)
UPDATE items
SET pub_ref = (
    SELECT 'N' || (
        ranked_notes.first_reference_number
        + ranked_notes.user_note_index
        - 1
    )
    FROM ranked_notes
    WHERE ranked_notes.id = items.id
)
WHERE item_type = 'note';

INSERT INTO ref_sequences (user_id, ref_type, next_value)
SELECT
    user_id,
    'note',
    MAX(CAST(SUBSTR(pub_ref, 2) AS INTEGER)) + 1
FROM items
WHERE item_type = 'note'
GROUP BY user_id
ON CONFLICT (user_id, ref_type) DO UPDATE SET
    next_value = MAX(ref_sequences.next_value, excluded.next_value);

CREATE UNIQUE INDEX idx_items_user_pub_ref
ON items (user_id, pub_ref);

CREATE TRIGGER trg_items_require_valid_pub_ref
BEFORE INSERT ON items
FOR EACH ROW
WHEN NEW.pub_ref IS NULL
    OR length(NEW.pub_ref) < 2
    OR substr(NEW.pub_ref, 2, 1) NOT GLOB '[1-9]'
    OR substr(NEW.pub_ref, 2) GLOB '*[^0-9]*'
    OR CAST(substr(NEW.pub_ref, 2) AS INTEGER) < 1
    OR (NEW.item_type = 'task' AND substr(NEW.pub_ref, 1, 1) <> 'T')
    OR (NEW.item_type = 'note' AND substr(NEW.pub_ref, 1, 1) <> 'N')
BEGIN
    SELECT RAISE(ABORT, 'items.pub_ref must match the item type and contain a positive integer');
END;

CREATE TRIGGER trg_items_prevent_pub_ref_change
BEFORE UPDATE OF pub_ref ON items
FOR EACH ROW
WHEN NEW.pub_ref IS NOT OLD.pub_ref
BEGIN
    SELECT RAISE(ABORT, 'items.pub_ref cannot be changed');
END;

DROP TABLE task_refs;
