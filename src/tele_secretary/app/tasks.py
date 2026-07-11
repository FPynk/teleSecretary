"""Task and note application services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from sqlite3 import Connection
from typing import Any
from uuid import uuid4

from tele_secretary.persistence.refs import allocate_ref
from tele_secretary.time_utils import to_storage_text, utc_now_iso


ALLOWED_SOURCES = {
    "telegram_command",
    "telegram_nl",
    "manual_entry",
    "system_generated",
    "test_fixture",
}
ALLOWED_PARSE_STATUSES = {
    "not_applicable",
    "parsed",
    "fallback",
    "needs_clarification",
    "failed",
}
ALLOWED_STATUSES = {"active", "completed", "archived", "deleted"}
ALLOWED_DEADLINE_TYPES = {"hard", "soft"}
ALLOWED_URGENCIES = {"low", "medium", "high", "top_priority"}


class TaskServiceError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TaskValidationError(TaskServiceError):
    pass


class TaskNotFoundError(TaskServiceError):
    pass


@dataclass(frozen=True)
class CategoryRecord:
    id: str
    user_id: str
    name: str
    created_at: str
    archived_at: str | None


@dataclass(frozen=True)
class TagRecord:
    id: str
    user_id: str
    name: str
    created_at: str


@dataclass(frozen=True)
class VocabularyRecord:
    categories: tuple[CategoryRecord, ...]
    tags: tuple[TagRecord, ...]


@dataclass(frozen=True)
class TaskRecord:
    id: str
    ref: str
    user_id: str
    title: str
    status: str
    source: str
    raw_input_text: str | None
    parse_status: str
    parse_confidence: float | None
    created_at: str
    updated_at: str
    deleted_at: str | None
    description: str | None
    category_id: str | None
    category_name: str | None
    deadline_at: str | None
    deadline_type: str | None
    planned_start_at: str | None
    planned_end_at: str | None
    estimated_minutes: int | None
    urgency: str | None
    completed_at: str | None
    tags: tuple[TagRecord, ...]


@dataclass(frozen=True)
class NoteRecord:
    id: str
    user_id: str
    title: str
    status: str
    source: str
    raw_input_text: str | None
    parse_status: str
    parse_confidence: float | None
    created_at: str
    updated_at: str
    deleted_at: str | None
    body: str | None


@dataclass(frozen=True)
class DeleteTaskResult:
    task_id: str
    deleted_at: str


class _UnsetValue:
    pass


UNSET = _UnsetValue()


def create_task(
    conn: Connection,
    *,
    user_id: str,
    title: str,
    source: str,
    description: str | None = None,
    category_id: str | None = None,
    deadline_at: datetime | None = None,
    deadline_type: str | None = None,
    planned_start_at: datetime | None = None,
    planned_end_at: datetime | None = None,
    estimated_minutes: int | None = None,
    urgency: str | None = None,
    raw_input_text: str | None = None,
    parse_status: str = "not_applicable",
    parse_confidence: float | None = None,
    tag_ids: tuple[str, ...] | list[str] = (),
) -> TaskRecord:
    trimmed_title = _validate_title(title)
    _validate_source(source)
    _validate_parse_metadata(parse_status, parse_confidence)
    deadline_at_text = _format_optional_datetime(deadline_at, "deadline_at")
    planned_start_at_text = _format_optional_datetime(planned_start_at, "planned_start_at")
    planned_end_at_text = _format_optional_datetime(planned_end_at, "planned_end_at")
    _validate_task_fields(
        deadline_at=deadline_at_text,
        deadline_type=deadline_type,
        planned_start_at=planned_start_at_text,
        planned_end_at=planned_end_at_text,
        estimated_minutes=estimated_minutes,
        urgency=urgency,
    )
    unique_tag_ids = _unique_ids(tag_ids)
    item_id = str(uuid4())
    now = utc_now_iso()

    _require_user(conn, user_id)
    if category_id is not None:
        _require_active_category(conn, user_id=user_id, category_id=category_id)
    _require_tags(conn, user_id=user_id, tag_ids=unique_tag_ids)
    task_ref = allocate_ref(conn, user_id=user_id, ref_type="task")

    with conn:
        conn.execute(
            """
            INSERT INTO items (
                id, user_id, item_type, title, status, source, raw_input_text,
                parse_status, parse_confidence, created_at, updated_at
            )
            VALUES (?, ?, 'task', ?, 'active', ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                user_id,
                trimmed_title,
                source,
                raw_input_text,
                parse_status,
                parse_confidence,
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO task_items (
                item_id, description, category_id, deadline_at, deadline_type,
                planned_start_at, planned_end_at, estimated_minutes, urgency
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                description,
                category_id,
                deadline_at_text,
                deadline_type,
                planned_start_at_text,
                planned_end_at_text,
                estimated_minutes,
                urgency,
            ),
        )
        conn.execute(
            "INSERT INTO task_refs (task_id, user_id, task_ref) VALUES (?, ?, ?)",
            (item_id, user_id, task_ref),
        )
        _replace_item_tags(conn, item_id=item_id, tag_ids=unique_tag_ids)

    return get_task_details(conn, user_id=user_id, task_id=item_id)


