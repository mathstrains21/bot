import asyncio
import random
import typing as t
from datetime import timedelta
from operator import attrgetter

import arrow
import discord
import discord.abc
from discord.ext import commands

from bot import constants
from bot.bot import Bot
from bot.constants import Channels, RedirectOutput
from bot.exts.help_channels import _caches, _channel, _message, _name, _stats
from bot.log import get_logger
from bot.utils import channel as channel_utils, lock, members, scheduling

log = get_logger(__name__)

NAMESPACE = "help"
HELP_CHANNEL_TOPIC = """
This is a Python help channel. You can claim your own help channel in the Python Help: Available category.
"""
AVAILABLE_HELP_CHANNELS = "**Currently available help channel(s):** {available}"


class HelpChannels(commands.Cog):
    """
    Manage the help channel system of the guild.

    The system is based on a 3-category system:

    Available Category

    * Contains channels which are ready to be occupied by someone who needs help
    * Will always contain `constants.HelpChannels.max_available` channels; refilled automatically
      from the pool of dormant channels
        * Prioritise using the channels which have been dormant for the longest amount of time
        * If there are no more dormant channels, the bot will automatically create a new one
        * If there are no dormant channels to move, helpers will be notified (see `notify()`)
    * When a channel becomes available, the dormant embed will be edited to show `AVAILABLE_MSG`
    * User can only claim a channel at an interval `constants.HelpChannels.claim_minutes`
        * To keep track of cooldowns, user which claimed a channel will have a temporary role

    In Use Category

    * Contains all channels which are occupied by someone needing help
    * Channel moves to dormant category after
        - `constants.HelpChannels.idle_minutes_other` minutes since the last user message, or
        - `constants.HelpChannels.idle_minutes_claimant` minutes since the last claimant message.
    * Command can prematurely mark a channel as dormant
        * Channel claimant is allowed to use the command
        * Allowed roles for the command are configurable with `constants.HelpChannels.cmd_whitelist`
    * When a channel becomes dormant, an embed with `DORMANT_MSG` will be sent

    Dormant Category

    * Contains channels which aren't in use
    * Channels are used to refill the Available category

    Help channels are named after the foods in `bot/resources/foods.json`.
    """

    def __init__(self, bot: Bot):
        self.bot = bot
        self.scheduler = scheduling.Scheduler(self.__class__.__name__)

        # Categories
        self.available_category: discord.CategoryChannel = None
        self.in_use_category: discord.CategoryChannel = None
        self.dormant_category: discord.CategoryChannel = None

        # Queues
        self.channel_queue: asyncio.Queue[discord.TextChannel] = None
        self.name_queue: t.Deque[str] = None

        self.last_notification: t.Optional[arrow.Arrow] = None

        self.dynamic_message: t.Optional[int] = None
        self.available_help_channels: t.Set[discord.TextChannel] = set()

        # Asyncio stuff
        self.queue_tasks: t.List[asyncio.Task] = []
        self.init_task = scheduling.create_task(
            self.init_cog(), event_loop=self.bot.loop
        )

    def cog_unload(self) -> None:
        """Cancel the init task and scheduled tasks when the cog unloads."""
        log.trace("Cog unload: cancelling the init_cog task")
        self.init_task.cancel()

        log.trace("Cog unload: cancelling the channel queue tasks")
        for task in self.queue_tasks:
            task.cancel()

        self.scheduler.cancel_all()

    async def _handle_role_change(
        self, member: discord.Member, coro: t.Callable[..., t.Coroutine]
    ) -> None:
        """
        Change `member`'s cooldown role via awaiting `coro` and handle errors.

        `coro` is intended to be `discord.Member.add_roles` or `discord.Member.remove_roles`.
        """
        try:
            await coro(
                self.bot.get_guild(constants.Guild.id).get_role(
                    constants.Roles.help_cooldown
                )
            )
        except discord.NotFound:
            log.debug(
                f"Failed to change role for {member} ({member.id}): member not found"
            )
        except discord.Forbidden:
            log.debug(
                f"Forbidden to change role for {member} ({member.id}); "
                f"possibly due to role hierarchy"
            )
        except discord.HTTPException as e:
            log.error(
                f"Failed to change role for {member} ({member.id}): {e.status} {e.code}"
            )

    @lock.lock_arg(NAMESPACE, "message", attrgetter("channel.id"))
    @lock.lock_arg(NAMESPACE, "message", attrgetter("author.id"))
    @lock.lock_arg(
        f"{NAMESPACE}.unclaim", "message", attrgetter("author.id"), wait=True
    )
    async def claim_channel(self, message: discord.Message) -> None:
        """
        Claim the channel in which the question `message` was sent.

        Move the channel to the In Use category and pin the `message`. Add a cooldown to the
        claimant to prevent them from asking another question. Lastly, make a new channel available.
        """
        log.info(f"Channel #{message.channel} was claimed by `{message.author.id}`.")
        await self.move_to_in_use(message.channel)

        # Handle odd edge case of `message.author` not being a `discord.Member` (see bot#1839)
        if not isinstance(message.author, discord.Member):
            log.warning(
                f"{message.author} ({message.author.id}) isn't a member. Not giving cooldown role or sending DM."
            )
        else:
            await self._handle_role_change(message.author, message.author.add_roles)

            try:
                await _message.dm_on_open(message)
            except Exception as e:
                log.warning("Error occurred while sending DM:", exc_info=e)

        await _message.pin(message)

        # Add user with channel for dormant check.
        await _caches.claimants.set(message.channel.id, message.author.id)

        self.bot.stats.incr("help.claimed")

        # datetime.timestamp() would assume it's local, despite d.py giving a (naïve) UTC time.
        timestamp = arrow.Arrow.fromdatetime(message.created_at).timestamp()

        await _caches.claim_times.set(message.channel.id, timestamp)
        await _caches.claimant_last_message_times.set(message.channel.id, timestamp)
        # Delete to indicate that the help session has yet to receive an answer.
        await _caches.non_claimant_last_message_times.delete(message.channel.id)

        # Removing the help channel from the dynamic message, and editing/sending that message.
        self.available_help_channels.remove(message.channel)

        # Not awaited because it may indefinitely hold the lock while waiting for a channel.
        scheduling.create_task(
            self.move_to_available(), name=f"help_claim_{message.id}"
        )

    def create_channel_queue(self) -> asyncio.Queue:
        """
        Return a queue of dormant channels to use for getting the next available channel.

        The channels are added to the queue in a random order.
        """
        log.trace("Creating the channel queue.")

        channels = list(_channel.get_category_channels(self.dormant_category))
        random.shuffle(channels)

        log.trace("Populating the channel queue with channels.")
        queue = asyncio.Queue()
        for channel in channels:
            queue.put_nowait(channel)

        return queue

    async def create_dormant(self) -> t.Optional[discord.TextChannel]:
        """
        Create and return a new channel in the Dormant category.

        The new channel will sync its permission overwrites with the category.

        Return None if no more channel names are available.
        """
        log.trace("Getting a name for a new dormant channel.")

        try:
            name = self.name_queue.popleft()
        except IndexError:
            log.debug("No more names available for new dormant channels.")
            return None

        log.debug(f"Creating a new dormant channel named {name}.")
        return await self.dormant_category.create_text_channel(
            name, topic=HELP_CHANNEL_TOPIC
        )

    async def close_check(self, ctx: commands.Context) -> bool:
        """Return True if the channel is in use and the user is the claimant or has a whitelisted role."""
        if ctx.channel.category != self.in_use_category:
            log.debug(
                f"{ctx.author} invoked command 'close' outside an in-use help channel"
            )
            return False

        if await _caches.claimants.get(ctx.channel.id) == ctx.author.id:
            log.trace(
                f"{ctx.author} is the help channel claimant, passing the check for dormant."
            )
            self.bot.stats.incr("help.dormant_invoke.claimant")
            return True

        log.trace(f"{ctx.author} is not the help channel claimant, checking roles.")
        has_role = await commands.has_any_role(
            *constants.HelpChannels.cmd_whitelist
        ).predicate(ctx)

        if has_role:
            self.bot.stats.incr("help.dormant_invoke.staff")

        return has_role

    @commands.command(name="close", aliases=["dormant", "solved"], enabled=False)
    async def close_command(self, ctx: commands.Context) -> None:
        """
        Make the current in-use help channel dormant.

        May only be invoked by the channel's claimant or by staff.
        """
        # Don't use a discord.py check because the check needs to fail silently.
        if await self.close_check(ctx):
            log.info(f"Close command invoked by {ctx.author} in #{ctx.channel}.")
            await self.unclaim_channel(
                ctx.channel, closed_on=_channel.ClosingReason.COMMAND
            )

    async def get_available_candidate(self) -> discord.TextChannel:
        """
        Return a dormant channel to turn into an available channel.

        If no channel is available, wait indefinitely until one becomes available.
        """
        log.trace("Getting an available channel candidate.")

        try:
            channel = self.channel_queue.get_nowait()
        except asyncio.QueueEmpty:
            log.info("No candidate channels in the queue; creating a new channel.")
            channel = await self.create_dormant()

            if not channel:
                log.info(
                    "Couldn't create a candidate channel; waiting to get one from the queue."
                )
                notify_channel = self.bot.get_channel(
                    constants.HelpChannels.notify_channel
                )
                last_notification = await _message.notify(
                    notify_channel, self.last_notification
                )
                if last_notification:
                    self.last_notification = last_notification
                    self.bot.stats.incr("help.out_of_channel_alerts")

                channel = await self.wait_for_dormant_channel()

        return channel

    async def init_available(self) -> None:
        """Initialise the Available category with channels."""
        log.trace("Initialising the Available category with channels.")

        channels = list(_channel.get_category_channels(self.available_category))
        missing = constants.HelpChannels.max_available - len(channels)

        # If we've got less than `max_available` channel available, we should add some.
        if missing > 0:
            log.trace(f"Moving {missing} missing channels to the Available category.")
            for _ in range(missing):
                await self.move_to_available()

        # If for some reason we have more than `max_available` channels available,
        # we should move the superfluous ones over to dormant.
        elif missing < 0:
            log.trace(
                f"Moving {abs(missing)} superfluous available channels over to the Dormant category."
            )
            for channel in channels[: abs(missing)]:
                await self.unclaim_channel(
                    channel, closed_on=_channel.ClosingReason.CLEANUP
                )

        self.available_help_channels = set(
            _channel.get_category_channels(self.available_category)
        )

        # Getting channels that need to be included in the dynamic message.
        await self.update_available_help_channels()
        log.trace("Dynamic available help message updated.")

    async def init_categories(self) -> None:
        """Get the help category objects. Remove the cog if retrieval fails."""
        log.trace("Getting the CategoryChannel objects for the help categories.")

        try:
            self.available_category = await channel_utils.get_or_fetch_channel(
                constants.Categories.help_available
            )
            self.in_use_category = await channel_utils.get_or_fetch_channel(
                constants.Categories.help_in_use
            )
            self.dormant_category = await channel_utils.get_or_fetch_channel(
                constants.Categories.help_dormant
            )
        except discord.HTTPException:
            log.exception("Failed to get a category; cog will be removed")
            self.bot.remove_cog(self.qualified_name)

    async def init_cog(self) -> None:
        """Initialise the help channel system."""
        log.trace("Waiting for the guild to be available before initialisation.")
        await self.bot.wait_until_guild_available()

        log.trace("Initialising the cog.")
        await self.init_categories()

        self.channel_queue = self.create_channel_queue()
        self.name_queue = _name.create_name_queue(
            self.available_category,
            self.in_use_category,
            self.dormant_category,
        )

        log.trace("Moving or rescheduling in-use channels.")
        for channel in _channel.get_category_channels(self.in_use_category):
            await self.move_idle_channel(channel, has_task=False)

        # Prevent the command from being used until ready.
        # The ready event wasn't used because channels could change categories between the time
        # the command is invoked and the cog is ready (e.g. if move_idle_channel wasn't called yet).
        # This may confuse users. So would potentially long delays for the cog to become ready.
        self.close_command.enabled = True

        # Acquiring the dynamic message ID, if it exists within the cache.
        log.trace("Attempting to fetch How-to-get-help dynamic message ID.")
        self.dynamic_message = await _caches.dynamic_message.get("message_id")

        await self.init_available()
        _stats.report_counts()

        log.info("Cog is ready!")

    async def move_idle_channel(
        self, channel: discord.TextChannel, has_task: bool = True
    ) -> None:
        """
        Make the `channel` dormant if idle or schedule the move if still active.

        If `has_task` is True and rescheduling is required, the extant task to make the channel
        dormant will first be cancelled.
        """
        log.trace(f"Handling in-use channel #{channel} ({channel.id}).")

        closing_time, closed_on = await _channel.get_closing_time(
            channel, self.init_task.done()
        )

        # Closing time is in the past.
        # Add 1 second due to POSIX timestamps being lower resolution than datetime objects.
        if closing_time < (arrow.utcnow() + timedelta(seconds=1)):
            log.info(
                f"#{channel} ({channel.id}) is idle past {closing_time} "
                f"and will be made dormant. Reason: {closed_on.value}"
            )

            await self.unclaim_channel(channel, closed_on=closed_on)
        else:
            # Cancel the existing task, if any.
            if has_task:
                self.scheduler.cancel(channel.id)

            delay = (closing_time - arrow.utcnow()).seconds
            log.info(
                f"#{channel} ({channel.id}) is still active; "
                f"scheduling it to be moved after {delay} seconds."
            )

            self.scheduler.schedule_later(
                delay, channel.id, self.move_idle_channel(channel)
            )

    async def move_to_available(self) -> None:
        """Make a channel available."""
        log.trace("Making a channel available.")

        channel = await self.get_available_candidate()
        log.info(f"Making #{channel} ({channel.id}) available.")

        await _message.send_available_message(channel)

        log.trace(f"Moving #{channel} ({channel.id}) to the Available category.")

        await _channel.move_to_bottom(
            channel=channel,
            category_id=constants.Categories.help_available,
        )

        # Adding the help channel to the dynamic message, and editing/sending that message.
        self.available_help_channels.add(channel)
        await self.update_available_help_channels()

        _stats.report_counts()

    async def move_to_dormant(self, channel: discord.TextChannel) -> None:
        """Make the `channel` dormant."""
        log.info(f"Moving #{channel} ({channel.id}) to the Dormant category.")
        await _channel.move_to_bottom(
            channel=channel,
            category_id=constants.Categories.help_dormant,
        )

        log.trace(f"Sending dormant message for #{channel} ({channel.id}).")
        embed = discord.Embed(
            description=_message.DORMANT_MSG.format(
                dormant=self.dormant_category.name,
                available=self.available_category.name,
            )
        )
        await channel.send(embed=embed)

        log.trace(f"Pushing #{channel} ({channel.id}) into the channel queue.")
        self.channel_queue.put_nowait(channel)

        _stats.report_counts()

    @lock.lock_arg(f"{NAMESPACE}.unclaim", "channel")
    async def unclaim_channel(
        self, channel: discord.TextChannel, *, closed_on: _channel.ClosingReason
    ) -> None:
        """
        Unclaim an in-use help `channel` to make it dormant.

        Unpin the claimant's question message and move the channel to the Dormant category.
        Remove the cooldown role from the channel claimant if they have no other channels claimed.
        Cancel the scheduled cooldown role removal task.

        `closed_on` is the reason that the channel was closed. See _channel.ClosingReason for possible values.
        """
        claimant_id = await _caches.claimants.get(channel.id)
        _unclaim_channel = self._unclaim_channel

        # It could be possible that there is no claimant cached. In such case, it'd be useless and
        # possibly incorrect to lock on None. Therefore, the lock is applied conditionally.
        if claimant_id is not None:
            decorator = lock.lock_arg(f"{NAMESPACE}.unclaim", "claimant_id", wait=True)
            _unclaim_channel = decorator(_unclaim_channel)

        return await _unclaim_channel(channel, claimant_id, closed_on)

    async def _unclaim_channel(
        self,
        channel: discord.TextChannel,
        claimant_id: int,
        closed_on: _channel.ClosingReason,
    ) -> None:
        """Actual implementation of `unclaim_channel`. See that for full documentation."""
        await _caches.claimants.delete(channel.id)
        await _caches.session_participants.delete(channel.id)

        claimant = await members.get_or_fetch_member(
            self.bot.get_guild(constants.Guild.id), claimant_id
        )
        if claimant is None:
            log.info(
                f"{claimant_id} left the guild during their help session; the cooldown role won't be removed"
            )
        else:
            await self._handle_role_change(claimant, claimant.remove_roles)

        await _message.unpin(channel)
        await _stats.report_complete_session(channel.id, closed_on)
        await self.move_to_dormant(channel)

        # Cancel the task that makes the channel dormant only if called by the close command.
        # In other cases, the task is either already done or not-existent.
        if closed_on == _channel.ClosingReason.COMMAND:
            self.scheduler.cancel(channel.id)

    async def move_to_in_use(self, channel: discord.TextChannel) -> None:
        """Make a channel in-use and schedule it to be made dormant."""
        log.info(f"Moving #{channel} ({channel.id}) to the In Use category.")

        await _channel.move_to_bottom(
            channel=channel,
            category_id=constants.Categories.help_in_use,
        )

        timeout = constants.HelpChannels.idle_minutes_claimant * 60

        log.trace(
            f"Scheduling #{channel} ({channel.id}) to become dormant in {timeout} sec."
        )
        self.scheduler.schedule_later(
            timeout, channel.id, self.move_idle_channel(channel)
        )
        _stats.report_counts()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Move an available channel to the In Use category and replace it with a dormant one."""
        if message.author.bot:
            return  # Ignore messages sent by bots.

        await self.init_task

        if channel_utils.is_in_category(
            message.channel, constants.Categories.help_available
        ):
            if not _channel.is_excluded_channel(message.channel):
                await self.claim_channel(message)

        elif channel_utils.is_in_category(
            message.channel, constants.Categories.help_in_use
        ):
            await self.notify_session_participants(message)
            await _message.update_message_caches(message)

    @commands.Cog.listener()
    async def on_message_delete(self, msg: discord.Message) -> None:
        """
        Reschedule an in-use channel to become dormant sooner if the channel is empty.

        The new time for the dormant task is configured with `HelpChannels.deleted_idle_minutes`.
        """
        await self.init_task

        if not channel_utils.is_in_category(
            msg.channel, constants.Categories.help_in_use
        ):
            return

        if not await _message.is_empty(msg.channel):
            return

        log.info(
            f"Claimant of #{msg.channel} ({msg.author}) deleted message, channel is empty now. Rescheduling task."
        )

        # Cancel existing dormant task before scheduling new.
        self.scheduler.cancel(msg.channel.id)

        delay = constants.HelpChannels.deleted_idle_minutes * 60
        self.scheduler.schedule_later(
            delay, msg.channel.id, self.move_idle_channel(msg.channel)
        )

    async def wait_for_dormant_channel(self) -> discord.TextChannel:
        """Wait for a dormant channel to become available in the queue and return it."""
        log.trace("Waiting for a dormant channel.")

        task = scheduling.create_task(self.channel_queue.get())
        self.queue_tasks.append(task)
        channel = await task

        log.trace(
            f"Channel #{channel} ({channel.id}) finally retrieved from the queue."
        )
        self.queue_tasks.remove(task)

        return channel

    async def update_available_help_channels(self) -> None:
        """Updates the dynamic message within #how-to-get-help for available help channels."""
        available_channels = AVAILABLE_HELP_CHANNELS.format(
            available=", ".join(
                c.mention
                for c in sorted(
                    self.available_help_channels, key=attrgetter("position")
                )
            )
            or None
        )

        if self.dynamic_message is not None:
            try:
                log.trace(
                    "Help channels have changed, dynamic message has been edited."
                )
                await self.bot.http.edit_message(
                    constants.Channels.how_to_get_help,
                    self.dynamic_message,
                    content=available_channels,
                )
            except discord.NotFound:
                pass
            else:
                return

        log.trace("Dynamic message could not be edited or found. Creating a new one.")
        new_dynamic_message = await self.bot.http.send_message(
            constants.Channels.how_to_get_help, available_channels
        )
        self.dynamic_message = new_dynamic_message["id"]
        await _caches.dynamic_message.set("message_id", self.dynamic_message)

    @staticmethod
    def _serialise_session_participants(participants: set[int]) -> str:
        """Convert a set to a comma separated string."""
        return ",".join(str(p) for p in participants)

    @staticmethod
    def _deserialise_session_participants(s: str) -> set[int]:
        """Convert a comma separated string into a set."""
        return set(int(user_id) for user_id in s.split(",") if user_id != "")

    @lock.lock_arg(NAMESPACE, "message", attrgetter("channel.id"))
    @lock.lock_arg(NAMESPACE, "message", attrgetter("author.id"))
    async def notify_session_participants(self, message: discord.Message) -> None:
        """
        Check if the message author meets the requirements to be notified.

        If they meet the requirements they are notified.
        """
        if await _caches.claimants.get(message.channel.id) == message.author.id:
            return  # Ignore messages sent by claimants

        if not await _caches.help_dm.get(message.author.id):
            return  # Ignore message if user is opted out of help dms

        if (await self.bot.get_context(message)).command == self.close_command:
            return  # Ignore messages that are closing the channel

        session_participants = self._deserialise_session_participants(
            await _caches.session_participants.get(message.channel.id) or ""
        )

        if message.author.id not in session_participants:
            session_participants.add(message.author.id)

            embed = discord.Embed(
                title="Currently Helping",
                description=f"You're currently helping in {message.channel.mention}",
                color=constants.Colours.soft_green,
                timestamp=message.created_at,
            )
            embed.add_field(
                name="Conversation", value=f"[Jump to message]({message.jump_url})"
            )

            try:
                await message.author.send(embed=embed)
            except discord.Forbidden:
                log.trace(
                    f"Failed to send helpdm message to {message.author.id}. DMs Closed/Blocked. "
                    "Removing user from helpdm."
                )
                bot_commands_channel = self.bot.get_channel(Channels.bot_commands)
                await _caches.help_dm.delete(message.author.id)
                await bot_commands_channel.send(
                    f"{message.author.mention} {constants.Emojis.cross_mark} "
                    "To receive updates on help channels you're active in, enable your DMs.",
                    delete_after=RedirectOutput.delete_delay,
                )
                return

            await _caches.session_participants.set(
                message.channel.id,
                self._serialise_session_participants(session_participants),
            )

    @commands.command(name="helpdm")
    async def helpdm_command(self, ctx: commands.Context, state_bool: bool) -> None:
        """
        Allows user to toggle "Helping" dms.

        If this is set to on the user will receive a dm for the channel they are participating in.

        If this is set to off the user will not receive a dm for channel that they are participating in.
        """
        state_str = "ON" if state_bool else "OFF"

        if state_bool == await _caches.help_dm.get(ctx.author.id, False):
            await ctx.send(
                f"{constants.Emojis.cross_mark} {ctx.author.mention} Help DMs are already {state_str}"
            )
            return

        if state_bool:
            await _caches.help_dm.set(ctx.author.id, True)
        else:
            await _caches.help_dm.delete(ctx.author.id)
        await ctx.send(
            f"{constants.Emojis.ok_hand} {ctx.author.mention} Help DMs {state_str}!"
        )
