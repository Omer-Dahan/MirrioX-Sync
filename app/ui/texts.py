"""All Hebrew UI strings. Single source of truth for the management bot."""
from __future__ import annotations

from typing import TYPE_CHECKING

# Safe at runtime: app.models imports nothing from app, so there is no cycle.
from app.models import ALL_CONTENT_TYPES, DEFAULT_CONTENT_TYPES

if TYPE_CHECKING:
    from app.models import (
        Job, Source, Destination, Admin, BlockedWord, WorkerState, Userbot, ChannelAccessRow,
    )

# ── Status labels ──────────────────────────────────────────────────────────────

STATUS_LABELS: dict[str, str] = {
    "draft":         "📝 טיוטה",
    "pending":       "⏳ ממתין לביצוע",
    "running":       "▶️ פועל",
    "paused":        "⏸ מושהית",
    "completed":     "✅ הושלם",
    "cancelled":     "🚫 בוטל",
    "failed":        "❌ נכשל",
    "waiting_retry": "🔄 ממתין לניסיון חוזר",
}

STATUS_ICONS: dict[str, str] = {
    "draft":         "📝",
    "pending":       "⏳",
    "running":       "▶️",
    "paused":        "⏸",
    "completed":     "✅",
    "cancelled":     "🚫",
    "failed":        "❌",
    "waiting_retry": "🔄",
}

def job_status_label(job: "Job") -> str:
    """
    Status label for a job.

    A continuous job has two phases, and they look very different to the user:
    it first copies history (a normal bulk run), then listens for new messages.
    """
    if getattr(job, "continuous", False):
        if not getattr(job, "backfill_done", False):
            if job.status == "running":
                return "▶️ מעתיק היסטוריה"
            if job.status == "pending":
                return "⏳ ממתין להעתקת היסטוריה"
        else:
            if job.status == "running":
                return "🟢 מאזין ברקע"
            if job.status == "pending":
                return "🔄 ממתין להאזנה"
    return STATUS_LABELS.get(job.status, job.status)


def job_status_icon(job: "Job") -> str:
    if getattr(job, "continuous", False) and job.status in ("running", "pending"):
        if not getattr(job, "backfill_done", False):
            return "▶️" if job.status == "running" else "⏳"
        return "🟢" if job.status == "running" else "🔄"
    return STATUS_ICONS.get(job.status, "•")


SCAN_STATUS_ICONS: dict[str, str] = {
    "pending": "⏳",
    "running": "▶️",
    "done":    "✅",
    "failed":  "❌",
}

MODE_LABELS: dict[str, str] = {
    "all":        "📋 כל ההודעות",
    "date_range": "📅 טווח תאריכים",
    "id_range":   "🔢 טווח מזהים",
    "single_id":  "1️⃣ הודעה בודדת",
}

WORKER_STATUS_LABELS: dict[str, str] = {
    "idle":    "💤 במתינה",
    "running": "▶️ פועל",
    "stopped": "⏹ עצור",
    "error":   "❌ שגיאה",
}

# ── Button labels ──────────────────────────────────────────────────────────────

BTN_MAIN_MENU       = "🏠 תפריט ראשי"
BTN_JOBS            = "📂 משימות"
BTN_NEW_JOB         = "➕ משימה חדשה"
BTN_SOURCES         = "📡 מקורות"
BTN_DESTINATIONS    = "📤 יעדים"
BTN_BLOCKED_WORDS   = "🚫 מילים חסומות"
BTN_ADMINS          = "👥 מנהלים"
BTN_SETTINGS        = "⚙️ הגדרות"
BTN_BACK            = "⬅️ חזרה"
BTN_CANCEL          = "❌ ביטול"
BTN_CONFIRM         = "✅ אישור"
BTN_DELETE          = "🗑 מחק"
BTN_REFRESH         = "🔄 רענן"
BTN_ADD             = "➕ הוסף"
BTN_SUBMIT_JOB      = "▶️ הגש להרצה"
BTN_PAUSE_JOB       = "⏸ השהה משימה"
BTN_RESUME_JOB      = "▶️ המשך משימה"
BTN_EDIT_JOB        = "✏️ ערוך משימה"
BTN_EDIT_CONTENT_TYPES = "📁 סוגי תוכן"
BTN_EDIT_DESTS      = "🎯 ערוך יעדים"
BTN_RESET_EXCLUSIONS = "🔄 אפס חסימות גישה"
BTN_EDIT_ACCOUNTS   = "🤖 חשבונות מריצים"
BTN_CANCEL_JOB      = "⏹ בטל משימה"
BTN_DELETE_JOB      = "🗑 מחק משימה"
BTN_YES_DELETE      = "✅ כן, מחק"
BTN_YES_CANCEL      = "✅ כן, בטל"
BTN_YES_CLEAR       = "✅ כן, מחק הכל"
BTN_FILTER_TOGGLE_ON  = "🚫 סינון: כן"
BTN_FILTER_TOGGLE_OFF = "✅ סינון: לא"
BTN_GROUP_TOGGLE_ON   = "✅ שליחה במרוכז: כן"
BTN_GROUP_TOGGLE_OFF  = "❌ שליחה במרוכז: לא"
BTN_TEXT_TOGGLE_ON    = "✅ העתקת טקסט: כן"
BTN_TEXT_TOGGLE_OFF   = "❌ העתקת טקסט: לא"
BTN_CONTINUOUS_ON     = "🔄 סנכרון רציף: כן"
BTN_CONTINUOUS_OFF    = "⏹ סנכרון רציף: לא"
BTN_STOP_LISTENING    = "⏸ עצור האזנה"
BTN_START_LISTENING   = "🟢 הפעל האזנה"
BTN_WZD_ALL_ACCOUNTS  = "🤖 כל החשבונות"
BTN_WZD_ACCOUNTS_DONE = "✔ חזור לסיכום"
BTN_SAVE_DRAFT      = "💾 שמור כטיוטה"
BTN_TRANSFER_STATS  = "📊 סטטיסטיקות העברות"
BTN_SCAN_DUPES      = "🔍 סרוק כפילויות"
BTN_VIEW_SCAN       = "📄 הצג דוח סריקה"
BTN_DELETE_DUPES    = "🗑 מחק כפילויות"
BTN_RESCAN          = "🔄 סרוק מחדש"
BTN_RETRY_SCAN      = "🔄 נסה שוב"

# ── Screen titles ──────────────────────────────────────────────────────────────

TITLE_MAIN_MENU       = "🏠 <b>מיריוקס — לוח בקרה</b>"
TITLE_JOBS            = "📂 <b>משימות</b>"
TITLE_JOB_DETAIL      = "📋 <b>פרטי משימה</b>"
TITLE_NEW_JOB         = "➕ <b>משימה חדשה</b>"
TITLE_EDIT_JOB        = "✏️ <b>עריכת משימה</b>"
TITLE_SOURCES         = "📡 <b>ערוצי מקור</b>"
TITLE_SOURCE_DETAIL   = "📡 <b>פרטי מקור</b>"
TITLE_DESTINATIONS    = "📤 <b>ערוצי יעד</b>"
TITLE_DEST_DETAIL     = "📤 <b>פרטי יעד</b>"
TITLE_BLOCKED_WORDS   = "🚫 <b>מילים חסומות</b>"
TITLE_ADMINS          = "👥 <b>מנהלים</b>"
TITLE_SETTINGS        = "⚙️ <b>הגדרות</b>"
TITLE_CONFIRM_DELETE  = "⚠️ <b>אישור מחיקה</b>"
TITLE_CONFIRM_CANCEL  = "⚠️ <b>אישור ביטול</b>"
TITLE_CONFIRM_CLEAR   = "⚠️ <b>אישור מחיקת כל המילים</b>"
TITLE_ERROR           = "❌ <b>שגיאה</b>"
TITLE_SCAN_REPORT          = "🔍 <b>דוח כפילויות</b>"
TITLE_SCAN_PICKER          = "🔍 <b>סריקת כפילויות</b>"
TITLE_SCAN_CHANNEL_MENU    = "🔍 <b>תפריט ערוץ</b>"
TITLE_CONFIRM_DELETE_DUPES = "⚠️ <b>אישור מחיקת כפילויות</b>"
BTN_SCAN_DUPES_MENU        = "🔍 סריקת כפילויות"

