import textwrap
import typing as t

import arrow
import discord
from arrow import Arrow

import bot
from bot import constants
from bot.exts.help_channels import _caches
from bot.log import get_logger

log = get_logger(__name__)

ASKING_GUIDE_URL = "https://pythondiscord.com/pages/asking-good-questions/"

AVAILABLE_MSG = f"""
Send your question here to claim the channel.

**Remember to:**
• **Ask** your Python question, not if you can ask or if there's an expert who can help.
• **Show** a code sample as text (rather than a screenshot) and the error message, if you got one.
• **Explain** what you expect to happen and what actually happens.

For more tips, check out our guide on [asking good questions]({ASKING_GUIDE_URL}).
"""

AVAILABLE_TITLE = "Available help channel"

AVAILABLE_FOOTER = "Closes after a period of inactivity, or when you send !close."

DORMANT_MSG = f"""
This help channel has been marked as **dormant**, and has been moved into the **{{dormant}}** \
category at the bottom of the channel list. It is no longer possible to send messages in this \
channel until it becomes available again.

If your question wasn't answered yet, you can claim a new help channel from the \
**{{available}}** category by simply asking your question again. Consider rephrasing the \
question to maximize your chance of getting a good answer. If you're not sure how, have a look \
through our guide for **[asking a good question]({ASKING_GUIDE_URL})**.
"""


async def update_message_caches(message: discord.Message) -> None:
    """Checks the source of new content in a help channel and updates the appropriate cache."""
    channel = message.channel

    log.trace(f"Checking if #{channel} ({channel.id}) has had a reply.")

    claimant_id = await _caches.claimants.get(channel.id)
    if not claimant_id:
        # The mapping for this channel doesn't exist, we can't do anything.
        return

    # datetime.timestamp() would assume it's local, despite d.py giving a (naïve) UTC time.
    timestamp = Arrow.fromdatetime(message.created_at).timestamp()

    # Overwrite the appropriate last message cache depending on the author of the message
    if message.author.id == claimant_id:
        await _caches.claimant_last_message_times.set(channel.id, timestamp)
    else:
        await _caches.non_claimant_last_message_times.set(channel.id, timestamp)


async def get_last_message(channel: discord.TextChannel) -> t.Optional[discord.Message]:
    """Return the last message sent in the channel or None if no messages exist."""
    log.trace(f"Getting the last message in #{channel} ({channel.id}).")

    try:
        return await channel.history(limit=1).next()  # noqa: B305
    except discord.NoMoreItems:
        log.debug(
            f"No last message available; #{channel} ({channel.id}) has no messages."
        )
        return None


async def is_empty(channel: discord.TextChannel) -> bool:
    """Return True if there's an AVAILABLE_MSG and the messages leading up are bot messages."""
    log.trace(f"Checking if #{channel} ({channel.id}) is empty.")

    # A limit of 100 results in a single API call.
    # If AVAILABLE_MSG isn't found within 100 messages, then assume the channel is not empty.
    # Not gonna do an extensive search for it cause it's too expensive.
    async for msg in channel.history(limit=100):
        if not msg.author.bot:
            log.trace(f"#{channel} ({channel.id}) has a non-bot message.")
            return False

        if _match_bot_embed(msg, AVAILABLE_MSG):
            log.trace(f"#{channel} ({channel.id}) has the available message embed.")
            return True

    return False


async def dm_on_open(message: discord.Message) -> None:
    """
    DM claimant with a link to the claimed channel's first message, with a 100 letter preview of the message.

    Does nothing if the user has DMs disabled.
    """
    embed = discord.Embed(
        title="Help channel opened",
        description=f"You claimed {message.channel.mention}.",
        colour=bot.constants.Colours.bright_green,
        timestamp=message.created_at,
    )

    embed.set_thumbnail(url=constants.Icons.green_questionmark)
    formatted_message = textwrap.shorten(message.content, width=100, placeholder="...")
    if formatted_message:
        embed.add_field(name="Your message", value=formatted_message, inline=False)
    embed.add_field(
        name="Conversation",
        value=f"[Jump to message!]({message.jump_url})",
        inline=False,
    )

    try:
        await message.author.send(embed=embed)
        log.trace(f"Sent DM to {message.author.id} after claiming help channel.")
    except discord.errors.Forbidden:
        log.trace(
            f"Ignoring to send DM to {message.author.id} after claiming help channel: DMs disabled."
        )


