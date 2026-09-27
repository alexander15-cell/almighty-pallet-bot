"""Fail-closed recovery helpers. No network or database writes."""
import config


async def block_destructive(interaction):
    if not config.PRESERVE_DISCORD_HISTORY:
        return False
    await interaction.response.send_message(
        "History protection is on. This action is disabled so existing records stay unchanged.",
        ephemeral=True,
    )
    return True


async def require_current_card(interaction, item, status, *, source_message_id=None):
    """Reject retained cards/delayed forms. Forms carry the ORIGINAL card ID."""
    if not config.PRESERVE_DISCORD_HISTORY:
        return True
    expected_id = item.get("current_message_id") if item else None
    actual_id = source_message_id
    if actual_id is None:
        actual_id = getattr(getattr(interaction, "message", None), "id", None)
    guild_id = getattr(getattr(interaction, "guild", None), "id", None)
    if (
        not item or item.get("status") != status
        or not isinstance(expected_id, int) or isinstance(expected_id, bool)
        or not isinstance(actual_id, int) or isinstance(actual_id, bool)
        or actual_id != expected_id
        or (config.GUILD_ID and guild_id != config.GUILD_ID)
    ):
        await interaction.response.send_message(
            "This is an older or unavailable item card. Use the latest card for this item.",
            ephemeral=True,
        )
        return False
    return True