def get_task_details(
    conn: Connection,
    *,
    user_id: str,
    task_id: str,
    include_deleted: bool = False,
) -> TaskRecord:
    row = conn.execute(
        """
        SELECT
            items.id,
            task_refs.task_ref,
            items.user_id,
            items.title,
            items.status,
            items.source,
            items.raw_input_text,
            items.parse_status,
            items.parse_confidence,
            items.created_at,
            items.updated_at,
            items.deleted_at,
            task_items.description,
            task_items.category_id,
            categories.name AS category_name,
            task_items.deadline_at,
            task_items.deadline_type,
            task_items.planned_start_at,
            task_items.planned_end_at,
            task_items.estimated_minutes,
            task_items.urgency,
            task_items.completed_at
        FROM items
        JOIN task_items ON task_items.item_id = items.id
        JOIN task_refs ON task_refs.task_id = items.id
        LEFT JOIN categories ON categories.id = task_items.category_id
        WHERE items.user_id = ?
            AND items.id = ?
            AND items.item_type = 'task'
            AND (? OR items.deleted_at IS NULL)
        """,
        (user_id, task_id, include_deleted),
    ).fetchone()
    if row is None:
        raise TaskNotFoundError("task_not_found", "Task was not found.")
    return _task_from_row(conn, row)


def get_task_details_by_ref(
    conn: Connection,
    *,
    user_id: str,
    task_ref: str,
) -> TaskRecord:
    row = conn.execute(
        """
        SELECT task_id
        FROM task_refs
        WHERE user_id = ? AND task_ref = ?
        """,
        (user_id, task_ref.upper()),
    ).fetchone()
    if row is None:
        raise TaskNotFoundError("task_not_found", "Task was not found.")
    return get_task_details(conn, user_id=user_id, task_id=row["task_id"])


def edit_task_by_ref(
    conn: Connection,
    *,
    user_id: str,
    task_ref: str,
    source: str,
    task_field_updates: dict[str, Any],
    category_was_provided: bool = False,
    category_name: str | None = None,
    add_tag_names: tuple[str, ...] = (),
    remove_tag_names: tuple[str, ...] = (),
    clear_tags: bool = False,
) -> TaskRecord:
    task = get_task_details_by_ref(conn, user_id=user_id, task_ref=task_ref)
    resolved_updates = dict(task_field_updates)
    vocabulary = None

    if category_was_provided:
        if category_name is None:
            resolved_updates["category_id"] = None
        else:
            vocabulary = list_categories_and_tags(conn, user_id=user_id)
            categories_by_name = {
                category.name: category for category in vocabulary.categories
            }
            category = categories_by_name.get(category_name)
            if category is None:
                raise TaskValidationError(
                    "unknown_category",
                    f'Category "{category_name}" does not exist.',
                )
            resolved_updates["category_id"] = category.id

    if clear_tags or add_tag_names or remove_tag_names:
        if vocabulary is None:
            vocabulary = list_categories_and_tags(conn, user_id=user_id)
        tags_by_name = {tag.name: tag for tag in vocabulary.tags}
        for tag_name in (*add_tag_names, *remove_tag_names):
            if tag_name not in tags_by_name:
                raise TaskValidationError(
                    "unknown_tag",
                    f'Tag "{tag_name}" does not exist.',
                )

        next_tag_ids = set() if clear_tags else {tag.id for tag in task.tags}
        next_tag_ids.update(tags_by_name[tag_name].id for tag_name in add_tag_names)
        next_tag_ids.difference_update(
            tags_by_name[tag_name].id for tag_name in remove_tag_names
        )
        resolved_updates["tag_ids"] = tuple(sorted(next_tag_ids))

    return update_task_fields(
        conn,
        user_id=user_id,
        task_id=task.id,
        source=source,
        **resolved_updates,
    )