# ── Userbot accounts ───────────────────────────────────────────────────────────

BTN_USERBOTS          = "🤖 חשבונות יוזרבוט"
BTN_ADD_USERBOT       = "➕ הוסף חשבון"
BTN_ENABLE_USERBOT    = "✅ הפעל"
BTN_DISABLE_USERBOT   = "⏸ השבת"
BTN_REMOVE_USERBOT    = "🗑 הסר חשבון"
BTN_YES_REMOVE        = "✅ כן, הסר"

TITLE_USERBOTS        = "🤖 <b>חשבונות יוזרבוט</b>"
TITLE_USERBOT_DETAIL  = "🤖 <b>פרטי חשבון</b>"
TITLE_ADD_USERBOT     = "🤖 <b>הוספת חשבון יוזרבוט</b>"

USERBOT_STATUS_LABELS: dict[str, str] = {
    "active":       "✅ פעיל",
    "inactive":     "⏸ מושבת",
    "unauthorized": "🔑 לא מאושר",
    "error":        "❌ שגיאה",
}

PROMPT_USERBOT_PHONE = (
    f"{TITLE_ADD_USERBOT}\n\n"
    "שלב 1/3 — הזן מספר טלפון עם קידומת מדינה:\n"
    "<i>לדוגמה: +972501234567</i>"
)

PROMPT_USERBOT_CODE = (
    f"{TITLE_ADD_USERBOT}\n\n"
    "שלב 2/3 — נשלח קוד לאפליקציית טלגרם של המספר.\n"
    "הזן את הקוד שקיבלת:\n\n"
    "<i>טיפ: אם טלגרם חוסם הדבקה של הקוד, הזן אותו עם רווחים (1 2 3 4 5) — נתעלם מהם.</i>"
)

PROMPT_USERBOT_2FA = (
    f"{TITLE_ADD_USERBOT}\n\n"
    "שלב 3/3 — לחשבון זה מוגדרת אימות דו-שלבי.\n"
    "הזן את סיסמת ה-2FA:"
)


def userbot_list_text(userbots: list) -> str:
    if not userbots:
        return (
            f"{TITLE_USERBOTS}\n\n"
            "אין חשבונות יוזרבוט.\n\n"
            "<i>הוסף חשבון כדי שהמערכת תוכל להעתיק הודעות. "
            "ככל שיש יותר חשבונות פעילים — יותר משימות ירוצו במקביל.</i>"
        )
    active = sum(1 for u in userbots if u.status == "active")
    lines = [
        f"{TITLE_USERBOTS}\n",
        f"סה\"כ: <b>{len(userbots)}</b> | פעילים: <b>{active}</b>",
        f"\n<i>משימות מתחלקות אוטומטית בין {active} החשבונות הפעילים "
        f"({active} משימות במקביל).</i>\n" if active else "\n<i>אין חשבון פעיל — משימות לא ירוצו.</i>\n",
        "בחר חשבון לניהול:",
    ]
    return "\n".join(lines)


def userbot_detail_text(ub) -> str:
    status = USERBOT_STATUS_LABELS.get(ub.status, ub.status)
    uname = f"@{esc(ub.username)}" if ub.username else "—"
    tid = str(ub.telegram_id) if ub.telegram_id else "—"
    default_line = "\n⭐ חשבון ברירת מחדל (מוגדר ב-.env, לא ניתן להסרה)" if ub.is_default else ""
    err = f"\n\n⚠️ {esc(ub.error_message[:200])}" if ub.error_message else ""
    return (
        f"{TITLE_USERBOT_DETAIL}: <b>{esc(ub.display())}</b>\n\n"
        f"טלפון: <code>{esc(ub.phone or '—')}</code>\n"
        f"שם משתמש: {uname}\n"
        f"מזהה: {tid}\n"
        f"סטטוס: {status}\n"
        f"session: <code>{esc(ub.session_name)}</code>\n"
        f"נוסף: {_fmt_dt(ub.added_at)}\n"
        f"נראה לאחרונה: {_fmt_dt(ub.last_seen)}"
        f"{default_line}"
        f"{err}"
    )


def confirm_remove_userbot_text(name: str) -> str:
    return (
        f"⚠️ <b>אישור הסרת חשבון</b>\n\n"
        f"להסיר את החשבון <b>{esc(name)}</b>?\n\n"
        "המשימות שהחשבון מריץ יוחזרו לתור ויחולקו לחשבונות אחרים.\n"
        "קובץ ה-session יימחק — כדי להחזיר את החשבון תצטרך להתחבר מחדש."
    )


def userbot_added_text(name: str) -> str:
    return (
        f"✅ <b>החשבון נוסף בהצלחה</b>\n\n"
        f"🤖 {esc(name)}\n\n"
        "החשבון פעיל ויתחיל לקבל משימות תוך כמה שניות."
    )
BTN_START_SCAN             = "▶️ התחל סריקה"
BTN_STOP_SCAN              = "⏹ עצור סריקה"
BTN_RESET_SCAN             = "🗑 אפס נתונים"
BTN_CONFIRM_RESET          = "✅ כן, אפס"
BTN_DEL_SCAN               = "🗑 מחיקת סריקה"
BTN_CONFIRM_DEL_SCAN       = "✅ כן, מחק סריקה"

# ── Main menu ──────────────────────────────────────────────────────────────────

def main_menu_text(
    worker_status: str,
    active_job: "Job | None",
    active_scan: dict | None = None,
    active_delete_job: dict | None = None,
) -> str:
    ws_label = WORKER_STATUS_LABELS.get(worker_status, worker_status)
    if active_job:
        eta_str = ""
        if active_job.total_messages > 0 and not (active_job.continuous and active_job.backfill_done):
            rem = max(0, active_job.total_messages - (active_job.copied_count + active_job.skipped_count + active_job.failed_count))
            if rem > 0 and active_job.status in ("running", "pending", "waiting_retry"):
                eta_sec = _estimate_copy_time(rem)
                eta_str = f" | משוער לסיום: {_format_eta(eta_sec)}"

        job_line = (
            f"📋 משימה פעילה: <b>{esc(active_job.name)}</b> "
            f"[{job_status_label(active_job)}]\n"
            f"   הועתקו: {active_job.copied_count} | דולגו: {active_job.skipped_count}{eta_str}"
        )
    else:
        job_line = "אין משימה פעילה כרגע"

    scan_line = ""
    if active_scan:
        st = "▶️ סורק" if active_scan.get("status") == "running" else "⏳ ממתין לסריקה"
        c = active_scan.get("messages_scanned", 0)
        t = active_scan.get("total_messages", 0)
        pct = f"({int(c/t*100)}%) " if t else ""
        
        eta_str = ""
        if t > 0:
            rem = max(0, t - c)
            if rem > 0 and active_scan.get("status") in ("running", "pending"):
                eta_sec = _estimate_scan_time(rem)
                eta_str = f"\n   ⏱ זמן משוער: {_format_eta(eta_sec)}"

        title = active_scan.get("channel_title") or active_scan.get("channel_ref") or "?"
        scan_line = f"\n\n🔍 כפילויות: {st} <b>{pct}{c:,}</b> מתוך <b>{t:,}</b> הודעות ({esc(title)}){eta_str}"

    delete_line = ""
    if active_delete_job:
        st = "▶️ מוחק" if active_delete_job.get("status") == "running" else "⏳ ממתין למחיקה"
        d = active_delete_job.get("deleted_count", 0)
        title = active_delete_job.get("channel_title") or active_delete_job.get("channel_ref") or "?"
        delete_line = f"\n\n🗑 מחיקה: {st} <b>{d:,}</b> נמחקו ({esc(title)})"

    return (
        f"{TITLE_MAIN_MENU}\n\n"
        f"🖥 עובד: {ws_label}\n"
        f"{job_line}"
        f"{scan_line}"
        f"{delete_line}"
    )


