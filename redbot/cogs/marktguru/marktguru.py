import asyncio
import json
import logging
import os
import re
from datetime import datetime
import aiohttp
import discord
from redbot.core import commands
from redbot.core.bot import Red

logger = logging.getLogger("red.marktguru")

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "marktguru_config.json")


class Marktguru(commands.Cog):
    """Daily CRON job and query commands for Marktguru offers (Spezi & Ofenkäse)."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.last_run_date = None
        self._config_cache = self.load_config()
        self.cron_task = asyncio.create_task(self.cron_loop())

    def cog_unload(self):
        self.cron_task.cancel()
        logger.info("Marktguru daily CRON task canceled.")

    def load_config(self) -> dict:
        """Loads configuration from the marktguru_config.json file."""
        default_config = {
            "cron_time": "08:00",
            "channel_id": None,
            "zip_code": "60487",
            "x_apikey": "",
            "x_clientkey": ""
        }
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    # Merge defaults for any missing keys
                    for k, v in default_config.items():
                        if k not in data:
                            data[k] = v
                    self._config_cache = data
                    return data
            except Exception as e:
                logger.error(f"Error loading marktguru config: {e}")
        
        # Save default config if not existing or failed to load
        self._config_cache = default_config
        self.save_config(default_config)
        return default_config

    def save_config(self, config_data: dict):
        """Saves configuration to the marktguru_config.json file."""
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(config_data, f, indent=4, ensure_ascii=False)
            self._config_cache = config_data
        except Exception as e:
            logger.error(f"Error saving marktguru config: {e}")

    async def retrieve_api_keys(self, session: aiohttp.ClientSession) -> tuple:
        """Dynamically retrieves the latest API and Client keys from the Marktguru website."""
        url = "https://www.marktguru.de/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        try:
            async with session.get(url, headers=headers, timeout=10) as response:
                if response.status == 200:
                    html_content = await response.text()
                    # Search for API keys in Javascript strings or JSON config in the page source
                    apikey_match = re.search(r'["\']apiKey["\']\s*:\s*["\'](.*?)["\']', html_content, re.IGNORECASE)
                    clientkey_match = re.search(r'["\']clientKey["\']\s*:\s*["\'](.*?)["\']', html_content, re.IGNORECASE)
                    
                    x_apikey = apikey_match.group(1) if apikey_match else None
                    x_clientkey = clientkey_match.group(1) if clientkey_match else None
                    
                    if x_apikey and x_clientkey:
                        logger.debug("Successfully retrieved dynamic API keys from Marktguru website.")
                        return x_apikey, x_clientkey
        except Exception as e:
            logger.error(f"Failed to dynamically retrieve API keys: {e}")
        return None, None

    async def fetch_offers(self, session: aiohttp.ClientSession, query: str, zip_code: str, x_apikey: str, x_clientkey: str) -> list:
        """Queries the Marktguru API for offers matching a product query."""
        api_url = "https://api.marktguru.de/api/v1/offers/search"
        params = {
            "as": "web",
            "limit": "100",
            "offset": "0",
            "q": query,
            "zipCode": zip_code
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "x-apikey": x_apikey,
            "x-clientkey": x_clientkey,
            "Content-Type": "application/json"
        }
        try:
            async with session.get(api_url, headers=headers, params=params, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("results", [])
                else:
                    text = await response.text()
                    logger.error(f"Marktguru search API returned status {response.status} for query '{query}': {text}")
        except Exception as e:
            logger.error(f"Exception occurred while calling Marktguru API for query '{query}': {e}")
        return []

    def classify_and_filter_offers(self, results: list) -> tuple:
        """Classifies raw API results into Paulaner Spezi, Paulaner Spezi Zero, and Rougette Ofenkäse."""
        spezi_list = []
        spezi_zero_list = []
        ofenkaese_list = []
        
        seen_offers = set()

        for offer in results:
            product_name = offer.get("product", {}).get("name") or ""
            brand_name = offer.get("brand", {}).get("name") or ""
            description = offer.get("description") or ""

            prod_lower = product_name.lower()
            brand_lower = brand_name.lower()
            desc_lower = description.lower()

            # Unique key for deduplication
            price = offer.get("price")
            advertiser = offer.get("advertisers", [{}])[0].get("name") or "Unknown"
            offer_key = (prod_lower, advertiser, price)
            if offer_key in seen_offers:
                continue
            seen_offers.add(offer_key)

            # Check if it matches Rougette Ofenkäse
            is_rougette = "rougette" in brand_lower or "rougette" in prod_lower
            is_ofenkaese = "ofenkäse" in prod_lower or "ofen" in prod_lower or "ofenkäse" in desc_lower
            
            if is_rougette and is_ofenkaese:
                ofenkaese_list.append(offer)
                continue

            # Check if it matches Paulaner Spezi or Paulaner Spezi Zero
            is_paulaner = "paulaner" in brand_lower or "paulaner" in prod_lower
            is_spezi = "spezi" in prod_lower or "spezi" in desc_lower

            if is_paulaner and is_spezi:
                is_zero = "zero" in prod_lower or "zero" in desc_lower or "0%" in prod_lower
                if is_zero:
                    spezi_zero_list.append(offer)
                else:
                    spezi_list.append(offer)

        return spezi_list, spezi_zero_list, ofenkaese_list

    def format_offer_line(self, offer: dict) -> str:
        """Formats a single offer dictionary into a clean markdown string."""
        store = offer.get("advertisers", [{}])[0].get("name") or "Unbekannter Laden"
        price = offer.get("price")
        ref_price = offer.get("referencePrice")
        description = offer.get("description") or ""
        
        # Format price
        price_str = f"{price:.2f}€" if price is not None else "N/A"
        
        # Savings info
        savings_str = ""
        if price is not None and ref_price is not None:
            try:
                savings = float(ref_price) - float(price)
                if savings > 0:
                    savings_str = f" *(UVP: {ref_price:.2f}€, gespart: {savings:.2f}€)*"
            except (ValueError, TypeError):
                pass

        # Validity Dates
        valid_str = ""
        if "validityDates" in offer and offer["validityDates"]:
            v = offer["validityDates"][0]
            if "from" in v and "to" in v:
                try:
                    frm = datetime.fromisoformat(v["from"].replace("Z", "+00:00")).strftime("%d.%m")
                    to = datetime.fromisoformat(v["to"].replace("Z", "+00:00")).strftime("%d.%m")
                    valid_str = f" • *bis {to}*"
                except Exception:
                    pass

        # Shorten description if too long
        desc_str = f" ({description})" if description else ""
        if len(desc_str) > 60:
            desc_str = desc_str[:57] + "...)"

        return f"• **{store}**: {price_str}{savings_str}{valid_str}{desc_str}"

    async def get_combined_offers(self, zip_code: str) -> tuple:
        """Fetches and merges offers for Spezi, Spezi Zero, and Ofenkäse in the given zip code."""
        async with aiohttp.ClientSession() as session:
            # 1. Retrieve API Keys dynamically
            x_apikey, x_clientkey = await self.retrieve_api_keys(session)
            
            # Fallback to configured keys if dynamic scraping fails
            config = self._config_cache
            if not x_apikey or not x_clientkey:
                x_apikey = config.get("x_apikey")
                x_clientkey = config.get("x_clientkey")
                
            if not x_apikey or not x_clientkey:
                raise ValueError("No valid API keys available (dynamic retrieval failed and config keys are empty).")

            # 2. Query separate keywords to cover all targets
            results_spezi = await self.fetch_offers(session, "Paulaner Spezi", zip_code, x_apikey, x_clientkey)
            results_ofenkaese = await self.fetch_offers(session, "Ofenkäse", zip_code, x_apikey, x_clientkey)
            
            # Merge and classify
            all_raw_results = results_spezi + results_ofenkaese
            return self.classify_and_filter_offers(all_raw_results)

    async def build_embed(self, zip_code: str) -> discord.Embed:
        """Builds a beautiful Discord Embed containing the classified offers."""
        embed = discord.Embed(
            title="🛒 Marktguru Angebote",
            description=f"Aktuelle Angebote für deinen Bereich (PLZ: `{zip_code}`)",
            color=discord.Color.from_rgb(255, 123, 0),
            timestamp=datetime.now()
        )
        
        try:
            spezi, zero, ofenkaese = await self.get_combined_offers(zip_code)
            
            # 1. Paulaner Spezi
            if spezi:
                lines = [self.format_offer_line(o) for o in spezi]
                embed.add_field(name="🥤 Paulaner Spezi", value="\n".join(lines)[:1024], inline=False)
            else:
                embed.add_field(name="🥤 Paulaner Spezi", value="*Keine aktuellen Angebote gefunden.*", inline=False)

            # 2. Paulaner Spezi Zero
            if zero:
                lines = [self.format_offer_line(o) for o in zero]
                embed.add_field(name="🥤 Paulaner Spezi Zero", value="\n".join(lines)[:1024], inline=False)
            else:
                embed.add_field(name="🥤 Paulaner Spezi Zero", value="*Keine aktuellen Angebote gefunden.*", inline=False)

            # 3. Rougette Ofenkäse
            if ofenkaese:
                lines = [self.format_offer_line(o) for o in ofenkaese]
                embed.add_field(name="🧀 Rougette Ofenkäse", value="\n".join(lines)[:1024], inline=False)
            else:
                embed.add_field(name="🧀 Rougette Ofenkäse", value="*Keine aktuellen Angebote gefunden.*", inline=False)

        except Exception as e:
            embed.color = discord.Color.red()
            embed.add_field(
                name="⚠️ Fehler bei der Abfrage",
                value=f"Die Angebote konnten nicht geladen werden.\nDetails: `{str(e)}`",
                inline=False
            )
            
        embed.set_footer(text="Marktguru API • Alle Angaben ohne Gewähr")
        return embed

    async def run_and_send(self, target: discord.abc.Messageable, zip_code: str, config: dict):
        """Builds the embed and sends it to the target channel."""
        async with target.typing():
            embed = await self.build_embed(zip_code)
            await target.send(embed=embed)

    async def cron_loop(self):
        """Background task checking if it is time to run the daily cron job."""
        await self.bot.wait_until_ready()
        logger.info("Marktguru daily CRON loop initialized.")
        while True:
            try:
                config = self.load_config()
                cron_time_str = config.get("cron_time", "08:00")
                
                now = datetime.now()
                current_time = now.strftime("%H:%M")
                current_date = now.strftime("%Y-%m-%d")
                
                if current_time == cron_time_str and self.last_run_date != current_date:
                    channel_id = config.get("channel_id")
                    if channel_id:
                        channel = self.bot.get_channel(int(channel_id))
                        if channel:
                            logger.info(f"Triggering daily Marktguru cron search for PLZ {config.get('zip_code')}")
                            await self.run_and_send(channel, zip_code=config.get("zip_code", "60487"), config=config)
                            self.last_run_date = current_date
                        else:
                            logger.error(f"Marktguru daily channel ID {channel_id} could not be resolved.")
                    else:
                        logger.warning("Marktguru daily CRON triggered, but no channel_id is configured.")
                    # Mark run today regardless to prevent repeating in the same minute
                    self.last_run_date = current_date
            except Exception as e:
                logger.error(f"Error in Marktguru cron loop: {e}", exc_info=True)
                
            await asyncio.sleep(30)  # Check every 30 seconds

    # ==========================================
    # DISCORD COMMANDS
    # ==========================================

    @commands.command(name="mast")
    async def cmd_mast(self, ctx: commands.Context, zip_code: str = None):
        """Query Spezi and Ofenkäse offers for a specific ZIP code.
        
        If no ZIP code is provided, the default ZIP code from configuration is used.
        """
        if not zip_code:
            config = self.load_config()
            zip_code = config.get("zip_code", "60487")

        if not re.match(r"^\d{5}$", zip_code):
            await ctx.send("❌ Bitte gib eine gültige 5-stellige Postleitzahl an.")
            return

        await self.run_and_send(ctx, zip_code=zip_code, config=self._config_cache)

    @commands.group(name="marktguru")
    async def marktguru_group(self, ctx: commands.Context):
        """Marktguru configuration and management commands."""
        pass

    @marktguru_group.command(name="run")
    @commands.admin_or_permissions(manage_guild=True)
    async def cmd_run(self, ctx: commands.Context):
        """Manually trigger the daily Marktguru offer search to the configured channel."""
        config = self.load_config()
        channel_id = config.get("channel_id")
        if not channel_id:
            await ctx.send("❌ Kein Kanal konfiguriert. Verwende zuerst `[p]marktguru setchannel`.")
            return

        channel = self.bot.get_channel(int(channel_id))
        if not channel:
            await ctx.send(f"❌ Konfigurierter Kanal mit ID `{channel_id}` wurde nicht gefunden.")
            return

        await ctx.send(f"⏳ Starte Abfrage und sende Ergebnisse an {channel.mention}...")
        await self.run_and_send(channel, zip_code=config.get("zip_code", "60487"), config=config)

    @marktguru_group.command(name="status")
    @commands.admin_or_permissions(manage_guild=True)
    async def cmd_status(self, ctx: commands.Context):
        """Show current Marktguru daily task configuration."""
        config = self.load_config()
        channel_id = config.get("channel_id")
        channel_mention = "Keiner"
        if channel_id:
            channel = self.bot.get_channel(int(channel_id))
            if channel:
                channel_mention = channel.mention
            else:
                channel_mention = f"ID: {channel_id} (nicht gefunden)"

        has_keys = bool(config.get("x_apikey")) and bool(config.get("x_clientkey"))
        
        embed = discord.Embed(
            title="Marktguru Cog Status",
            color=discord.Color.blue()
        )
        embed.add_field(name="Uhrzeit (CRON)", value=config.get("cron_time", "08:00"), inline=True)
        embed.add_field(name="Standard PLZ (ZIP)", value=config.get("zip_code", "60487"), inline=True)
        embed.add_field(name="Zielkanal", value=channel_mention, inline=True)
        embed.add_field(name="Manuelle Keys konfiguriert?", value="Ja" if has_keys else "Nein (Nutzt Auto-Scraping)", inline=False)
        embed.add_field(name="Letzter täglicher Suchlauf (Datum)", value=str(self.last_run_date or "Noch nicht gelaufen"), inline=True)
        
        await ctx.send(embed=embed)

    @marktguru_group.command(name="setchannel")
    @commands.admin_or_permissions(manage_guild=True)
    async def cmd_setchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the channel where daily offers are posted."""
        config = self.load_config()
        config["channel_id"] = channel.id
        self.save_config(config)
        await ctx.send(f"✅ Kanal erfolgreich auf {channel.mention} festgelegt.")

    @marktguru_group.command(name="settime")
    @commands.admin_or_permissions(manage_guild=True)
    async def cmd_settime(self, ctx: commands.Context, time_str: str):
        """Set the daily execution time (format: HH:MM, e.g. 08:30)."""
        if not re.match(r"^(0[0-9]|1[0-9]|2[0-3]):[0-5][0-9]$", time_str):
            await ctx.send("❌ Ungültiges Zeitformat! Bitte verwende `HH:MM` (z. B. `08:00` oder `18:30`).")
            return

        config = self.load_config()
        config["cron_time"] = time_str
        self.save_config(config)
        # Reset last run date to allow running today if the new time is in the future
        self.last_run_date = None
        await ctx.send(f"✅ Uhrzeit für den täglichen Suchlauf auf `{time_str}` festgelegt.")

    @marktguru_group.command(name="setzip")
    @commands.admin_or_permissions(manage_guild=True)
    async def cmd_setzip(self, ctx: commands.Context, zip_code: str):
        """Set the default ZIP code for search queries (must be 5 digits)."""
        if not re.match(r"^\d{5}$", zip_code):
            await ctx.send("❌ Ungültige Postleitzahl! Bitte gib eine 5-stellige Zahl ein.")
            return

        config = self.load_config()
        config["zip_code"] = zip_code
        self.save_config(config)
        await ctx.send(f"✅ Standard-Postleitzahl auf `{zip_code}` festgelegt.")