def list_active_tasks(
    conn: Connection,
    *,
    user_id: str,
    status: str = "active",
    category_id: str | None = None,
) -> tuple[TaskRecord, ...]:
    _validate_status(status)
    sql = """
        SELECT
            items.id,
            task_refs.task_ref,
            items.user_id,
            items.title,
            items.status,
            items.source,
            items.raw_input_text,
            items.parse_status,
            items.parse_confidence,
            items.created_at,
            items.updated_at,
            items.deleted_at,
            task_items.description,
            task_items.category_id,
            categories.name AS category_name,
            task_items.deadline_at,
            task_items.deadline_type,
            task_items.planned_start_at,
            task_items.planned_end_at,
            task_items.estimated_minutes,
            task_items.urgency,
            task_items.completed_at
        FROM items
        JOIN task_items ON task_items.item_id = items.id
        JOIN task_refs ON task_refs.task_id = items.id
        LEFT JOIN categories ON categories.id = task_items.category_id
        WHERE items.user_id = ?
            AND items.item_type = 'task'
            AND items.status = ?
            AND items.deleted_at IS NULL
    """
    params: list[Any] = [user_id, status]
    if category_id is not None:
        sql += " AND task_items.category_id = ?"
        params.append(category_id)
    sql += " ORDER BY items.created_at ASC, items.id ASC"
    return tuple(_task_from_row(conn, row) for row in conn.execute(sql, params).fetchall())


def list_categories_and_tags(
    conn: Connection,
    *,
    user_id: str,
    include_archived_categories: bool = False,
) -> VocabularyRecord:
    category_sql = """
        SELECT id, user_id, name, created_at, archived_at
        FROM categories
        WHERE user_id = ?
    """
    if not include_archived_categories:
        category_sql += " AND archived_at IS NULL"
    category_sql += " ORDER BY name ASC, id ASC"
    categories = tuple(
        CategoryRecord(
            id=row["id"],
            user_id=row["user_id"],
            name=row["name"],
            created_at=row["created_at"],
            archived_at=row["archived_at"],
        )
        for row in conn.execute(category_sql, (user_id,)).fetchall()
    )
    tags = tuple(
        TagRecord(
            id=row["id"],
            user_id=row["user_id"],
            name=row["name"],
            created_at=row["created_at"],
        )
        for row in conn.execute(
            """
            SELECT id, user_id, name, created_at
            FROM tags
            WHERE user_id = ?
            ORDER BY name ASC, id ASC
            """,
            (user_id,),
        ).fetchall()
    )
    return VocabularyRecord(categories=categories, tags=tags)


