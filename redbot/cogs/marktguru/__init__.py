from redbot.core.bot import Red
from .marktguru import Marktguru


async def setup(bot: Red) -> None:
    await bot.add_cog(Marktguru(bot))
