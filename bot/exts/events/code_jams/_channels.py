import typing as t

import discord

from bot.constants import Categories, Channels, Roles
from bot.log import get_logger

log = get_logger(__name__)

MAX_CHANNELS = 50
CATEGORY_NAME = "Code Jam"


async def _get_category(guild: discord.Guild) -> discord.CategoryChannel:
    """
    Return a code jam category.

    If all categories are full or none exist, create a new category.
    """
    for category in guild.categories:
        if category.name == CATEGORY_NAME and len(category.channels) < MAX_CHANNELS:
            return category

    return await _create_category(guild)


async def _create_category(guild: discord.Guild) -> discord.CategoryChannel:
    """Create a new code jam category and return it."""
    log.info("Creating a new code jam category.")

    category_overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        guild.me: discord.PermissionOverwrite(read_messages=True),
    }

    category = await guild.create_category_channel(
        CATEGORY_NAME, overwrites=category_overwrites, reason="It's code jam time!"
    )

    await _send_status_update(
        guild,
        f"Created a new category with the ID {category.id} for this Code Jam's team channels.",
    )

    return category


def _get_overwrites(
    members: list[tuple[discord.Member, bool]],
    guild: discord.Guild,
) -> dict[t.Union[discord.Member, discord.Role], discord.PermissionOverwrite]:
    """Get code jam team channels permission overwrites."""
    team_channel_overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        guild.get_role(Roles.code_jam_event_team): discord.PermissionOverwrite(
            read_messages=True
        ),
    }

    for member, _ in members:
        team_channel_overwrites[member] = discord.PermissionOverwrite(
            read_messages=True
        )

    return team_channel_overwrites


async def create_team_channel(
    guild: discord.Guild,
    team_name: str,
    members: list[tuple[discord.Member, bool]],
    team_leaders: discord.Role,
) -> None:
    """Create the team's text channel."""
    await _add_team_leader_roles(members, team_leaders)

    # Get permission overwrites and category
    team_channel_overwrites = _get_overwrites(members, guild)
    code_jam_category = await _get_category(guild)

    # Create a text channel for the team
    await code_jam_category.create_text_channel(
        team_name,
        overwrites=team_channel_overwrites,
    )


async def create_team_leader_channel(
    guild: discord.Guild, team_leaders: discord.Role
) -> None:
    """Create the Team Leader Chat channel for the Code Jam team leaders."""
    category: discord.CategoryChannel = guild.get_channel(Categories.summer_code_jam)

    team_leaders_chat = await category.create_text_channel(
        name="team-leaders-chat",
        overwrites={
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            team_leaders: discord.PermissionOverwrite(read_messages=True),
        },
    )

    await _send_status_update(
        guild, f"Created {team_leaders_chat.mention} in the {category} category."
    )


async def _send_status_update(guild: discord.Guild, message: str) -> None:
    """Inform the events lead with a status update when the command is ran."""
    channel: discord.TextChannel = guild.get_channel(Channels.code_jam_planning)

    await channel.send(f"<@&{Roles.events_lead}>\n\n{message}")


async def _add_team_leader_roles(
    members: list[tuple[discord.Member, bool]], team_leaders: discord.Role
) -> None:
    """Assign the team leader role to the team leaders."""
    for member, is_leader in members:
        if is_leader:
            await member.add_roles(team_leaders)
