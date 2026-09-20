"""Incident-only log review supported by fresh, successful database evidence.

Ordinary deployments keep DockerHost's strict policy. This adapter applies only
to unchanged retained workers during reviewed rollback. It reads the complete
interval and retains counts/digests; it never moves the original log boundary.
"""

import ast
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re

from .contracts import DeploymentError
from .docker_host import (
    ALLIANCEAUTH_LOG_HEADER_RE,
    ALLIANCEAUTH_LOG_MONTHS,
    CELERY_LOG_HEADER_RE,
    DockerHost,
    FATAL_LOG_RE,
)
from .recovery_sync import stamp

TASK_CLOCKS = {
    "structures.tasks.update_structures_assets_for_owner": (
        "structures_owners",
        "assets_last_update_at",
    ),
    "structures.tasks.fetch_notification_for_owner": (
        "structures_owners",
        "notifications_last_update_at",
    ),
    "moonmining.tasks.update_refineries_from_esi_for_owner": (
        "moonmining_owners",
        "last_update_at",
    ),
    "moonmining.tasks.fetch_notifications_from_esi_for_owner": (
        "moonmining_owners",
        "last_update_at",
    ),
}
ASSET_TASK = "memberaudit.tasks.assets_build_list_from_esi"
TASK_FAILURE = re.compile(
    r"Task (?P<task>[a-zA-Z0-9_.]+)\[[a-f0-9-]{36}\] raised unexpected: HTTPError\(\)$"
)
HARD_FAILURE = re.compile(
    r"CRITICAL|PermissionError|permission denied|ModuleNotFoundError|"
    r"ImproperlyConfigured|ImportError:|SyntaxError:|django\.db\.|"
    r"DatabaseError|IntegrityError|OperationalError|MemoryError|Worker failed to boot|"
    r"\b(?:401 Unauthorized|403 Forbidden|Missing Permissions)\b|"
    r"\b(?:401|403) Client Error\b|\bHTTP(?:Client)?Error[\s:<(]+(?:401|403)\b|"
    r"\bstatus(?:_code)?[\s:=]+(?:401|403)\b",
    re.IGNORECASE,
)
HTTP_EXCEPTIONS = {
    "HTTPError",
    "HTTPClientError",
    "requests.exceptions.HTTPError",
    "esi.exceptions.HTTPClientError",
    "aiopenapi3.errors.HTTPClientError",
}
JWT_KEYS = {
    "scp",
    "jti",
    "kid",
    "sub",
    "azp",
    "tenant",
    "tier",
    "region",
    "aud",
    "name",
    "owner",
    "exp",
    "iat",
    "nbf",
    "iss",
    "character_id",
    "token_type",
}


class MetadataLogError(ValueError):
    """Unknown authentication metadata must fail without exposing its payload."""


def header(line):
    match = CELERY_LOG_HEADER_RE.match(line)
    if match:
        return (
            stamp(match["timestamp"] + "+00:00"),
            match["level"],
            "celery",
            line[match.end() :],
        )
    match = ALLIANCEAUTH_LOG_HEADER_RE.match(line)
    if match:
        clock = (
            f"{match['year']}-{ALLIANCEAUTH_LOG_MONTHS[match['month']]}-"
            f"{match['day']} {match['clock']}+00:00"
        )
        return stamp(clock), match["level"], match["component"], line[match.end() :]
    return None


def records(text):
    current = []
    for line in text.splitlines():
        if header(line) and current:
            yield current
            current = []
        current.append(line)
    if current:
        yield current


def http_traceback(lines):
    if len(lines) > 128 or any(HARD_FAILURE.search(line) for line in lines):
        return False
    terminals = []
    for line in lines[1:]:
        match = re.match(
            r"^((?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*(?:Error|Exception))(?::|$)", line
        )
        if match:
            terminals.append(match[1])
        elif (
            line
            and not line[0].isspace()
            and line
            not in {
                "Traceback (most recent call last):",
                "During handling of the above exception, another exception occurred:",
                "The above exception was the direct cause of the following exception:",
            }
        ):
            return False
    return (
        "Traceback (most recent call last):" in lines
        and bool(terminals)
        and set(terminals) <= HTTP_EXCEPTIONS
    )


