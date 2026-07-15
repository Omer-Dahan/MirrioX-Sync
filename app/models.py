"""Domain model dataclasses. Each has a from_row() classmethod for SQLite rows."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

# Content types a job can copy, as classified by CopyEngine._get_content_type.
# Anything the classifier can't place lands outside this set and is only copied
# when every type is selected — see the filter shortcut in copy_engine.
ALL_CONTENT_TYPES = frozenset({"text", "image", "video", "file"})
DEFAULT_CONTENT_TYPES = "file,image,text,video"


@dataclass
class Admin:
    id: int
    telegram_id: int
    username: Optional[str]
    added_at: str
    added_by: Optional[int]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Admin":
        return cls(
            id=row["id"],
            telegram_id=row["telegram_id"],
            username=row["username"],
            added_at=row["added_at"],
            added_by=row["added_by"],
        )


@dataclass
class Source:
    id: int
    name: str
    channel_ref: str
    title: Optional[str]
    resolved_id: Optional[int]
    created_at: str
    validation_error: Optional[str] = None
    username: Optional[str] = None
    participants_count: Optional[int] = None
    about: Optional[str] = None
    verified: bool = False
    channel_type: Optional[str] = None
    total_messages: Optional[int] = None
    photos_count: Optional[int] = None
    videos_count: Optional[int] = None
    docs_count: Optional[int] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Source":
        keys = row.keys()
        return cls(
            id=row["id"],
            name=row["name"],
            channel_ref=row["channel_ref"],
            title=row["title"],
            resolved_id=row["resolved_id"],
            created_at=row["created_at"],
            validation_error=row["validation_error"] if "validation_error" in keys else None,
            username=row["username"] if "username" in keys else None,
            participants_count=row["participants_count"] if "participants_count" in keys else None,
            about=row["about"] if "about" in keys else None,
            verified=bool(row["verified"]) if "verified" in keys else False,
            channel_type=row["channel_type"] if "channel_type" in keys else None,
            total_messages=row["total_messages"] if "total_messages" in keys else None,
            photos_count=row["photos_count"] if "photos_count" in keys else None,
            videos_count=row["videos_count"] if "videos_count" in keys else None,
            docs_count=row["docs_count"] if "docs_count" in keys else None,
        )

    def display(self) -> str:
        label = self.title or self.channel_ref
        if label == self.name:
            return self.name
        return f"{self.name} ({label})"


@dataclass
class Destination:
    id: int
    name: str
    channel_ref: str
    title: Optional[str]
    resolved_id: Optional[int]
    created_at: str
    validation_error: Optional[str] = None
    username: Optional[str] = None
    participants_count: Optional[int] = None
    about: Optional[str] = None
    verified: bool = False
    channel_type: Optional[str] = None
    total_messages: Optional[int] = None
    photos_count: Optional[int] = None
    videos_count: Optional[int] = None
    docs_count: Optional[int] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Destination":
        keys = row.keys()
        return cls(
            id=row["id"],
            name=row["name"],
            channel_ref=row["channel_ref"],
            title=row["title"],
            resolved_id=row["resolved_id"],
            created_at=row["created_at"],
            validation_error=row["validation_error"] if "validation_error" in keys else None,
            username=row["username"] if "username" in keys else None,
            participants_count=row["participants_count"] if "participants_count" in keys else None,
            about=row["about"] if "about" in keys else None,
            verified=bool(row["verified"]) if "verified" in keys else False,
            channel_type=row["channel_type"] if "channel_type" in keys else None,
            total_messages=row["total_messages"] if "total_messages" in keys else None,
            photos_count=row["photos_count"] if "photos_count" in keys else None,
            videos_count=row["videos_count"] if "videos_count" in keys else None,
            docs_count=row["docs_count"] if "docs_count" in keys else None,
        )

    def display(self) -> str:
        label = self.title or self.channel_ref
        if label == self.name:
            return self.name
        return f"{self.name} ({label})"


@dataclass
class BlockedWord:
    id: int
    word: str
    added_at: str
    added_by: Optional[int]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "BlockedWord":
        return cls(
            id=row["id"],
            word=row["word"],
            added_at=row["added_at"],
            added_by=row["added_by"],
        )


@dataclass
class Job:
    id: int
    name: str
    source_id: int
    destination_id: int
    mode: str  # all | date_range | id_range | single_id
    date_from: Optional[str]
    date_to: Optional[str]
    id_from: Optional[int]
    id_to: Optional[int]
    single_message_id: Optional[int]
    use_blocked_words: bool
    group_media: bool
    copy_text: bool
    content_types: str  # comma-separated subset of ALL_CONTENT_TYPES
    report_url: Optional[str]
    status: str
    created_at: str
    started_at: Optional[str]
    completed_at: Optional[str]
    last_updated_at: str
    total_messages: int
    copied_count: int
    skipped_count: int
    failed_count: int
    last_processed_id: Optional[int]
    retry_count: int
    max_retries: int
    next_retry_at: Optional[str]
    error_message: Optional[str]
    submitted_at: Optional[str] = None
    created_by: Optional[int] = None
    continuous: bool = False
    backfill_done: bool = False
    assigned_userbot_id: Optional[int] = None
    excluded_userbot_ids: Optional[str] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Job":
        keys = row.keys()
        return cls(
            id=row["id"],
            name=row["name"],
            source_id=row["source_id"],
            destination_id=row["destination_id"],
            mode=row["mode"],
            date_from=row["date_from"],
            date_to=row["date_to"],
            id_from=row["id_from"],
            id_to=row["id_to"],
            single_message_id=row["single_message_id"],
            use_blocked_words=bool(row["use_blocked_words"]),
            group_media=bool(row["group_media"]) if "group_media" in row.keys() else True,
            copy_text=bool(row["copy_text"]) if "copy_text" in row.keys() else True,
            content_types=row["content_types"] if "content_types" in row.keys() else DEFAULT_CONTENT_TYPES,
            report_url=row["report_url"] if "report_url" in row.keys() else None,
            status=row["status"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            last_updated_at=row["last_updated_at"],
            total_messages=row["total_messages"] or 0,
            copied_count=row["copied_count"] or 0,
            skipped_count=row["skipped_count"] or 0,
            failed_count=row["failed_count"] or 0,
            last_processed_id=row["last_processed_id"],
            retry_count=row["retry_count"] or 0,
            max_retries=row["max_retries"] or 3,
            next_retry_at=row["next_retry_at"],
            error_message=row["error_message"],
            submitted_at=row["submitted_at"] if "submitted_at" in keys else None,
            created_by=row["created_by"] if "created_by" in keys else None,
            continuous=bool(row["continuous"]) if "continuous" in keys else False,
            backfill_done=bool(row["backfill_done"]) if "backfill_done" in keys else False,
            assigned_userbot_id=row["assigned_userbot_id"] if "assigned_userbot_id" in keys else None,
            excluded_userbot_ids=row["excluded_userbot_ids"] if "excluded_userbot_ids" in keys else None,
        )

    def is_active(self) -> bool:
        return self.status in ("pending", "running", "waiting_retry")

    def is_terminal(self) -> bool:
        return self.status in ("completed", "cancelled", "failed")

    def excluded_ids(self) -> set[int]:
        """Userbot IDs that already failed this job for lack of channel access."""
        if not self.excluded_userbot_ids:
            return set()
        out: set[int] = set()
        for part in self.excluded_userbot_ids.split(","):
            part = part.strip()
            if part.isdigit():
                out.add(int(part))
        return out


@dataclass
class CopiedMessage:
    id: int
    job_id: int
    source_message_id: int
    dest_message_id: Optional[int]
    status: str  # copied | skipped | failed
    skip_reason: Optional[str]
    processed_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "CopiedMessage":
        return cls(
            id=row["id"],
            job_id=row["job_id"],
            source_message_id=row["source_message_id"],
            dest_message_id=row["dest_message_id"],
            status=row["status"],
            skip_reason=row["skip_reason"],
            processed_at=row["processed_at"],
        )


@dataclass
class Userbot:
    id: int
    name: str
    phone: str
    session_name: str
    telegram_id: Optional[int]
    username: Optional[str]
    status: str  # active | inactive | unauthorized | error
    is_default: bool
    added_at: str
    last_seen: Optional[str]
    error_message: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Userbot":
        return cls(
            id=row["id"],
            name=row["name"],
            phone=row["phone"],
            session_name=row["session_name"],
            telegram_id=row["telegram_id"],
            username=row["username"],
            status=row["status"],
            is_default=bool(row["is_default"]),
            added_at=row["added_at"],
            last_seen=row["last_seen"],
            error_message=row["error_message"],
        )

    def display(self) -> str:
        label = self.name or self.username or self.phone or self.session_name
        if self.username and self.username != label:
            return f"{label} (@{self.username})"
        return label


@dataclass
class WorkerState:
    id: int
    status: str  # idle | running | stopped | error
    current_job_id: Optional[int]
    last_heartbeat: Optional[str]
    error_message: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "WorkerState":
        return cls(
            id=row["id"],
            status=row["status"],
            current_job_id=row["current_job_id"],
            last_heartbeat=row["last_heartbeat"],
            error_message=row["error_message"],
        )


class MirrioxError(Exception):
    """Base exception for business logic errors."""


class ValidationError(MirrioxError):
    """Input validation failed. Message should be Hebrew-ready."""


class JobError(MirrioxError):
    """Job lifecycle rule violated."""


class NoAccessError(MirrioxError):
    """
    The running userbot cannot reach the job's source or destination channel
    (not a member / private / write-forbidden).

    Raised instead of failing the job outright so the worker can hand the job
    to a different userbot that *is* a member. Only when every active userbot
    has raised this does the job fail.
    """
