"""Services used by B-UH maximum-history management commands."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from django.utils.timezone import now
from esi.errors import TokenError
from memberaudit import tasks
from memberaudit.models import Character

ProgressWriter = Callable[[str], None]


@dataclass(frozen=True)
class MailBackfillResult:
    """Summary of one character's mail backfill."""

    header_count: int
    bodies_requested: int
    bodies_loaded: int
    bodies_failed: int
    token_error: bool

    @property
    def is_success(self) -> bool:
        return not self.token_error and self.bodies_failed == 0


def configure_runtime_mail_limit(mail_limit: int) -> int:
    """Remove Member Audit's normal mail cap in this process only.

    The live workers retain their normal configured cap, so recurring updates do not
    crawl a character's complete mailbox every few hours.
    """

    if mail_limit < 250:
        raise ValueError("mail_limit must be at least 250")

    from memberaudit.managers import character_sections_2

    old_limit = character_sections_2.MEMBERAUDIT_MAX_MAILS
    character_sections_2.MEMBERAUDIT_MAX_MAILS = mail_limit
    return old_limit


def queue_non_mail_sections(character: Character) -> int:
    """Force all enabled non-mail Member Audit sections into the low-priority queue."""

    queued = 0
    sections = sorted(
        Character.UpdateSection.enabled_sections(), key=lambda item: item.value
    )
    for section in sections:
        if section == Character.UpdateSection.MAILS:
            continue

        task = getattr(tasks, f"update_character_{section.value}", None)
        if task is None:
            continue

        task.apply_async(
            kwargs={"character_pk": character.pk, "force_update": True},
            priority=tasks.MEMBERAUDIT_TASKS_LOW_PRIORITY,
        )
        queued += 1

    return queued


def _record_mail_failure(character: Character, error: Exception, token_error: bool) -> None:
    """Record a backfill error on the normal Member Audit mail status."""

    message = f"{type(error).__name__}: {error}"[:2000]
    character.update_status_set.update_or_create(
        section=Character.UpdateSection.MAILS,
        defaults={
            "is_success": False,
            "has_token_error": token_error,
            "error_message": message,
            "run_finished_at": now(),
        },
    )


def _load_one_mail_body(
    character: Character,
    mail_id: int,
    max_retries: int,
    retry_delay: float,
) -> tuple[bool, Exception | None]:
    """Load one missing body, retrying transient failures."""

    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            mail = character.mails.get(mail_id=mail_id)
            character.update_mail_body(mail, force_update=False)
            return True, None
        except TokenError:
            raise
        except character.mails.model.DoesNotExist:
            # ESI may report that a mail was deleted and Member Audit removes its row.
            return True, None
        except Exception as ex:  # noqa: BLE001 - each mail must not abort all characters
            last_error = ex
            if attempt < max_retries:
                time.sleep(retry_delay * attempt)

    return False, last_error


def backfill_character_mail(
    character: Character,
    body_delay: float,
    max_retries: int,
    progress: ProgressWriter,
) -> MailBackfillResult:
    """Fetch all ESI-visible mail headers and every currently missing body."""

    section = Character.UpdateSection.MAILS
    character.reset_update_section(section)

    try:
        character.perform_update_with_error_logging(
            section=section,
            method=character.update_mailing_lists,
            force_update=True,
        )
        character.perform_update_with_error_logging(
            section=section,
            method=character.update_mail_labels,
            force_update=True,
        )
        header_result = character.perform_update_with_error_logging(
            section=section,
            method=character.update_mail_headers,
            force_update=True,
        )
    except Exception as ex:  # noqa: BLE001 - Member Audit records the exact failure.
        progress(f"  Mail headers stopped: {type(ex).__name__}: {ex}")
        return MailBackfillResult(0, 0, 0, 0, isinstance(ex, TokenError))

    header_count = character.mails.count()
    missing_ids = list(
        character.mails.filter(body="")
        .order_by("-mail_id")
        .values_list("mail_id", flat=True)
    )
    total_missing = len(missing_ids)
    progress(f"  Stored mail headers: {header_count:,}")
    progress(f"  Missing mail bodies to fetch: {total_missing:,}")

    loaded = 0
    failed = 0
    failure_samples: list[str] = []

    for position, mail_id in enumerate(missing_ids, start=1):
        try:
            succeeded, error = _load_one_mail_body(
                character=character,
                mail_id=mail_id,
                max_retries=max_retries,
                retry_delay=max(body_delay, 0.5),
            )
        except TokenError as ex:
            _record_mail_failure(character, ex, token_error=True)
            progress(f"  Token error while reading mail {mail_id}: {ex}")
            return MailBackfillResult(header_count, total_missing, loaded, failed + 1, True)

        if succeeded:
            loaded += 1
        else:
            failed += 1
            if error is not None and len(failure_samples) < 5:
                failure_samples.append(f"mail {mail_id}: {type(error).__name__}: {error}")

        if position == 1 or position % 25 == 0 or position == total_missing:
            progress(
                f"  Mail bodies: {position:,}/{total_missing:,} checked; "
                f"loaded={loaded:,}, failed={failed:,}"
            )

        if body_delay > 0:
            time.sleep(body_delay)

    if failed:
        error = RuntimeError(
            f"{failed} mail bodies could not be fetched. " + "; ".join(failure_samples)
        )
        _record_mail_failure(character, error, token_error=False)
    else:
        character.update_section_log_result(
            section=section,
            is_success=True,
            is_updated=header_result.is_updated or loaded > 0,
        )

    return MailBackfillResult(
        header_count=header_count,
        bodies_requested=total_missing,
        bodies_loaded=loaded,
        bodies_failed=failed,
        token_error=False,
    )
