from datetime import datetime, timedelta
from email.parser import HeaderParser
from io import StringIO
from typing import Dict, Optional, Tuple

from discord import Colour, Embed
from discord.ext.commands import Cog, Context, command

from bot.bot import Bot
from bot.constants import Keys
from bot.log import get_logger
from bot.utils import scheduling
from bot.utils.caching import AsyncCache

log = get_logger(__name__)

ICON_URL = "https://www.python.org/static/opengraph-icon-200x200.png"
BASE_PEP_URL = "http://www.python.org/dev/peps/pep-"
PEPS_LISTING_API_URL = "https://api.github.com/repos/python/peps/contents?ref=master"

pep_cache = AsyncCache()

GITHUB_API_HEADERS = {}
if Keys.github:
    GITHUB_API_HEADERS["Authorization"] = f"token {Keys.github}"


class PythonEnhancementProposals(Cog):
    """Cog for displaying information about PEPs."""

    def __init__(self, bot: Bot):
        self.bot = bot
        self.peps: Dict[int, str] = {}
        # To avoid situations where we don't have last datetime, set this to now.
        self.last_refreshed_peps: datetime = datetime.now()
        scheduling.create_task(self.refresh_peps_urls(), event_loop=self.bot.loop)

    async def refresh_peps_urls(self) -> None:
        """Refresh PEP URLs listing in every 3 hours."""
        # Wait until HTTP client is available
        await self.bot.wait_until_ready()
        log.trace("Started refreshing PEP URLs.")
        self.last_refreshed_peps = datetime.now()

        async with self.bot.http_session.get(
            PEPS_LISTING_API_URL, headers=GITHUB_API_HEADERS
        ) as resp:
            if resp.status != 200:
                log.warning(
                    f"Fetching PEP URLs from GitHub API failed with code {resp.status}"
                )
                return

            listing = await resp.json()

        log.trace("Got PEP URLs listing from GitHub API")

        for file in listing:
            name = file["name"]
            if name.startswith("pep-") and name.endswith((".rst", ".txt")):
                pep_number = name.replace("pep-", "").split(".")[0]
                self.peps[int(pep_number)] = file["download_url"]

        log.info("Successfully refreshed PEP URLs listing.")

    @staticmethod
    def get_pep_zero_embed() -> Embed:
        """Get information embed about PEP 0."""
        pep_embed = Embed(
            title="**PEP 0 - Index of Python Enhancement Proposals (PEPs)**",
            url="https://www.python.org/dev/peps/",
        )
        pep_embed.set_thumbnail(url=ICON_URL)
        pep_embed.add_field(name="Status", value="Active")
        pep_embed.add_field(name="Created", value="13-Jul-2000")
        pep_embed.add_field(name="Type", value="Informational")

        return pep_embed

    async def validate_pep_number(self, pep_nr: int) -> Optional[Embed]:
        """Validate is PEP number valid. When it isn't, return error embed, otherwise None."""
        if (
            pep_nr not in self.peps
            and (self.last_refreshed_peps + timedelta(minutes=30)) <= datetime.now()
            and len(str(pep_nr)) < 5
        ):
            await self.refresh_peps_urls()

        if pep_nr not in self.peps:
            log.trace(f"PEP {pep_nr} was not found")
            return Embed(
                title="PEP not found",
                description=f"PEP {pep_nr} does not exist.",
                colour=Colour.red(),
            )

        return None

    def generate_pep_embed(self, pep_header: Dict, pep_nr: int) -> Embed:
        """Generate PEP embed based on PEP headers data."""
        # Assemble the embed
        pep_embed = Embed(
            title=f"**PEP {pep_nr} - {pep_header['Title']}**",
            description=f"[Link]({BASE_PEP_URL}{pep_nr:04})",
        )

        pep_embed.set_thumbnail(url=ICON_URL)

        # Add the interesting information
        fields_to_check = ("Status", "Python-Version", "Created", "Type")
        for field in fields_to_check:
            # Check for a PEP metadata field that is present but has an empty value
            # embed field values can't contain an empty string
            if pep_header.get(field, ""):
                pep_embed.add_field(name=field, value=pep_header[field])

        return pep_embed

    @pep_cache(arg_offset=1)
    async def get_pep_embed(self, pep_nr: int) -> Tuple[Embed, bool]:
        """Fetch, generate and return PEP embed. Second item of return tuple show does getting success."""
        response = await self.bot.http_session.get(self.peps[pep_nr])

        if response.status == 200:
            log.trace(f"PEP {pep_nr} found")
            pep_content = await response.text()

            # Taken from https://github.com/python/peps/blob/master/pep0/pep.py#L179
            pep_header = HeaderParser().parse(StringIO(pep_content))
            return self.generate_pep_embed(pep_header, pep_nr), True
        else:
            log.trace(
                f"The user requested PEP {pep_nr}, but the response had an unexpected status code: {response.status}."
            )
            return (
                Embed(
                    title="Unexpected error",
                    description="Unexpected HTTP error during PEP search. Please let us know.",
                    colour=Colour.red(),
                ),
                False,
            )

    @command(name="pep", aliases=("get_pep", "p"))
    async def pep_command(self, ctx: Context, pep_number: int) -> None:
        """Fetches information about a PEP and sends it to the channel."""
        # Trigger typing in chat to show users that bot is responding
        await ctx.trigger_typing()

        # Handle PEP 0 directly because it's not in .rst or .txt so it can't be accessed like other PEPs.
        if pep_number == 0:
            pep_embed = self.get_pep_zero_embed()
            success = True
        else:
            success = False
            if not (pep_embed := await self.validate_pep_number(pep_number)):
                pep_embed, success = await self.get_pep_embed(pep_number)

        await ctx.send(embed=pep_embed)
        if success:
            log.trace(
                f"PEP {pep_number} getting and sending finished successfully. Increasing stat."
            )
            self.bot.stats.incr(f"pep_fetches.{pep_number}")
        else:
            log.trace(f"Getting PEP {pep_number} failed. Error embed sent.")


def setup(bot: Bot) -> None:
    """Load the PEP cog."""
    bot.add_cog(PythonEnhancementProposals(bot))