# ── Job list ───────────────────────────────────────────────────────────────────

def jobs_list_text(jobs: list["Job"]) -> str:
    has_jobs = bool(jobs)
    if not has_jobs:
        return f"{TITLE_JOBS}\n\nאין משימות עדיין."
    return f"{TITLE_JOBS}\n\nבחר משימה מהרשימה:"


def scan_row_text(scan: dict) -> str:
    """One-line label for a scan in the unified job list."""
    icon = SCAN_STATUS_ICONS.get(scan.get("status", ""), "🔍")
    channel = scan.get("channel_title") or scan.get("channel_ref") or "?"
    return f"🔍 {channel[:38]} {icon}"


# ── Job detail ─────────────────────────────────────────────────────────────────

def job_detail_text(
    job: "Job",
    source: "Source | None",
    dests: "list[Destination | None]",
    queue_position: "int | None" = None,
) -> str:
    src_str = source.display() if source else f"[#{job.source_id}]"
    dst_str = ", ".join(
        d.display() if d else f"[#{i}]"
        for i, d in zip(job.destination_id_list(), dests)
    )
    dst_label = "יעד" if len(dests) <= 1 else "יעדים (אקראי)"
    status_label = job_status_label(job)
    mode_label = MODE_LABELS.get(job.mode, job.mode)

    filter_str = "כן" if job.use_blocked_words else "לא"
    ct_parts = [p.strip() for p in (job.content_types or DEFAULT_CONTENT_TYPES).split(",") if p.strip()]
    ct_map = {"image": "תמונות", "video": "סרטונים", "file": "קבצים", "text": "טקסט"}
    ct_str = ", ".join(ct_map[p] for p in ("image", "video", "file", "text") if p in ct_parts) or "—"

    params_line = ""
    if job.mode == "date_range":
        params_line = f"\nטווח: {job.date_from} – {job.date_to}"
    elif job.mode == "id_range":
        params_line = f"\nטווח מזהים: #{job.id_from} – #{job.id_to}"
    elif job.mode == "single_id":
        params_line = f"\nמזהה: #{job.single_message_id}"

    if job.continuous:
        phase = (
            "🟢 שלב 2/2 — מאזין להודעות חדשות"
            if job.backfill_done
            else "▶️ שלב 1/2 — מעתיק היסטוריה, יאזין בסיום"
        )
        params_line += f"\n🔄 סנכרון רציף: {phase}"

    queue_line = f"\nמיקום בתור: #{queue_position}" if queue_position else ""

    allowed_line = _job_allowed_line(job)

    runner_line = _render_runners(job)

    retry_info = ""
    if job.status == "waiting_retry":
        retry_info = f"\n\nניסיון חוזר: {job.retry_count}/{job.max_retries}"
        if job.next_retry_at:
            retry_info += f" (ב-{_fmt_dt(job.next_retry_at)})"

    error_info = ""
    if job.error_message:
        error_info = f"\n\n⚠️ שגיאה אחרונה:\n<code>{esc(job.error_message[:200])}</code>"

    checkpoint = f"#{job.last_processed_id}" if job.last_processed_id else "—"
    started  = _fmt_dt(job.started_at)
    finished = _fmt_dt(job.completed_at)
    updated  = _fmt_dt(job.last_updated_at)

    report_line = ""
    if job.report_url:
        report_line = f"\n\n📋 <a href=\"{job.report_url}\">דוח שגיאות / דילוגים</a>"

    eta_str = ""
    # Only the listening phase is open-ended. The backfill is a bounded copy, so
    # an ETA is meaningful there.
    if job.total_messages > 0 and not (job.continuous and job.backfill_done):
        rem = max(0, job.total_messages - (job.copied_count + job.skipped_count + job.failed_count))
        if rem > 0 and job.status in ("running", "pending", "waiting_retry", "paused"):
            eta_sec = _estimate_copy_time(rem)
            eta_str = f"\n⏱ זמן משוער לסיום (אופטימלי): {_format_eta(eta_sec)}"

    return (
        f"{status_label} {esc(job.name)}\n"
        f"\n"
        f"שם: {esc(job.name)}\n"
        f"מזהה: {job.id}\n"
        f"\n"
        f"מקור: {esc(src_str)}\n"
        f"{dst_label}: {esc(dst_str)}\n"
        f"מצב: {mode_label}{params_line}\n"
        f"תוכן: {ct_str}\n"
        f"סינון מילים: {filter_str}\n"
        f"{allowed_line}"
        f"סטטוס: {status_label}{queue_line}{runner_line}{eta_str}"
        f"\n"
        f"\nתוצאות:\n"
        f"הועתקו: {job.copied_count}\n"
        f"דולגו: {job.skipped_count}\n"
        f"נכשלו: {job.failed_count}\n"
        f"נקודת המשך: {checkpoint}"
        f"\n"
        f"\nזמנים:\n"
        f"התחלה: {started}\n"
        f"סיום: {finished}\n"
        f"עדכון אחרון: {updated}"
        f"{retry_info}"
        f"{error_info}"
        f"{report_line}"
    )


# ── Job creation wizard ────────────────────────────────────────────────────────

def _job_allowed_line(job: "Job") -> str:
    """A 'restricted to accounts X, Y' line, or '' when the job runs on all."""
    allowed = job.allowed_ids()
    if not allowed:
        return ""
    from app.repositories import userbot_repo

    names = [
        esc(ub.display())
        for ub in (userbot_repo.get_by_id(i) for i in sorted(allowed))
        if ub
    ]
    if not names:
        return ""
    return f"חשבונות מריצים: {', '.join(names)}\n"