def update_task_fields(
    conn: Connection,
    *,
    user_id: str,
    task_id: str,
    source: str,
    title: str | _UnsetValue = UNSET,
    description: str | None | _UnsetValue = UNSET,
    category_id: str | None | _UnsetValue = UNSET,
    deadline_at: datetime | None | _UnsetValue = UNSET,
    deadline_type: str | None | _UnsetValue = UNSET,
    planned_start_at: datetime | None | _UnsetValue = UNSET,
    planned_end_at: datetime | None | _UnsetValue = UNSET,
    estimated_minutes: int | None | _UnsetValue = UNSET,
    urgency: str | None | _UnsetValue = UNSET,
    tag_ids: tuple[str, ...] | list[str] | _UnsetValue = UNSET,
) -> TaskRecord:
    _validate_source(source)
    current_task = get_task_details(conn, user_id=user_id, task_id=task_id)
    next_title = current_task.title if isinstance(title, _UnsetValue) else _validate_title(title)
    next_description = current_task.description if isinstance(description, _UnsetValue) else description
    next_category_id = current_task.category_id if isinstance(category_id, _UnsetValue) else category_id
    next_deadline_at = (
        current_task.deadline_at
        if isinstance(deadline_at, _UnsetValue)
        else _format_optional_datetime(deadline_at, "deadline_at")
    )
    next_deadline_type = current_task.deadline_type if isinstance(deadline_type, _UnsetValue) else deadline_type
    next_planned_start_at = (
        current_task.planned_start_at
        if isinstance(planned_start_at, _UnsetValue)
        else _format_optional_datetime(planned_start_at, "planned_start_at")
    )
    next_planned_end_at = (
        current_task.planned_end_at
        if isinstance(planned_end_at, _UnsetValue)
        else _format_optional_datetime(planned_end_at, "planned_end_at")
    )
    next_estimated_minutes = (
        current_task.estimated_minutes
        if isinstance(estimated_minutes, _UnsetValue)
        else estimated_minutes
    )
    next_urgency = current_task.urgency if isinstance(urgency, _UnsetValue) else urgency
    next_tag_ids = None if isinstance(tag_ids, _UnsetValue) else _unique_ids(tag_ids)
    _validate_task_fields(
        deadline_at=next_deadline_at,
        deadline_type=next_deadline_type,
        planned_start_at=next_planned_start_at,
        planned_end_at=next_planned_end_at,
        estimated_minutes=next_estimated_minutes,
        urgency=next_urgency,
    )
    now = utc_now_iso()

    with conn:
        if next_category_id is not None:
            _require_active_category(conn, user_id=user_id, category_id=next_category_id)
        if next_tag_ids is not None:
            _require_tags(conn, user_id=user_id, tag_ids=next_tag_ids)
        conn.execute(
            """
            UPDATE items
            SET title = ?, source = ?, updated_at = ?
            WHERE id = ? AND user_id = ? AND item_type = 'task' AND deleted_at IS NULL
            """,
            (next_title, source, now, task_id, user_id),
        )
        conn.execute(
            """
            UPDATE task_items
            SET description = ?,
                category_id = ?,
                deadline_at = ?,
                deadline_type = ?,
                planned_start_at = ?,
                planned_end_at = ?,
                estimated_minutes = ?,
                urgency = ?
            WHERE item_id = ?
            """,
            (
                next_description,
                next_category_id,
                next_deadline_at,
                next_deadline_type,
                next_planned_start_at,
                next_planned_end_at,
                next_estimated_minutes,
                next_urgency,
                task_id,
            ),
        )
        if next_tag_ids is not None:
            _replace_item_tags(conn, item_id=task_id, tag_ids=next_tag_ids)

    return get_task_details(conn, user_id=user_id, task_id=task_id)


def complete_task(
    conn: Connection,
    *,
    user_id: str,
    task_id: str,
    source: str,
    completed_at: datetime | None = None,
) -> TaskRecord:
    _validate_source(source)
    current_task = get_task_details(conn, user_id=user_id, task_id=task_id)
    if current_task.status != "active":
        raise TaskValidationError(
            "invalid_completion_transition",
            "Only active tasks can be completed.",
        )
    completed_at_text = _format_optional_datetime(completed_at, "completed_at") or utc_now_iso()

    with conn:
        conn.execute(
            """
            UPDATE items
            SET status = 'completed', source = ?, updated_at = ?
            WHERE id = ? AND user_id = ? AND item_type = 'task' AND deleted_at IS NULL
            """,
            (source, completed_at_text, task_id, user_id),
        )
        conn.execute(
            "UPDATE task_items SET completed_at = ? WHERE item_id = ?",
            (completed_at_text, task_id),
        )
        conn.execute(
            """
            INSERT INTO completion_logs (id, item_id, event_type, occurred_at, source)
            VALUES (?, ?, 'completed', ?, ?)
            """,
            (str(uuid4()), task_id, completed_at_text, source),
        )

    return get_task_details(conn, user_id=user_id, task_id=task_id)


def reopen_task(
    conn: Connection,
    *,
    user_id: str,
    task_id: str,
    source: str,
    reopened_at: datetime | None = None,
) -> TaskRecord:
    _validate_source(source)
    current_task = get_task_details(conn, user_id=user_id, task_id=task_id)
    if current_task.status != "completed":
        raise TaskValidationError(
            "invalid_reopen_transition",
            "Only completed tasks can be reopened.",
        )
    reopened_at_text = _format_optional_datetime(reopened_at, "reopened_at") or utc_now_iso()

    with conn:
        conn.execute(
            """
            UPDATE items
            SET status = 'active', source = ?, updated_at = ?
            WHERE id = ? AND user_id = ? AND item_type = 'task' AND deleted_at IS NULL
            """,
            (source, reopened_at_text, task_id, user_id),
        )
        conn.execute(
            "UPDATE task_items SET completed_at = NULL WHERE item_id = ?",
            (task_id,),
        )
        conn.execute(
            """
            INSERT INTO completion_logs (id, item_id, event_type, occurred_at, source)
            VALUES (?, ?, 'reopened', ?, ?)
            """,
            (str(uuid4()), task_id, reopened_at_text, source),
        )

    return get_task_details(conn, user_id=user_id, task_id=task_id)


