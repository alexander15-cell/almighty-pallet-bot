"""
Regression guard: Discord rejects the entire command tree sync if ANY
command/group name or description, or ANY @app_commands.describe() parameter
description, is outside 1-100 characters - and it fails ALL commands at
once (CommandSyncFailure), not just the offending one, so a single typo'd
description can silently take down every slash command in the bot. This
already happened once in this project (a command description one character
over the limit) - this statically scans every cog for both contexts and
enforces the bound so it's caught locally instead of at bot startup.
"""
import re
from pathlib import Path

COGS_DIR = Path(__file__).resolve().parent.parent / "cogs"


def _iter_command_and_group_descriptions():
    for path in sorted(COGS_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for pattern in (
            r'@\w*\.?command\(\s*name="([^"]*)",\s*description="([^"]*)"',
            r'app_commands\.Group\(\s*name="([^"]*)",\s*description="([^"]*)"',
        ):
            for match in re.finditer(pattern, text, re.DOTALL):
                name, description = match.groups()
                yield path.name, name, description


def _iter_describe_parameter_descriptions():
    for path in sorted(COGS_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for block_match in re.finditer(r"@app_commands\.describe\((.*?)\)\n", text, re.DOTALL):
            block = block_match.group(1)
            for kv_match in re.finditer(r'(\w+)="((?:[^"\\]|\\.)*)"', block):
                param_name, description = kv_match.groups()
                yield path.name, param_name, description


def test_every_command_and_group_description_is_within_discord_limits():
    for filename, name, description in _iter_command_and_group_descriptions():
        assert 1 <= len(description) <= 100, (
            f"{filename}: '{name}' description is {len(description)} chars "
            f"(Discord requires 1-100) - this would fail the ENTIRE command "
            f"sync, not just this command: {description!r}"
        )


def test_every_describe_parameter_description_is_within_discord_limits():
    for filename, param_name, description in _iter_describe_parameter_descriptions():
        assert 1 <= len(description) <= 100, (
            f"{filename}: parameter {param_name!r} description is "
            f"{len(description)} chars (Discord requires 1-100): {description!r}"
        )