def _render_runners(job: "Job") -> str:
    """
    Which account(s) are on this job, and how far a parallel run has got.

    A sharded job has no single owner: the leader in assigned_userbot_id is just
    the account that claimed it out of the queue, while any number of others may
    be working chunks of it right now. Reading the accounts off the chunks is the
    only way to show who is actually copying.
    """
    from app.repositories import job_chunk_repo, userbot_repo

    done, total = job_chunk_repo.progress(job.id)
    if total == 0:
        if not job.assigned_userbot_id:
            return ""
        ub = userbot_repo.get_by_id(job.assigned_userbot_id)
        return f"\n🤖 מבוצע ע\"י: {esc(ub.display())}" if ub else ""

    working_ids = set(job_chunk_repo.active_userbot_ids(job.id))
    if job.assigned_userbot_id:
        working_ids.add(job.assigned_userbot_id)
    names = [
        esc(ub.display())
        for ub in (userbot_repo.get_by_id(i) for i in sorted(working_ids))
        if ub
    ]
    who = ", ".join(names) if names else "—"
    line = f"\n🤖 מבוצע ע\"י: {who}"
    if len(names) > 1:
        line += f" <b>(מקבילי ×{len(names)})</b>"
    return line + f"\n🧩 בלוקים: {done:,}/{total:,}"


def wizard_header(step: int, total: int, partial: dict) -> str:
    lines = [f"<b>שלב {step}/{total}</b>"]
    if partial.get("name"):
        lines.append(f"שם: {esc(partial['name'])}")
    names = partial.get("source_names", [])
    if names:
        if len(names) == 1:
            lines.append(f"מקור: {esc(names[0])}")
        else:
            lines.append(f"מקורות: {len(names)} נבחרו")
    if partial.get("dest_name"):
        lines.append(f"יעד: {esc(partial['dest_name'])}")
    if partial.get("mode"):
        lines.append(f"מצב: {MODE_LABELS.get(partial['mode'], partial['mode'])}")
    return "\n".join(lines)


WIZARD_SELECT_CONTENT_TYPES = "בחר אילו סוגי תוכן להעתיק:"
WIZARD_ENTER_NAME = "הזן שם למשימה:"
WIZARD_SELECT_SOURCE = "בחר ערוצי מקור (ניתן לבחור כמה) — לחץ ✔ סיים בחירה לאחר הבחירה:"
WIZARD_SELECT_DEST = "בחר ערוץ יעד:"
WIZARD_SELECT_MODE = "בחר מצב העתקה:"
WIZARD_ENTER_DATE_FROM = "הזן תאריך התחלה (DD/MM/YYYY או DD/MM/YYYY HH:MM):"
WIZARD_ENTER_DATE_TO = "הזן תאריך סיום (DD/MM/YYYY או DD/MM/YYYY HH:MM):"
WIZARD_ENTER_ID_FROM = "הזן מזהה הודעה ראשונה (מספר):"
WIZARD_ENTER_ID_TO = "הזן מזהה הודעה אחרונה (מספר):"
WIZARD_ENTER_SINGLE_ID = "הזן מזהה ההודעה:"
WIZARD_FILTER_AND_CONFIRM = "בדוק את הפרטים ואשר:"
WIZARD_SELECT_ACCOUNTS = (
    "בחר אילו חשבונות יריצו את המשימה:\n"
    "<i>סמן ✅ עבור החשבונות המורשים. אם כולם מסומנים — המשימה תרוץ על כל "
    "החשבונות הפעילים (ברירת מחדל). בחירת חשבון אחד תצמיד את המשימה אליו בלבד.</i>"
)

NO_SOURCES_YET = "לא הוגדרו מקורות עדיין. הוסף מקור תחילה."
NO_DESTINATIONS_YET = "לא הוגדרו יעדים עדיין. הוסף יעד תחילה."


def _wizard_allowed_names(partial: dict) -> "list[str] | None":
    """
    Names of the accounts allowed to run the job, or None when unrestricted.

    'Unrestricted' covers both the untouched default and the case where every
    active account is still selected — either way the job runs on all of them.
    """
    from app.repositories import userbot_repo

    active = userbot_repo.get_active()
    active_ids = {u.id for u in active}
    selected = partial.get("allowed_ubs")
    if not selected or set(selected) >= active_ids:
        return None
    return [u.display() for u in active if u.id in selected]


def wizard_accounts_label(partial: dict) -> str:
    """Short label for the summary-screen accounts button."""
    names = _wizard_allowed_names(partial)
    if names is None:
        return "🤖 חשבונות: כל החשבונות"
    return f"🤖 חשבונות: {len(names)} נבחרו"


def wizard_summary_text(partial: dict, word_count: int) -> str:
    mode = partial.get("mode", "")
    mode_label = MODE_LABELS.get(mode, mode)
    filter_status = f"כן ({word_count} מילים)" if partial.get("use_blocked_words", True) else "לא"
    group_status = "כן" if partial.get("group_media", True) else "לא"
    text_status = "כן" if partial.get("copy_text", True) else "לא"
    continuous = partial.get("continuous", False)

    ct_set = partial.get("content_types", set(ALL_CONTENT_TYPES))
    ct_labels = []
    if "image" in ct_set:
        ct_labels.append("🖼 תמונות")
    if "video" in ct_set:
        ct_labels.append("🎬 סרטונים")
    if "file" in ct_set:
        ct_labels.append("📎 קבצים")
    if "text" in ct_set:
        ct_labels.append("💬 טקסט")
    content_types_str = ", ".join(ct_labels) if ct_labels else "—"

    params = ""
    if mode == "date_range":
        params = f"\nטווח תאריכים: {partial.get('date_from','?')} – {partial.get('date_to','?')}"
    elif mode == "id_range":
        params = f"\nטווח מזהים: #{partial.get('id_from','?')} – #{partial.get('id_to','?')}"
    elif mode == "single_id":
        params = f"\nמזהה הודעה: #{partial.get('single_id','?')}"

    src_names = partial.get("source_names", [])
    if len(src_names) == 1:
        src_str = esc(src_names[0])
    elif len(src_names) > 1:
        src_str = f"{len(src_names)} מקורות: " + ", ".join(esc(n) for n in src_names)
    else:
        src_str = "?"

    allowed_names = _wizard_allowed_names(partial)
    accounts_str = "כל החשבונות" if allowed_names is None else ", ".join(esc(n) for n in allowed_names)

    mode_line = f"🔧 מצב: {mode_label}{params}\n"
    if continuous:
        mode_line += (
            "🔄 סנכרון רציף: <b>כן</b>\n"
            "<i>שלב 1: יעתיק את ההיסטוריה לפי המצב שנבחר למעלה.\n"
            "שלב 2: ימשיך להאזין ויעתיק כל הודעה חדשה עם הגעתה.\n"
            "רץ ברקע בעדיפות נמוכה ולא מסתיים עד שתעצור אותו.</i>\n"
        )

    return (
        f"{TITLE_NEW_JOB}\n\n"
        f"📝 שם: <b>{esc(partial.get('name','?'))}</b>\n"
        f"📡 מקור: {src_str}\n"
        f"📤 יעד: {esc(partial.get('dest_name','?'))}\n"
        f"{mode_line}"
        f"📁 סוגי תוכן: {content_types_str}\n"
        f"🚫 סינון מילים: {filter_status}\n"
        f"📦 שליחה במרוכז: {group_status}\n"
        f"📝 העתקת טקסט: {text_status}\n"
        f"🤖 חשבונות מריצים: {accounts_str}\n\n"
        f"אשר כדי לשמור כטיוטה."
    )


def _content_types_display(content_types: str) -> str:
    """Human-readable, ordered list of content-type labels for a job."""
    parts = [p.strip() for p in (content_types or DEFAULT_CONTENT_TYPES).split(",") if p.strip()]
    ct_map = {"image": "🖼 תמונות", "video": "🎬 סרטונים", "file": "📎 קבצים", "text": "💬 טקסט"}
    labels = [ct_map[p] for p in ("image", "video", "file", "text") if p in parts]
    return ", ".join(labels) or "—"


