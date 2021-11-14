from typing import Iterable

from bot.rules import mentions
from tests.bot.rules import DisallowedCase, RuleTest
from tests.helpers import MockMember, MockMessage


def make_msg(
    author: str, total_user_mentions: int, total_bot_mentions: int = 0
) -> MockMessage:
    """Makes a message with `total_mentions` mentions."""
    user_mentions = [MockMember() for _ in range(total_user_mentions)]
    bot_mentions = [MockMember(bot=True) for _ in range(total_bot_mentions)]
    return MockMessage(author=author, mentions=user_mentions + bot_mentions)


class TestMentions(RuleTest):
    """Tests applying the `mentions` antispam rule."""

    def setUp(self):
        self.apply = mentions.apply
        self.config = {
            "max": 2,
            "interval": 10,
        }

    async def test_mentions_within_limit(self):
        """Messages with an allowed amount of mentions."""
        cases = (
            [make_msg("bob", 0)],
            [make_msg("bob", 2)],
            [make_msg("bob", 1), make_msg("bob", 1)],
            [make_msg("bob", 1), make_msg("alice", 2)],
        )

        await self.run_allowed(cases)

    async def test_mentions_exceeding_limit(self):
        """Messages with a higher than allowed amount of mentions."""
        cases = (
            DisallowedCase(
                [make_msg("bob", 3)],
                ("bob",),
                3,
            ),
            DisallowedCase(
                [make_msg("alice", 2), make_msg("alice", 0), make_msg("alice", 1)],
                ("alice",),
                3,
            ),
            DisallowedCase(
                [make_msg("bob", 2), make_msg("alice", 3), make_msg("bob", 2)],
                ("bob",),
                4,
            ),
            DisallowedCase(
                [make_msg("bob", 3, 1)],
                ("bob",),
                3,
            ),
        )

        await self.run_disallowed(cases)

    async def test_ignore_bot_mentions(self):
        """Messages with an allowed amount of mentions, also containing bot mentions."""
        cases = (
            [make_msg("bob", 0, 3)],
            [make_msg("bob", 2, 1)],
            [make_msg("bob", 1, 2), make_msg("bob", 1, 2)],
            [make_msg("bob", 1, 5), make_msg("alice", 2, 5)],
        )

        await self.run_allowed(cases)

    def relevant_messages(self, case: DisallowedCase) -> Iterable[MockMessage]:
        last_message = case.recent_messages[0]
        return tuple(
            msg for msg in case.recent_messages if msg.author == last_message.author
        )

    def get_report(self, case: DisallowedCase) -> str:
        return f"sent {case.n_violations} mentions in {self.config['interval']}s"