def soft_delete_task(
    conn: Connection,
    *,
    user_id: str,
    task_id: str,
    source: str,
    deleted_at: datetime | None = None,
) -> DeleteTaskResult:
    _validate_source(source)
    current_task = get_task_details(conn, user_id=user_id, task_id=task_id)
    if current_task.status not in {"active", "completed"}:
        raise TaskValidationError(
            "invalid_delete_transition",
            "Only active or completed tasks can be deleted.",
        )
    deleted_at_text = _format_optional_datetime(deleted_at, "deleted_at") or utc_now_iso()

    with conn:
        conn.execute(
            """
            UPDATE items
            SET status = 'deleted',
                source = ?,
                updated_at = ?,
                deleted_at = ?
            WHERE id = ? AND user_id = ? AND item_type = 'task' AND deleted_at IS NULL
            """,
            (source, deleted_at_text, deleted_at_text, task_id, user_id),
        )

    return DeleteTaskResult(task_id=task_id, deleted_at=deleted_at_text)


def create_note(
    conn: Connection,
    *,
    user_id: str,
    title: str,
    source: str,
    body: str | None = None,
    raw_input_text: str | None = None,
    parse_status: str = "not_applicable",
    parse_confidence: float | None = None,
) -> NoteRecord:
    trimmed_title = _validate_title(title)
    _validate_source(source)
    _validate_parse_metadata(parse_status, parse_confidence)
    item_id = str(uuid4())
    now = utc_now_iso()

    with conn:
        _require_user(conn, user_id)
        conn.execute(
            """
            INSERT INTO items (
                id, user_id, item_type, title, status, source, raw_input_text,
                parse_status, parse_confidence, created_at, updated_at
            )
            VALUES (?, ?, 'note', ?, 'active', ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                user_id,
                trimmed_title,
                source,
                raw_input_text,
                parse_status,
                parse_confidence,
                now,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO note_items (item_id, body) VALUES (?, ?)",
            (item_id, body),
        )

    return _get_note(conn, user_id=user_id, note_id=item_id)


def _get_note(conn: Connection, *, user_id: str, note_id: str) -> NoteRecord:
    row = conn.execute(
        """
        SELECT
            items.id,
            items.user_id,
            items.title,
            items.status,
            items.source,
            items.raw_input_text,
            items.parse_status,
            items.parse_confidence,
            items.created_at,
            items.updated_at,
            items.deleted_at,
            note_items.body
        FROM items
        JOIN note_items ON note_items.item_id = items.id
        WHERE items.user_id = ?
            AND items.id = ?
            AND items.item_type = 'note'
            AND items.deleted_at IS NULL
        """,
        (user_id, note_id),
    ).fetchone()
    if row is None:
        raise TaskNotFoundError("note_not_found", "Note was not found.")
    return NoteRecord(
        id=row["id"],
        user_id=row["user_id"],
        title=row["title"],
        status=row["status"],
        source=row["source"],
        raw_input_text=row["raw_input_text"],
        parse_status=row["parse_status"],
        parse_confidence=row["parse_confidence"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row["deleted_at"],
        body=row["body"],
    )


def _task_from_row(conn: Connection, row: Any) -> TaskRecord:
    return TaskRecord(
        id=row["id"],
        ref=row["task_ref"],
        user_id=row["user_id"],
        title=row["title"],
        status=row["status"],
        source=row["source"],
        raw_input_text=row["raw_input_text"],
        parse_status=row["parse_status"],
        parse_confidence=row["parse_confidence"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row["deleted_at"],
        description=row["description"],
        category_id=row["category_id"],
        category_name=row["category_name"],
        deadline_at=row["deadline_at"],
        deadline_type=row["deadline_type"],
        planned_start_at=row["planned_start_at"],
        planned_end_at=row["planned_end_at"],
        estimated_minutes=row["estimated_minutes"],
        urgency=row["urgency"],
        completed_at=row["completed_at"],
        tags=_get_item_tags(conn, item_id=row["id"]),
    )


def _get_item_tags(conn: Connection, *, item_id: str) -> tuple[TagRecord, ...]:
    return tuple(
        TagRecord(
            id=row["id"],
            user_id=row["user_id"],
            name=row["name"],
            created_at=row["created_at"],
        )
        for row in conn.execute(
            """
            SELECT tags.id, tags.user_id, tags.name, tags.created_at
            FROM item_tags
            JOIN tags ON tags.id = item_tags.tag_id
            WHERE item_tags.item_id = ?
            ORDER BY tags.name ASC, tags.id ASC
            """,
            (item_id,),
        ).fetchall()
    )


def _replace_item_tags(
    conn: Connection,
    *,
    item_id: str,
    tag_ids: tuple[str, ...],
) -> None:
    conn.execute("DELETE FROM item_tags WHERE item_id = ?", (item_id,))
    conn.executemany(
        "INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)",
        ((item_id, tag_id) for tag_id in tag_ids),
    )


def _require_user(conn: Connection, user_id: str) -> None:
    if conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
        raise TaskValidationError("unknown_user", "User does not exist.")


def _require_active_category(
    conn: Connection,
    *,
    user_id: str,
    category_id: str,
) -> None:
    row = conn.execute(
        """
        SELECT 1
        FROM categories
        WHERE id = ? AND user_id = ? AND archived_at IS NULL
        """,
        (category_id, user_id),
    ).fetchone()
    if row is None:
        raise TaskValidationError(
            "invalid_category",
            "Category does not exist for this user.",
        )


def _require_tags(
    conn: Connection,
    *,
    user_id: str,
    tag_ids: tuple[str, ...],
) -> None:
    if not tag_ids:
        return
    placeholders = ", ".join("?" for _ in tag_ids)
    rows = conn.execute(
        f"""
        SELECT id
        FROM tags
        WHERE user_id = ? AND id IN ({placeholders})
        """,
        (user_id, *tag_ids),
    ).fetchall()
    found_ids = {row["id"] for row in rows}
    missing_ids = set(tag_ids) - found_ids
    if missing_ids:
        raise TaskValidationError("invalid_tags", "One or more tags are invalid.")


def _validate_title(title: str) -> str:
    trimmed_title = title.strip()
    if not trimmed_title:
        raise TaskValidationError("invalid_title", "Title must not be blank.")
    return trimmed_title


def _validate_source(source: str) -> None:
    if source not in ALLOWED_SOURCES:
        raise TaskValidationError("invalid_source", "Source is not allowed.")


def _validate_status(status: str) -> None:
    if status not in ALLOWED_STATUSES:
        raise TaskValidationError("invalid_status", "Status is not allowed.")


def _validate_parse_metadata(
    parse_status: str,
    parse_confidence: float | None,
) -> None:
    if parse_status not in ALLOWED_PARSE_STATUSES:
        raise TaskValidationError(
            "invalid_parse_status",
            "Parse status is not allowed.",
        )
    if parse_confidence is not None and not 0.0 <= parse_confidence <= 1.0:
        raise TaskValidationError(
            "invalid_parse_confidence",
            "Parse confidence must be between 0.0 and 1.0.",
        )


def _validate_task_fields(
    *,
    deadline_at: str | None,
    deadline_type: str | None,
    planned_start_at: str | None,
    planned_end_at: str | None,
    estimated_minutes: int | None,
    urgency: str | None,
) -> None:
    if deadline_type is not None and deadline_type not in ALLOWED_DEADLINE_TYPES:
        raise TaskValidationError(
            "invalid_deadline_type",
            "Deadline type is not allowed.",
        )
    if deadline_at is None and deadline_type is not None:
        raise TaskValidationError(
            "deadline_type_without_deadline",
            "Deadline type requires a deadline.",
        )
    if planned_start_at is not None and planned_end_at is not None:
        if planned_end_at < planned_start_at:
            raise TaskValidationError(
                "invalid_planned_window",
                "Planned end must not be earlier than planned start.",
            )
    if estimated_minutes is not None and estimated_minutes <= 0:
        raise TaskValidationError(
            "invalid_estimated_minutes",
            "Estimated minutes must be positive.",
        )
    if urgency is not None and urgency not in ALLOWED_URGENCIES:
        raise TaskValidationError("invalid_urgency", "Urgency is not allowed.")


def _format_optional_datetime(
    value: datetime | None,
    field_name: str,
) -> str | None:
    if value is None:
        return None
    try:
        return to_storage_text(value)
    except ValueError as exc:
        raise TaskValidationError(
            f"invalid_{field_name}",
            f"{field_name} must be timezone-aware.",
        ) from exc


def _unique_ids(ids: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    unique_ids: list[str] = []
    for id_value in ids:
        if id_value not in unique_ids:
            unique_ids.append(id_value)
    return tuple(unique_ids)