def job_edit_text(job: "Job", word_count: int, accounts_str: str) -> str:
    """Editable-settings summary for a draft/paused job. Source/dest/range are fixed."""
    filter_status = f"כן ({word_count} מילים)" if job.use_blocked_words else "לא"
    group_status = "כן" if job.group_media else "לא"
    text_status = "כן" if job.copy_text else "לא"
    continuous_status = "כן" if job.continuous else "לא"
    status_label = job_status_label(job)
    checkpoint = f"#{job.last_processed_id}" if job.last_processed_id else "—"

    return (
        f"{TITLE_EDIT_JOB}\n\n"
        f"📋 <b>{esc(job.name)}</b> — {status_label}\n"
        f"נקודת המשך שמורה: {checkpoint}\n\n"
        f"<b>הגדרות ניתנות לעריכה:</b>\n"
        f"🤖 חשבונות מריצים: {accounts_str}\n"
        f"📁 סוגי תוכן: {_content_types_display(job.content_types)}\n"
        f"🚫 סינון מילים: {filter_status}\n"
        f"📦 שליחה במרוכז: {group_status}\n"
        f"📝 העתקת טקסט: {text_status}\n"
        f"🔄 סנכרון רציף: {continuous_status}\n\n"
        f"<i>מקור/מצב/טווח אינם ניתנים לשינוי — כדי לשמר את נקודת ההמשך "
        f"וההיסטוריה. לאחר העריכה לחץ 'המשך משימה' כדי להמשיך מהמקום שנעצר.</i>"
    )


def job_edit_accounts_text(job: "Job") -> str:
    """The account allow-list picker, in the edit context."""
    return (
        f"{TITLE_EDIT_JOB}\n\n"
        f"📋 <b>{esc(job.name)}</b>\n\n"
        f"{WIZARD_SELECT_ACCOUNTS}"
    )


def job_edit_destinations_text(job: "Job") -> str:
    """The destination picker, in the edit context."""
    return (
        f"{TITLE_EDIT_JOB}\n\n"
        f"📋 <b>{esc(job.name)}</b>\n\n"
        "בחר את ערוצי היעד של המשימה:\n"
        "<i>כשנבחרים כמה יעדים, כל הודעה נשלחת לאחד מהם באקראי "
        "(אלבום נשלח שלם לאותו יעד). חסימת כפילויות נאכפת מול כל היעדים יחד.</i>"
    )


def job_edit_content_types_text(job: "Job") -> str:
    return (
        f"{TITLE_EDIT_JOB}\n\n"
        f"📋 <b>{esc(job.name)}</b>\n\n"
        f"{WIZARD_SELECT_CONTENT_TYPES}"
    )


def job_allowed_accounts_str(job: "Job") -> str:
    """'כל החשבונות' or the comma-joined names of the job's allow-list."""
    allowed = job.allowed_ids()
    if not allowed:
        return "כל החשבונות"
    from app.repositories import userbot_repo

    names = [
        esc(ub.display())
        for ub in (userbot_repo.get_by_id(i) for i in sorted(allowed))
        if ub
    ]
    return ", ".join(names) if names else "כל החשבונות"


DAILY_LIMIT = 20_000


def moon_progress_bar(percent: float, total_cells: int = 10) -> str:
    """Moon-phase progress bar. Fills right→left (RTL): 🌑 empty, 🌒🌓🌔 partial, 🌕 full."""
    progress = max(0.0, min(100.0, percent)) / 100
    filled = progress * total_cells
    full_count = int(filled)
    remainder = filled - full_count

    if full_count < total_cells and remainder > 0:
        if remainder >= 0.67:
            partial = "🌔"
        elif remainder >= 0.34:
            partial = "🌓"
        else:
            partial = "🌒"
        empty_count = total_cells - full_count - 1
    else:
        partial = ""
        empty_count = total_cells - full_count

    return "🌑" * empty_count + partial + "🌕" * full_count


def transfer_stats_text(stats: dict, userbots: list["Userbot"]) -> str:
    today_total = stats["since_midnight"]
    
    active_userbots = [u for u in userbots if u.status == "active"]
    shared_limit = len(active_userbots) * DAILY_LIMIT if active_userbots else DAILY_LIMIT
    
    pct_total = min(today_total / shared_limit * 100, 100) if shared_limit else 0
    bar_total = moon_progress_bar(pct_total)
    remaining_total = max(shared_limit - today_total, 0)
    limit_line_total = (
        f"\n{bar_total} {pct_total:.1f}%\n"
        f"נוצלו: <b>{today_total:,}</b> / {shared_limit:,}  |  נותרו: <b>{remaining_total:,}</b>"
    )
    
    text = (
        "📊 <b>סטטיסטיקות העברות (כולל)</b>\n\n"
        f"🕐 שעה אחרונה: <b>{stats['last_hour']:,}</b> הודעות\n"
        f"📅 היום (מחצות): <b>{stats['since_midnight']:,}</b> הודעות\n"
        f"📆 24 שעות אחרונות: <b>{stats['last_24h']:,}</b> הודעות\n"
        f"\n<b>מכסה כוללת: {shared_limit:,} הודעות</b>"
        f" <i>({DAILY_LIMIT:,} לכל חשבון × {len(active_userbots) or 1})</i>\n"
        f"{limit_line_total}\n"
    )

    if userbots:
        text += "\n👥 <b>חלוקה לפי חשבון:</b>\n"
        for ub in userbots:
            ub_stats = stats.get("userbots", {}).get(ub.id, {"last_hour": 0, "since_midnight": 0, "last_24h": 0})
            ub_today = ub_stats["since_midnight"]
            
            if ub.status != "active":
                status_str = f" ({STATUS_LABELS.get(ub.status, ub.status)})"
                text += f"\n🔸 <b>{ub.display()}</b>{status_str}\n   הועברו היום: <b>{ub_today:,}</b> הודעות\n"
            else:
                pct = min(ub_today / DAILY_LIMIT * 100, 100)
                bar = moon_progress_bar(pct)
                text += (
                    f"\n🔹 <b>{ub.display()}</b>\n"
                    f"   היום: <b>{ub_today:,}</b> / {DAILY_LIMIT:,}  |  נותרו: <b>{max(DAILY_LIMIT - ub_today, 0):,}</b>\n"
                    f"   {bar}\n"
                )
            
    return text


# ── Sources / destinations ─────────────────────────────────────────────────────

def source_list_text(sources: list["Source"]) -> str:
    if not sources:
        return f"{TITLE_SOURCES}\n\nלא הוגדרו מקורות עדיין."
    return f"{TITLE_SOURCES}\n\nבחר מקור מהרשימה:"


def source_detail_text(source: "Source", access: list["ChannelAccessRow"] | None = None) -> str:
    title = source.title or "—"
    rid = str(source.resolved_id) if source.resolved_id else "⏳ ממתין לאימות"
    return (
        f"{TITLE_SOURCE_DETAIL}: <b>{esc(source.name)}</b>\n\n"
        f"הפניה: <code>{esc(source.channel_ref)}</code>\n"
        f"כותרת: {esc(title)}\n"
        f"מזהה: {rid}\n"
        f"גישה: {_access_summary(source, access or [])}\n"
        + _channel_access_lines(access or [])
        + _channel_extra_lines(source) +
        f"נוסף: {_fmt_dt(source.created_at)}"
    )


