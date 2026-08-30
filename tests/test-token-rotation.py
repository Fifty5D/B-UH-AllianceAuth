from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "ops" / "buh-rotate-discord-token.py"
SPEC = importlib.util.spec_from_file_location("buh_rotate_discord_token", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def main() -> None:
    old_token = "A" * 24 + "." + "B" * 6 + "." + "C" * 38
    new_token = "D" * 24 + "." + "E" * 6 + "." + "F" * 38
    source = (
        "OTHER_SETTING = True\n"
        f'DISCORD_BOT_TOKEN = "{old_token}"  # keep this comment\n'
        "FINAL_SETTING = 3\n"
    )
    updated, name = MODULE.replace_token_source(source, new_token)
    assert name == "DISCORD_BOT_TOKEN"
    assert old_token not in updated
    assert new_token in updated
    assert "# keep this comment" in updated
    assert "OTHER_SETTING = True" in updated
    assert "FINAL_SETTING = 3" in updated

    try:
        MODULE.replace_token_source("DISCORD_BOT_TOKEN = env('TOKEN')\n", new_token)
    except ValueError:
        pass
    else:
        raise AssertionError("Non-literal setting was modified")

    print("Token rotation tests passed.")


if __name__ == "__main__":
    main()