class RecoveredLogReview:
    def __init__(self, status, review, *, started_at):
        # The caller must validate the complete snapshot before enabling this.
        self.started_at = started_at.replace(microsecond=0)
        self.task_success = {
            task: min(stamp(row[field]) for row in status[model])
            for task, (model, field) in TASK_CLOCKS.items()
        }
        self.asset_success = {
            row["character_id"]: stamp(row["run_finished_at"])
            for row in status["asset_status"]
        }
        self.all_assets_success = min(self.asset_success.values())
        by_identity = {
            row["eve_character__character_id"]: row["pk"]
            for row in status["asset_characters"]
            if row["is_disabled"] is False
        }
        self.asset_aliases = {
            item["previous_character_pk"]: by_identity[item["eve_character_id"]]
            for item in review["asset_recoveries"]
        }
        self.groups = {}

    def retain(self, kind, source, lines, event):
        key = (kind, source)
        if key not in self.groups:
            self.groups[key] = {
                "records": 0,
                "first_at": event,
                "last_at": event,
                "digest": hashlib.sha256(),
            }
        group = self.groups[key]
        group["records"] += 1
        group["first_at"] = min(group["first_at"], event)
        group["last_at"] = max(group["last_at"], event)
        raw = ("\n".join(lines) + "\n").encode("utf-8")
        group["digest"].update(len(raw).to_bytes(8, "big") + raw)

    def summary(self):
        return [
            {
                "category": kind,
                "source": source,
                "log_records": group["records"],
                "first_at": group["first_at"].isoformat(),
                "last_at": group["last_at"].isoformat(),
                "records_sha256": group["digest"].hexdigest(),
            }
            for (kind, source), group in sorted(self.groups.items())
        ]

    def classify(self, lines, *, services_worker):
        details = header(lines[0]) if lines else None
        if details is None:
            return None
        event, level, logger, message = details
        event_end = event + timedelta(seconds=1)
        # Require later success by at least the log header's one-second precision.
        # Errors during this verification stay strict, even if a later task succeeds.
        if level == "DEBUG" and logger == "esi.managers":
            try:
                value = ast.literal_eval(message)
            except (ValueError, SyntaxError, RecursionError):
                value = None
            if (
                len(lines) == 1
                and isinstance(value, dict)
                and set(value) <= JWT_KEYS
                and isinstance(value.get("sub"), str)
                and re.fullmatch(r"CHARACTER:EVE:[1-9][0-9]*", value["sub"])
                and isinstance(value.get("scp"), list)
                and all(
                    isinstance(scope, str) and re.fullmatch(r"esi-[a-z0-9_.-]+", scope)
                    for scope in value["scp"]
                )
            ):
                return "decoded-ESI-identity-metadata"
            if FATAL_LOG_RE.search("\n".join(lines)):
                # Do not echo unknown JWT claims or authentication metadata.
                raise MetadataLogError(
                    "Unrecognized ESI metadata log record; payload withheld"
                )
            return None
        if event_end > self.started_at:
            return None
        if HARD_FAILURE.search("\n".join(lines)):
            return None
        task = (
            TASK_FAILURE.fullmatch(message)
            if level == "ERROR" and logger == "celery"
            else None
        )
        if task and http_traceback(lines):
            name = task["task"]
            if name in self.task_success and self.task_success[name] >= event_end:
                return "ESI-task-error-followed-by-success"
            if name == ASSET_TASK and self.all_assets_success >= event_end:
                return "assets-task-error-followed-by-success"
        if level == "ERROR" and logger in {"celery", "memberaudit.models.characters"}:
            match = re.fullmatch(
                r".{1,200} \(ID:([1-9][0-9]*)\): assets: Error occurred: HTTPClientError:.*",
                message,
            )
            if (
                match
                and "<HTTPClientError 404 " in message
                and "Invalid IDs in the request" in message
                and http_traceback(lines)
            ):
                original = int(match[1])
                current = self.asset_aliases.get(original, original)
                if (
                    self.asset_success.get(
                        current, datetime.min.replace(tzinfo=timezone.utc)
                    )
                    >= event_end
                ):
                    return "assets-404-followed-by-success"
        if (
            services_worker
            and level == "ERROR"
            and logger
            in {"celery", "allianceauth.services.modules.discord.discord_client.client"}
        ):
            match = re.fullmatch(
                r"\[Discord Service\] [a-f0-9]{32}: Discord API returned error code (429|503) "
                r"for member ID [1-9][0-9]{5,19} with this response: (.+)",
                message,
            )
            body = None
            if match and len(lines) == 1:
                try:
                    body = json.loads(match[2])
                except ValueError:
                    pass
            if (
                isinstance(body, dict)
                and isinstance(body.get("message"), str)
                and set(body) <= {"message", "code", "global", "retry_after"}
                and body.get("code") in {None, 0}
            ):
                # Keep dependency errors as warnings; do not claim Discord recovered.
                return (
                    "historical-Discord-rate-limit"
                    if match[1] == "429"
                    else "historical-Discord-unavailable"
                )
        return None


class ReviewedDockerHost(DockerHost):
    """Enable recovered-history review only on the retained rollback path."""

    recovered_log_review = None
    reviewed_worker_ids = frozenset()
    recheck_recovered_sync = None

    def _verify_restored(self, bundle, replaced_services):
        if self.recheck_recovered_sync is None:
            raise DeploymentError("Current sync recheck is required before completion")
        self.recheck_recovered_sync()
        super()._verify_restored(bundle, replaced_services)

    def _classify_log_batch(
        self, logs_by_source, individually_scanned, owner_transition_phase
    ):
        review = self.recovered_log_review
        if (
            review is None
            or owner_transition_phase != "rollback"
            or not self.recovery_baseline_verified
        ):
            return super()._classify_log_batch(
                logs_by_source, individually_scanned, owner_transition_phase
            )
        strict = []
        accepted = set()
        for source, text in logs_by_source:
            service, _, identity = source.partition("/")
            if (
                service not in individually_scanned
                or identity not in self.reviewed_worker_ids
            ):
                strict.append((source, text))
                continue
            remaining = []
            for record in records(text):
                try:
                    kind = review.classify(
                        record, services_worker=service.endswith("_services")
                    )
                except MetadataLogError:
                    remaining.append(
                        "[2000-01-01 00:00:00: ERROR/MainProcess] "
                        "Unrecognized ESI metadata log record; payload withheld"
                    )
                    continue
                if kind:
                    review.retain(kind, source, record, header(record[0])[0])
                    accepted.add(
                        f"Reviewed {kind}; source={source}; historical findings retained"
                    )
                    # Keep a record boundary so a later orphan traceback can
                    # never attach to an otherwise allowed owner incident.
                    remaining.append(
                        "[2000-01-01 00:00:00: INFO/MainProcess] Reviewed record retained"
                    )
                else:
                    remaining.extend(record)
            strict.append((source, "\n".join(remaining)))
        result = super()._classify_log_batch(
            strict, individually_scanned, owner_transition_phase
        )
        return tuple(sorted(accepted)) + result