def scan_picker_text(dests: list) -> str:
    if not dests:
        return (
            f"{TITLE_SCAN_PICKER}\n\n"
            "לא הוגדרו ערוצי יעד. לחץ <b>הזן ידנית</b> כדי להזין כתובת ערוץ."
        )
    return (
        f"{TITLE_SCAN_PICKER}\n\n"
        "בחר ערוץ יעד לסריקה, או לחץ <b>הזן ידנית</b> להזנת כתובת ערוץ אחרת:\n"
        "<i>(סריקה מאתרת קבצי מדיה כפולים בערוץ)</i>"
    )


def scan_report_text(scan: dict, channel_name: str) -> str:
    status = scan.get("status", "")
    scanned = scan.get("messages_scanned", 0)
    total = scan.get("total_messages", 0)
    groups = scan.get("duplicate_groups", 0)
    wasted = scan.get("wasted_count", 0)
    report_url = scan.get("report_url")
    error = scan.get("error_msg")

    header = f"{TITLE_SCAN_REPORT} — <b>{esc(channel_name)}</b>\n\n"

    if status == "pending":
        return header + "⏳ ממתין לתור (הוורקר יתחיל בקרוב)..."

    if status == "running":
        pct = int(scanned / total * 100) if total else 0
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        
        eta_str = ""
        if total > 0:
            rem = max(0, total - scanned)
            if rem > 0:
                eta_sec = _estimate_scan_time(rem)
                eta_str = f"⏱ זמן משוער לסיום (אופטימלי): {_format_eta(eta_sec)}\n\n"

        return (
            header
            + f"▶️ סורק...\n"
            + f"[{bar}] {pct}%\n"
            + f"{scanned:,} / {total:,} הודעות\n\n"
            + eta_str
            + "לחץ 🔄 לעדכון"
        )

    if status == "failed":
        reason = esc(error or "שגיאה לא ידועה")
        return header + f"❌ הסריקה נכשלה / הופסקה\n\n{reason}\n\nלחץ ▶️ להתחלה מחדש"

    # done
    if groups == 0:
        return (
            header
            + f"✅ הסריקה הושלמה\n\n"
            + f"📊 נסרקו: <b>{scanned:,}</b> הודעות\n"
            + "🎉 לא נמצאו כפילויות!"
        )

    lines = [
        header,
        "✅ הסריקה הושלמה\n",
        f"📊 נסרקו: <b>{scanned:,}</b> הודעות",
        f"🔁 קבוצות כפולות: <b>{groups:,}</b>",
        f"🗑 ניתן למחוק: <b>{wasted:,}</b> הודעות",
    ]
    if report_url:
        lines.append(f"\n📄 <a href=\"{report_url}\">דוח מפורט עם קישורים</a>")
    return "\n".join(lines)


def confirm_delete_dupes_text(wasted: int) -> str:
    return (
        f"{TITLE_CONFIRM_DELETE_DUPES}\n\n"
        f"פעולה זו תמחק <b>{wasted:,}</b> הודעות כפולות מהערוץ.\n"
        "ההודעה הישנה ביותר בכל קבוצה תישמר.\n\n"
        "⚠️ פעולה זו אינה הפיכה!"
    )


def scan_channel_menu_text(channel_title: str) -> str:
    return f"{TITLE_SCAN_CHANNEL_MENU} — <b>{esc(channel_title)}</b>\n\nמה ברצונך לעשות?"


def confirm_del_scan_text() -> str:
    return "⚠️ האם ברצונך למחוק סריקה זו?\n\nהפעולה תמחק את כל הנתונים של הסריקה הספציפית הזו."



def dest_list_text(dests: list["Destination"]) -> str:
    if not dests:
        return f"{TITLE_DESTINATIONS}\n\nלא הוגדרו יעדים עדיין."
    return f"{TITLE_DESTINATIONS}\n\nבחר יעד מהרשימה:"


def dest_detail_text(dest: "Destination", access: list["ChannelAccessRow"] | None = None) -> str:
    title = dest.title or "—"
    rid = str(dest.resolved_id) if dest.resolved_id else "⏳ ממתין לאימות"
    return (
        f"{TITLE_DEST_DETAIL}: <b>{esc(dest.name)}</b>\n\n"
        f"הפניה: <code>{esc(dest.channel_ref)}</code>\n"
        f"כותרת: {esc(title)}\n"
        f"מזהה: {rid}\n"
        f"גישה: {_access_summary(dest, access or [])}\n"
        + _channel_access_lines(access or [])
        + _channel_extra_lines(dest) +
        f"נוסף: {_fmt_dt(dest.created_at)}"
    )


def _access_summary(ch, access: list["ChannelAccessRow"]) -> str:
    """One-line verdict for the channel, across every active userbot account."""
    if not access:
        return "⏳ אין חשבונות יוזרבוט פעילים"
    granted = sum(1 for r in access if r.has_access)
    pending = sum(1 for r in access if r.has_access is None)
    if granted:
        return f"✅ נגיש ל-{granted} מתוך {len(access)} חשבונות"
    if pending:
        return "⏳ ממתין לבדיקה"
    return "❌ " + esc(ch.validation_error or "אף חשבון אינו יכול לגשת לערוץ")


def _channel_access_lines(access: list["ChannelAccessRow"]) -> str:
    """Per-account access report. Returns a string ending with \n, or '' when empty."""
    if not access:
        return ""
    lines = ["\n🤖 <b>גישה לפי חשבון:</b>"]
    for row in access:
        if row.has_access is None:
            lines.append(f"⏳ {esc(row.userbot_label)} — ממתין לבדיקה")
        elif row.has_access:
            lines.append(f"✅ {esc(row.userbot_label)}")
        else:
            reason = esc((row.error or "אין גישה")[:80])
            lines.append(f"❌ {esc(row.userbot_label)} — {reason}")
    return "\n".join(lines) + "\n"


def _channel_extra_lines(ch) -> str:
    """Build extra-info lines for a Source or Destination. Returns a string ending with \n."""
    lines = ""
    if ch.channel_type:
        lines += f"סוג: {esc(ch.channel_type)}"
        if ch.verified:
            lines += " ✅ מאומת"
        lines += "\n"
    if ch.username:
        lines += f"@: @{esc(ch.username)}\n"
    if ch.participants_count is not None:
        lines += f"👥 מנויים: {ch.participants_count:,}\n"
    if ch.about:
        about_short = ch.about[:120] + ("…" if len(ch.about) > 120 else "")
        lines += f"📝 תיאור: {esc(about_short)}\n"
    if ch.total_messages is not None or ch.photos_count is not None:
        stats = []
        if ch.total_messages is not None:
            stats.append(f"📨 {ch.total_messages:,} הודעות")
        if ch.photos_count is not None:
            stats.append(f"🖼 {ch.photos_count:,} תמונות")
        if ch.videos_count is not None:
            stats.append(f"🎬 {ch.videos_count:,} סרטונים")
        if ch.docs_count is not None:
            stats.append(f"📁 {ch.docs_count:,} קבצים")
        lines += " | ".join(stats) + "\n"
    if lines:
        lines = "\n" + lines + "\n"
    return lines


PROMPT_SOURCE_NAME = f"{TITLE_SOURCES}\n\nשלב 1 — הזן שם כינוי למקור:"
PROMPT_SOURCE_REF  = f"{TITLE_SOURCES}\n\nשלב 2 — הזן @username, מזהה מספרי, או קישור t.me/:"
PROMPT_DEST_NAME   = f"{TITLE_DESTINATIONS}\n\nשלב 1 — הזן שם כינוי ליעד:"
PROMPT_DEST_REF    = f"{TITLE_DESTINATIONS}\n\nשלב 2 — הזן @username, מזהה מספרי, או קישור t.me/:"
CONFIRM_DELETE_SOURCE = "האם למחוק את המקור? פעולה זו אינה הפיכה."
CONFIRM_DELETE_DEST   = "האם למחוק את היעד? פעולה זו אינה הפיכה."


