
"""Synthetic request correlation; contains no production account or endpoint."""
import hashlib

CLIENT = "allianceauth.services.modules.discord.discord_client.client"
TASKS = "allianceauth.services.modules.discord.tasks"
MODELS = "allianceauth.services.modules.discord.models"


def nickname_case():
    original, retry = "a" * 32, "b" * 32
    endpoint = "https://discord.com/api/guilds/111111111111111111/members/222222222222222222"

    def aa(second, level, logger, message):
        return f"[03/Oct/2026 19:00:{second:02d}] {level} [{logger}:679] {message}"

    error = (f"[Discord Service] {original}: Discord API returned error code 429 "
             'for member ID 222222222222222222 with this response: '
             '{"message":"You are being rate limited.","retry_after":448,"global":false}')
    fatal = [
        aa(44, "ERROR", CLIENT, error),
        f"[2026-10-03 19:00:44,792: ERROR/MainProcess] {error}",
    ]
    lines = [
        aa(44, "INFO", TASKS, "[Discord Service] Running update_nickname for user synthetic"),
        aa(44, "INFO", CLIENT, f"[Discord Service] {original}: sending PATCH request to url '{endpoint}'"),
        aa(44, "DEBUG", CLIENT, f"[Discord Service] {original}: returned status code 429 with headers: {{}}"),
        *fatal,
        aa(44, "INFO", TASKS, "[Discord Service] API back off for update_nickname wth user synthetic "
           "due to DiscordTooManyRequestsError(), retrying in 1 seconds"),
        aa(45, "INFO", TASKS, "[Discord Service] Running update_nickname for user synthetic"),
        aa(45, "INFO", CLIENT, f"[Discord Service] {retry}: sending PATCH request to url '{endpoint}'"),
        aa(46, "DEBUG", CLIENT, f"[Discord Service] {retry}: returned status code 204 with headers: {{}}"),
        aa(46, "INFO", MODELS, "[Discord Service] Nickname for synthetic has been updated"),
    ]
    proof = {
        "service": "allianceauth_worker_services", "container_id": "e" * 64,
        "since": "2026-10-03T19:00:44+00:00", "until": "2026-10-03T19:00:48+00:00",
        "username": "synthetic", "guild_id": "111111111111111111",
        "member_id": "222222222222222222", "request_id": original, "retry_request_id": retry,
        "error_line_sha256": [hashlib.sha256(line.encode()).hexdigest() for line in fatal],
    }
    return lines, proof