async def notify(
    channel: discord.TextChannel, last_notification: t.Optional[Arrow]
) -> t.Optional[Arrow]:
    """
    Send a message in `channel` notifying about a lack of available help channels.

    If a notification was sent, return the time at which the message was sent.
    Otherwise, return None.

    Configuration:

    * `HelpChannels.notify` - toggle notifications
    * `HelpChannels.notify_minutes` - minimum interval between notifications
    * `HelpChannels.notify_roles` - roles mentioned in notifications
    """
    if not constants.HelpChannels.notify:
        return

    log.trace("Notifying about lack of channels.")

    if last_notification:
        elapsed = (arrow.utcnow() - last_notification).seconds
        minimum_interval = constants.HelpChannels.notify_minutes * 60
        should_send = elapsed >= minimum_interval
    else:
        should_send = True

    if not should_send:
        log.trace(
            "Notification not sent because it's too recent since the previous one."
        )
        return

    try:
        log.trace("Sending notification message.")

        mentions = " ".join(
            f"<@&{role}>" for role in constants.HelpChannels.notify_roles
        )
        allowed_roles = [
            discord.Object(id_) for id_ in constants.HelpChannels.notify_roles
        ]

        message = await channel.send(
            f"{mentions} A new available help channel is needed but there "
            f"are no more dormant ones. Consider freeing up some in-use channels manually by "
            f"using the `{constants.Bot.prefix}dormant` command within the channels.",
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=allowed_roles
            ),
        )

        return Arrow.fromdatetime(message.created_at)
    except Exception:
        # Handle it here cause this feature isn't critical for the functionality of the system.
        log.exception("Failed to send notification about lack of dormant channels!")


async def pin(message: discord.Message) -> None:
    """Pin an initial question `message` and store it in a cache."""
    if await pin_wrapper(message.id, message.channel, pin=True):
        await _caches.question_messages.set(message.channel.id, message.id)


async def send_available_message(channel: discord.TextChannel) -> None:
    """Send the available message by editing a dormant message or sending a new message."""
    channel_info = f"#{channel} ({channel.id})"
    log.trace(f"Sending available message in {channel_info}.")

    embed = discord.Embed(
        color=constants.Colours.bright_green,
        description=AVAILABLE_MSG,
    )
    embed.set_author(name=AVAILABLE_TITLE, icon_url=constants.Icons.green_checkmark)
    embed.set_footer(text=AVAILABLE_FOOTER)

    msg = await get_last_message(channel)
    if _match_bot_embed(msg, DORMANT_MSG):
        log.trace(f"Found dormant message {msg.id} in {channel_info}; editing it.")
        await msg.edit(embed=embed)
    else:
        log.trace(
            f"Dormant message not found in {channel_info}; sending a new message."
        )
        await channel.send(embed=embed)


async def unpin(channel: discord.TextChannel) -> None:
    """Unpin the initial question message sent in `channel`."""
    msg_id = await _caches.question_messages.pop(channel.id)
    if msg_id is None:
        log.debug(f"#{channel} ({channel.id}) doesn't have a message pinned.")
    else:
        await pin_wrapper(msg_id, channel, pin=False)


def _match_bot_embed(message: t.Optional[discord.Message], description: str) -> bool:
    """Return `True` if the bot's `message`'s embed description matches `description`."""
    if not message or not message.embeds:
        return False

    bot_msg_desc = message.embeds[0].description
    if bot_msg_desc is discord.Embed.Empty:
        log.trace("Last message was a bot embed but it was empty.")
        return False
    return (
        message.author == bot.instance.user
        and bot_msg_desc.strip() == description.strip()
    )


async def pin_wrapper(msg_id: int, channel: discord.TextChannel, *, pin: bool) -> bool:
    """
    Pin message `msg_id` in `channel` if `pin` is True or unpin if it's False.

    Return True if successful and False otherwise.
    """
    channel_str = f"#{channel} ({channel.id})"
    if pin:
        func = bot.instance.http.pin_message
        verb = "pin"
    else:
        func = bot.instance.http.unpin_message
        verb = "unpin"

    try:
        await func(channel.id, msg_id)
    except discord.HTTPException as e:
        if e.code == 10008:
            log.debug(f"Message {msg_id} in {channel_str} doesn't exist; can't {verb}.")
        else:
            log.exception(
                f"Error {verb}ning message {msg_id} in {channel_str}: {e.status} ({e.code})"
            )
        return False
    else:
        log.trace(f"{verb.capitalize()}ned message {msg_id} in {channel_str}.")
        return True