# ── Blocked words ──────────────────────────────────────────────────────────────

def blocked_words_text(words: list["BlockedWord"]) -> str:
    if not words:
        return f"{TITLE_BLOCKED_WORDS}\n\nאין מילים חסומות."
    lines = [f"{TITLE_BLOCKED_WORDS}\n", f"סה\"כ: {len(words)} מילים\n"]
    for w in words:
        lines.append(f"• {esc(w.word)}")
    return "\n".join(lines)


PROMPT_BLOCKED_WORD = f"{TITLE_BLOCKED_WORDS}\n\nהזן מילה לחסימה:"
CONFIRM_CLEAR_WORDS = "האם למחוק את כל המילים החסומות? פעולה זו אינה הפיכה."


# ── Admins ─────────────────────────────────────────────────────────────────────

def admin_list_text(admins: list["Admin"], bootstrap_ids: list[int]) -> str:
    lines = [f"{TITLE_ADMINS}\n"]
    if not admins and not bootstrap_ids:
        lines.append("אין מנהלים מוגדרים.")
    else:
        for tid in bootstrap_ids:
            lines.append(f"• <code>{tid}</code> — מוגדר ב-config (לא ניתן להסרה)")
        for a in admins:
            if a.telegram_id not in bootstrap_ids:
                uname = f"@{a.username}" if a.username else ""
                lines.append(f"• {uname} <code>{a.telegram_id}</code>")
    lines.append("\n⚠️ מנהלי ה-bootstrap מוגדרים ב-.env ואינם ניתנים להסרה דרך הממשק.")
    return "\n".join(lines)


PROMPT_ADMIN_ID = f"{TITLE_ADMINS}\n\nהזן מזהה Telegram של המנהל החדש (מספר):"
CONFIRM_REMOVE_ADMIN = "האם להסיר מנהל זה?"


# ── Settings ───────────────────────────────────────────────────────────────────

SETTINGS_LABELS: dict[str, str] = {
    "min_delay_ms":         "עיכוב מינימלי (מ\"ש)",
    "max_delay_ms":         "עיכוב מקסימלי (מ\"ש)",
    "flood_wait_buffer_s":  "כיסוי FloodWait (שניות)",
    "max_retries":          "מקסימום ניסיונות חוזרים",
    "heartbeat_interval_s": "מרווח דופק עובד (שניות)",
}

TOGGLE_SETTINGS: dict[str, str] = {
    "group_media":     "קיבוץ תמונות/סרטונים לאלבום (עד 10)",
    "skip_duplicates": "דלג על כפילויות (אל תשלח תוכן שכבר נשלח ליעד)",
}

EDITABLE_SETTINGS = list(SETTINGS_LABELS.keys())

# Toggles that default to OFF when the setting row is missing.
# Everything else defaults to ON, preserving the original behaviour.
TOGGLE_DEFAULT_OFF: frozenset[str] = frozenset({"skip_duplicates"})


def toggle_is_on(settings: dict[str, str], key: str) -> bool:
    default = "0" if key in TOGGLE_DEFAULT_OFF else "1"
    return settings.get(key, default) == "1"


def settings_text(settings: dict[str, str]) -> str:
    lines = [f"{TITLE_SETTINGS}\n"]
    for key, label in SETTINGS_LABELS.items():
        val = settings.get(key, "—")
        lines.append(f"• {label}: <b>{esc(val)}</b>")
    for key, label in TOGGLE_SETTINGS.items():
        status = "✅ פעיל" if toggle_is_on(settings, key) else "❌ כבוי"
        lines.append(f"• {label}: <b>{status}</b>")
    return "\n".join(lines)


def prompt_setting(key: str) -> str:
    label = SETTINGS_LABELS.get(key, key)
    return f"{TITLE_SETTINGS}\n\nהזן ערך חדש עבור <b>{label}</b>:"


# ── Hyper backup ───────────────────────────────────────────────────────────────

BTN_HYPER = "⚡ מצב הייפר"

TITLE_HYPER = "⚡ <b>מצב הייפר — גיבוי אוטומטי</b>"

# Media types a hyper filter can target, in display order.
HYPER_TYPES = ("video", "image", "file", "audio")
HYPER_TYPE_LABELS: dict[str, str] = {
    "video": "🎬 סרטונים",
    "image": "🖼 תמונות",
    "file":  "📎 קבצים",
    "audio": "🎵 אודיו",
}
# Only video/audio carry a duration; a size bound applies to every type.
HYPER_TYPES_WITH_DURATION = frozenset({"video", "audio"})

# Short codes used in callback data ↔ DB column names.
HYPER_FIELD_COLUMNS: dict[str, str] = {
    "minsize": "min_size",
    "maxsize": "max_size",
    "mindur":  "min_duration",
    "maxdur":  "max_duration",
}
HYPER_FIELD_LABELS: dict[str, str] = {
    "minsize": "גודל מינימלי",
    "maxsize": "גודל מקסימלי",
    "mindur":  "אורך מינימלי",
    "maxdur":  "אורך מקסימלי",
}


def fmt_size(num_bytes: "int | None") -> str:
    if not num_bytes:
        return "—"
    mb = num_bytes / (1024 * 1024)
    if mb >= 1024:
        return f"{mb / 1024:.1f}GB"
    return f"{mb:.0f}MB" if mb >= 10 else f"{mb:.1f}MB"


def fmt_duration(seconds: "int | None") -> str:
    if not seconds:
        return "—"
    minutes = seconds / 60
    if minutes >= 1:
        return f"{minutes:.0f} דק׳"
    return f"{seconds} שנ׳"


def _rule_summary(media_type: str, rule: "dict | None") -> str:
    """One-line summary of a type's filter: 'הכל' / 'כבוי' / the active bounds."""
    if rule is None:
        return "הכל"
    if not rule.get("enabled", True):
        return "כבוי"
    parts: list[str] = []
    if rule.get("min_size") is not None:
        parts.append(f"≥{fmt_size(rule['min_size'])}")
    if rule.get("max_size") is not None:
        parts.append(f"≤{fmt_size(rule['max_size'])}")
    if media_type in HYPER_TYPES_WITH_DURATION:
        if rule.get("min_duration") is not None:
            parts.append(f"≥{fmt_duration(rule['min_duration'])}")
        if rule.get("max_duration") is not None:
            parts.append(f"≤{fmt_duration(rule['max_duration'])}")
    if not parts:
        return "הכל"
    joiner = " וגם " if (rule.get("combine") or "and") == "and" else " או "
    return joiner.join(parts)


def hyper_type_button(media_type: str, rule: "dict | None") -> str:
    return f"{HYPER_TYPE_LABELS.get(media_type, media_type)}: {_rule_summary(media_type, rule)}"


