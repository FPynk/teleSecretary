# TeleSecretary Database Diagram

```mermaid
erDiagram
    users {
        TEXT id PK
        INTEGER telegram_user_id UK
        TEXT timezone
        TEXT created_at
        TEXT updated_at
    }

    ref_sequences {
        TEXT user_id PK, FK
        TEXT ref_type PK
        INTEGER next_value
    }

    health_checks {
        INTEGER id PK
        TEXT checked_at
        TEXT status
        TEXT details
    }

    items {
        TEXT id PK
        TEXT user_id FK
        TEXT item_type
        TEXT title
        TEXT status
        TEXT source
        TEXT raw_input_text
        TEXT parse_status
        REAL parse_confidence
        TEXT created_at
        TEXT updated_at
        TEXT deleted_at
    }

    categories {
        TEXT id PK
        TEXT user_id FK
        TEXT name
        TEXT created_at
        TEXT archived_at
    }

    tags {
        TEXT id PK
        TEXT user_id FK
        TEXT name
        TEXT created_at
    }

    task_items {
        TEXT item_id PK, FK
        TEXT description
        TEXT category_id FK
        TEXT deadline_at
        TEXT deadline_type
        TEXT planned_start_at
        TEXT planned_end_at
        INTEGER estimated_minutes
        TEXT urgency
        TEXT completed_at
    }

    note_items {
        TEXT item_id PK, FK
        TEXT body
    }

    item_tags {
        TEXT item_id PK, FK
        TEXT tag_id PK, FK
    }

    completion_logs {
        TEXT id PK
        TEXT item_id FK
        TEXT event_type
        TEXT occurred_at
        TEXT source
    }

    users ||--o{ ref_sequences : owns
    users ||--o{ items : owns
    users ||--o{ categories : owns
    users ||--o{ tags : owns

    items ||--o| task_items : extends_task
    items ||--o| note_items : extends_note

    categories ||--o{ task_items : categorizes
    task_items ||--o{ completion_logs : records

    items ||--o{ item_tags : has
    tags ||--o{ item_tags : labels
```

## Relationship Notes

- `items` is the shared parent table for both tasks and notes.
- `task_items.item_id` must reference an `items` row with `item_type = 'task'`.
- `note_items.item_id` must reference an `items` row with `item_type = 'note'`.
- `items.item_type` cannot be changed after creation.
- `categories` are optional for tasks. Deleting a category sets
  `task_items.category_id` to `NULL`.
- `item_tags` is the many-to-many join table between `items` and `tags`.
- `completion_logs` only attach to task rows through `task_items`.
- `health_checks` is operational state and is not tied to a user.

## Important Constraints

- `users.telegram_user_id` is unique.
- `tags` are unique per user by `(user_id, name)`.
- Active category names are unique per user while `archived_at IS NULL`.
- `items.item_type` is either `task` or `note`.
- `items.status` is one of `active`, `completed`, `archived`, or `deleted`.
- Deleted items must have `deleted_at`; non-deleted items must not.
- `task_items.deadline_type` is `hard`, `soft`, or `NULL`.
- `task_items.urgency` is `low`, `medium`, `high`, `top_priority`, or `NULL`.
- `completion_logs.event_type` is either `completed` or `reopened`.