def hyper_account_list_text(userbots: list, statuses: dict) -> str:
    if not userbots:
        return (
            f"{TITLE_HYPER}\n\n"
            "אין חשבונות יוזרבוט. הוסף חשבון תחילה במסך החשבונות, "
            "ואז אפשר להפעיל עליו גיבוי הייפר."
        )
    active = sum(1 for s in statuses.values() if s)
    return (
        f"{TITLE_HYPER}\n\n"
        f"פעיל ב-<b>{active}</b> מתוך <b>{len(userbots)}</b> חשבונות\n\n"
        "<i>הייפר מגבה אוטומטית כל קובץ/מדיה שחשבון מעלה (בכל צ׳אט) לערוץ גיבוי — "
        "עם סינון חכם, בלי כפילויות, ובכפוף למכסה היומית.</i>\n\n"
        "🟢 = פעיל | 🔴 = כבוי\n"
        "בחר חשבון להגדרה וניהול:"
    )


def hyper_menu_text(ub, cfg: "dict | None", dst, queued: int = 0) -> str:
    enabled = bool(cfg and cfg["enabled"])
    status = "🟢 פעיל" if enabled else "🔴 כבוי"
    dst_line = dst.display() if dst else "<i>לא נבחר</i>"
    copied = (cfg or {}).get("copied_count", 0)
    skipped = (cfg or {}).get("skipped_count", 0)
    failed = (cfg or {}).get("failed_count", 0)
    warn = ""
    if enabled and not dst:
        warn = "\n\n⚠️ בחר ערוץ גיבוי כדי שהמצב יתחיל לפעול."
    queue_line = f"\n⏳ בתור להמשך: <b>{queued}</b> (ממתין למכסה/שליחה)" if queued else ""
    return (
        f"{TITLE_HYPER}\n\n"
        f"חשבון: <b>{esc(ub.display())}</b>\n"
        f"סטטוס: <b>{status}</b>\n"
        f"ערוץ גיבוי: {esc(dst_line) if dst else dst_line}\n\n"
        "<i>המצב מגבה אוטומטית כל קובץ/מדיה שהחשבון הזה מעלה, בכל צ׳אט, "
        "לערוץ הגיבוי — עם סינון חכם ובלי כפילויות.</i>\n\n"
        f"📊 גובו: <b>{copied}</b> | דולגו: <b>{skipped}</b> | נכשלו: <b>{failed}</b>"
        f"{queue_line}"
        f"{warn}"
    )


def hyper_type_text(ub, media_type: str, rule: "dict | None") -> str:
    label = HYPER_TYPE_LABELS.get(media_type, media_type)
    enabled = rule is None or rule.get("enabled", True)
    combine = (rule or {}).get("combine", "and")
    combine_label = "וגם (כל התנאים)" if combine == "and" else "או (לפחות תנאי אחד)"
    lines = [
        f"{TITLE_HYPER}\n",
        f"חשבון: <b>{esc(ub.display())}</b>",
        f"סוג: <b>{label}</b>",
        f"סטטוס: <b>{'✅ מגובה' if enabled else '❌ מדולג'}</b>\n",
    ]
    if enabled:
        lines.append(f"גודל מינימלי: <b>{fmt_size((rule or {}).get('min_size'))}</b>")
        lines.append(f"גודל מקסימלי: <b>{fmt_size((rule or {}).get('max_size'))}</b>")
        if media_type in HYPER_TYPES_WITH_DURATION:
            lines.append(f"אורך מינימלי: <b>{fmt_duration((rule or {}).get('min_duration'))}</b>")
            lines.append(f"אורך מקסימלי: <b>{fmt_duration((rule or {}).get('max_duration'))}</b>")
        lines.append(f"\nחיבור תנאים: <b>{combine_label}</b>")
        lines.append("\n<i>גבול ריק (—) = לא נבדק. ריק בכולם = מגבה הכל.</i>")
    return "\n".join(lines)


def hyper_dst_picker_text(ub) -> str:
    return (
        f"{TITLE_HYPER}\n\n"
        f"חשבון: <b>{esc(ub.display())}</b>\n\n"
        "בחר ערוץ גיבוי מתוך היעדים הקיימים:\n"
        "<i>(כדי להוסיף ערוץ חדש — הוסף אותו תחילה במסך היעדים)</i>"
    )


def hyper_prompt_value(media_type: str, field: str) -> str:
    label = HYPER_FIELD_LABELS.get(field, field)
    unit = "בדקות" if field in ("mindur", "maxdur") else "במגה-בייט (MB)"
    return (
        f"{TITLE_HYPER}\n\n"
        f"{HYPER_TYPE_LABELS.get(media_type, media_type)} — {label}\n\n"
        f"הזן ערך {unit} (מספר). הזן 0 כדי לנקות את הגבול:"
    )


# ── Errors and confirmations ───────────────────────────────────────────────────

def error_text(msg: str) -> str:
    return f"{TITLE_ERROR}\n\n{esc(msg)}"


def confirm_delete_job_text(job_name: str) -> str:
    return (
        f"{TITLE_CONFIRM_DELETE}\n\n"
        f"האם למחוק את המשימה <b>{esc(job_name)}</b>?\n"
        "פעולה זו אינה הפיכה."
    )


def confirm_cancel_job_text(job_name: str) -> str:
    return (
        f"{TITLE_CONFIRM_CANCEL}\n\n"
        f"האם לבטל את המשימה <b>{esc(job_name)}</b>?"
    )


# ── Utilities ──────────────────────────────────────────────────────────────────

def esc(text: str | None) -> str:
    """Escape HTML special characters."""
    if text is None:
        return ""
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )


def _fmt_dt(dt_str: str | None) -> str:
    """Parse a UTC datetime string from SQLite and return it in Israel local time (Asia/Jerusalem)."""
    if not dt_str:
        return "—"
    try:
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo
        _IL = ZoneInfo("Asia/Jerusalem")
        dt = datetime.strptime(dt_str[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone(_IL).strftime("%d/%m/%Y %H:%M")
    except Exception:
        return dt_str[:16]


# ── ETA Utilities ──────────────────────────────────────────────────────────────

def _format_eta(seconds: float) -> str:
    if seconds <= 0:
        return "מחשב..."
    minutes = int(seconds / 60)
    if minutes < 1:
        return "פחות מדקה"
    if minutes < 60:
        return f"כ-{minutes} דקות"
    hours = minutes // 60
    mins = minutes % 60
    if hours < 24:
        return f"כ-{hours} שעות ו-{mins} דקות"
    days = hours // 24
    hrs = hours % 24
    return f"כ-{days} ימים ו-{hrs} שעות"


def _estimate_copy_time(remaining_msgs: int) -> float:
    from app.repositories import state_repo
    settings = state_repo.get_settings_dict()
    min_ms = int(settings.get("min_delay_ms", 2000))
    max_ms = int(settings.get("max_delay_ms", 5000))
    batch_min = int(settings.get("batch_size_min", 50))
    batch_max = int(settings.get("batch_size_max", 100))
    pause_min = int(settings.get("batch_pause_min_s", 60))
    pause_max = int(settings.get("batch_pause_max_s", 120))
    
    avg_delay_s = (min_ms + max_ms) / 2000.0
    avg_batch = (batch_min + batch_max) / 2.0
    avg_pause = (pause_min + pause_max) / 2.0
    
    sec_per_msg = avg_delay_s + (avg_pause / avg_batch)
    return remaining_msgs * sec_per_msg


def _estimate_scan_time(remaining_msgs: int) -> float:
    # Scan fetches ~100 messages quickly, but uses _MSG_SLEEP_S (0.5s) per message 
    # and _BATCH_SLEEP_S (10s) every _BATCH_EVERY (50) messages.
    # So avg time per message is ~0.7 seconds.
    return remaining_msgs * 0.7
