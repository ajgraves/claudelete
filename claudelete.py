import discord
from discord import app_commands, HTTPException, NotFound, Forbidden
from discord.errors import RateLimited
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import pytz
import asyncio
import MySQLdb
from MySQLdb import Error
import time
import cdconfig

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

class AutoDeleteBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        print(f"Synced slash commands for {self.user}")

bot = AutoDeleteBot()

# Database configuration is now in config.py

def create_connection():
    try:
        connection = MySQLdb.connect(**cdconfig.DB_CONFIG)
        return connection
    except Error as e:
        print(f"Error connecting to MySQL Database: {e}")
        return None

def init_database():
    connection = create_connection()
    if connection:
        try:
            cursor = connection.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS channel_config (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    guild_id BIGINT,
                    channel_id BIGINT,
                    delete_after INT,
                    UNIQUE KEY guild_channel (guild_id, channel_id)
                )
            """)
            connection.commit()
        except Error as e:
            print(f"Error creating table: {e}")
        finally:
            cursor.close()
            connection.close()

def convert_to_minutes(time: int, unit: str) -> int:
    unit = unit.lower()
    if unit in ['m', 'minute', 'minutes']:
        return time
    elif unit in ['h', 'hour', 'hours']:
        return time * 60
    elif unit in ['d', 'day', 'days']:
        return time * 24 * 60
    elif unit in ['w', 'week', 'weeks']:
        return time * 7 * 24 * 60
    else:
        raise ValueError("Invalid time unit. Please use minutes, hours, days, or weeks.")

def format_time(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    elif minutes < 1440:  # 24 * 60
        hours = minutes // 60
        minutes_remainder = minutes % 60
        if minutes_remainder == 0:
            return f"{hours} hour{'s' if hours != 1 else ''}"
        else:
            return f"{hours} hour{'s' if hours != 1 else ''} {minutes_remainder} minute{'s' if minutes_remainder != 1 else ''}"
    elif minutes < 10080:  # 7 * 24 * 60
        days = minutes // 1440
        hours_remainder = minutes % 1440 // 60
        if hours_remainder == 0:
            return f"{days} day{'s' if days != 1 else ''}"
        else:
            return f"{days} day{'s' if days != 1 else ''} {hours_remainder} hour{'s' if hours_remainder != 1 else ''}"
    else:
        weeks = minutes // 10080
        days_remainder = minutes % 10080 // 1440
        if days_remainder == 0:
            return f"{weeks} week{'s' if weeks != 1 else ''}"
        else:
            return f"{weeks} week{'s' if weeks != 1 else ''} {days_remainder} day{'s' if days_remainder != 1 else ''}"

@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')
    init_database()
    delete_old_messages.start()

@tasks.loop(minutes=1)
async def delete_old_messages():
    connection = create_connection()
    if connection:
        try:
            cursor = connection.cursor(MySQLdb.cursors.DictCursor)
            cursor.execute("SELECT * FROM channel_config")
            configs = cursor.fetchall()
            
            for config in configs:
                guild = bot.get_guild(config['guild_id'])
                if guild:
                    channel = guild.get_channel(config['channel_id'])
                    if channel:
                        delete_after = timedelta(minutes=config['delete_after'])
                        utc_now = datetime.now(pytz.utc)
                        async for message in channel.history(limit=None):
                            message_time = message.created_at.replace(tzinfo=pytz.utc)
                            if utc_now - message_time > delete_after:
                                try:
                                    await message.delete()
                                    try:
                                        # Encode the guild and channel names to handle any Unicode characters
                                        print(f'Deleted message in {guild.name.encode("utf-8", "replace").decode("utf-8")} - {channel.name.encode("utf-8", "replace").decode("utf-8")}...')
                                    except UnicodeEncodeError:
                                        print(f'Deleted message in a guild/channel with unsupported characters...')
                                except discord.errors.NotFound:
                                    try:
                                        print(f'Message already deleted in {guild.name.encode("utf-8", "replace").decode("utf-8")} - {channel.name.encode("utf-8", "replace").decode("utf-8")}...')
                                    except UnicodeEncodeError:
                                        print(f'Message already deleted in a guild/channel with unsupported characters...')
                                except discord.Forbidden:
                                    try:
                                        print(f'Forbidden to delete message in {guild.name.encode("utf-8", "replace").decode("utf-8")} - {channel.name.encode("utf-8", "replace").decode("utf-8")}')
                                    except UnicodeEncodeError:
                                        print(f'Forbidden to delete message in a guild/channel with unsupported characters...')
                                    await asyncio.sleep(60)  # Wait 1 minute before trying again
                                except discord.RateLimited as e:
                                    print(f'Discord is rate limiting me, I am sleeping for {e.retry_after}...')
                                    await asyncio.sleep(e.retry_after)  # Wait for the recommended retry time
                                except discord.HTTPException as e:
                                    if e.status == 503:
                                        try:
                                            print(f'HTTP 503 error in {guild.name.encode("utf-8", "replace").decode("utf-8")} - {channel.name.encode("utf-8", "replace").decode("utf-8")}: {e}')
                                        except UnicodeEncodeError:
                                            print(f'HTTP 503 error in a guild/channel with unsupported characters...')
                                        await asyncio.sleep(60)  # Wait 1 minute before trying again
                                    else:
                                        try:
                                            print(f'Error deleting message in {guild.name.encode("utf-8", "replace").decode("utf-8")} - {channel.name.encode("utf-8", "replace").decode("utf-8")}: {e}')
                                        except UnicodeEncodeError:
                                            print(f'Error deleting message in a guild/channel with unsupported characters...')
                                    await asyncio.sleep(e.retry_after if hasattr(e, 'retry_after') else 5)  # Wait for the recommended retry time or 5 seconds
                                except discord.ConnectionClosed:
                                    try:
                                        print(f'Connection closed while deleting message in {guild.name.encode("utf-8", "replace").decode("utf-8")} - {channel.name.encode("utf-8", "replace").decode("utf-8")}')
                                    except UnicodeEncodeError:
                                        print(f'Connection closed while deleting message in a guild/channel with unsupported characters...')
                                    await asyncio.sleep(30)  # Wait 30 seconds before trying again
                                except asyncio.TimeoutError:
                                    try:
                                        print(f'Timeout while deleting message in {guild.name.encode("utf-8", "replace").decode("utf-8")} - {channel.name.encode("utf-8", "replace").decode("utf-8")}')
                                    except UnicodeEncodeError:
                                        print(f'Timeout while deleting message in a guild/channel with unsupported characters...')
                                    await asyncio.sleep(10)  # Wait 10 seconds before trying again
                                await asyncio.sleep(1)  # To avoid hitting rate limits
        except Error as e:
            print(f"Error reading from database: {e}")
        finally:
            cursor.close()
            connection.close()

def get_text_channels(guild):
    return [channel for channel in guild.channels if isinstance(channel, discord.TextChannel)]

@bot.tree.command(name="add_channel", description="Add a channel to auto-delete messages")
@app_commands.describe(
    channel="The channel to add auto-delete to",
    time="The amount of time before messages are deleted",
    unit="The unit of time (minutes, hours, days, weeks)"
)
@app_commands.choices(unit=[
    app_commands.Choice(name="Minutes", value="minutes"),
    app_commands.Choice(name="Hours", value="hours"),
    app_commands.Choice(name="Days", value="days"),
    app_commands.Choice(name="Weeks", value="weeks")
])
@app_commands.checks.has_permissions(manage_channels=True)
async def add_channel(interaction: discord.Interaction, channel: discord.TextChannel, time: int, unit: str):
    try:
        minutes = convert_to_minutes(time, unit)
    except ValueError as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return

    connection = create_connection()
    if connection:
        try:
            cursor = connection.cursor()
            sql = "INSERT INTO channel_config (guild_id, channel_id, delete_after) VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE delete_after = %s"
            val = (interaction.guild_id, channel.id, minutes, minutes)
            cursor.execute(sql, val)
            connection.commit()
            await interaction.response.send_message(f'Channel {channel.name} added. Messages will be deleted after {format_time(minutes)}.')
        except Error as e:
            print(f"Error adding channel: {e}")
            await interaction.response.send_message("An error occurred while adding the channel.", ephemeral=True)
        finally:
            cursor.close()
            connection.close()

@bot.tree.command(name="remove_channel", description="Remove a channel from auto-delete")
@app_commands.describe(channel="The channel to remove from auto-delete")
@app_commands.checks.has_permissions(manage_channels=True)
async def remove_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    connection = create_connection()
    if connection:
        try:
            cursor = connection.cursor()
            sql = "DELETE FROM channel_config WHERE guild_id = %s AND channel_id = %s"
            val = (interaction.guild_id, channel.id)
            cursor.execute(sql, val)
            connection.commit()
            if cursor.rowcount > 0:
                await interaction.response.send_message(f'Channel {channel.name} removed from auto-delete.')
            else:
                await interaction.response.send_message(f'Channel {channel.name} was not in the auto-delete list.')
        except Error as e:
            print(f"Error removing channel: {e}")
            await interaction.response.send_message("An error occurred while removing the channel.", ephemeral=True)
        finally:
            cursor.close()
            connection.close()

@bot.tree.command(name="update_time", description="Update the auto-delete time for a channel")
@app_commands.describe(
    channel="The channel to update",
    time="The new amount of time before messages are deleted",
    unit="The unit of time (minutes, hours, days, weeks)"
)
@app_commands.choices(unit=[
    app_commands.Choice(name="Minutes", value="minutes"),
    app_commands.Choice(name="Hours", value="hours"),
    app_commands.Choice(name="Days", value="days"),
    app_commands.Choice(name="Weeks", value="weeks")
])
@app_commands.checks.has_permissions(manage_channels=True)
async def update_time(interaction: discord.Interaction, channel: discord.TextChannel, time: int, unit: str):
    try:
        minutes = convert_to_minutes(time, unit)
    except ValueError as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return

    connection = create_connection()
    if connection:
        try:
            cursor = connection.cursor()
            sql = "UPDATE channel_config SET delete_after = %s WHERE guild_id = %s AND channel_id = %s"
            val = (minutes, interaction.guild_id, channel.id)
            cursor.execute(sql, val)
            connection.commit()
            if cursor.rowcount > 0:
                await interaction.response.send_message(f'Updated: Messages in {channel.name} will now be deleted after {format_time(minutes)}.')
            else:
                await interaction.response.send_message(f'Channel {channel.name} is not in the auto-delete list. Add it first.')
        except Error as e:
            print(f"Error updating channel: {e}")
            await interaction.response.send_message("An error occurred while updating the channel.", ephemeral=True)
        finally:
            cursor.close()
            connection.close()

@bot.tree.command(name="list_channels", description="List all channels with auto-delete enabled")
async def list_channels(interaction: discord.Interaction):
    connection = create_connection()
    if connection:
        try:
            cursor = connection.cursor(MySQLdb.cursors.DictCursor)
            sql = "SELECT channel_id, delete_after FROM channel_config WHERE guild_id = %s"
            val = (interaction.guild_id,)
            cursor.execute(sql, val)
            channels = cursor.fetchall()
            
            if channels:
                message = "Channels with auto-delete in this server:\n"
                for channel_data in channels:
                    channel = interaction.guild.get_channel(channel_data['channel_id'])
                    if channel:
                        message += f"- {channel.name}: {format_time(channel_data['delete_after'])}\n"
                await interaction.response.send_message(message)
            else:
                await interaction.response.send_message("No channels are currently set for auto-delete in this server.")
        except Error as e:
            print(f"Error listing channels: {e}")
            await interaction.response.send_message("An error occurred while listing the channels.", ephemeral=True)
        finally:
            cursor.close()
            connection.close()

@bot.tree.command(name="purge_user", description="Purge all messages from a single user")
@app_commands.describe(username="The username of the user whose messages to purge")
@app_commands.checks.has_permissions(administrator=True)
async def purge_user(interaction: discord.Interaction, username: str):
    await interaction.response.defer(ephemeral=True)
    
    try:
        print(f"Purging messages from user {username} in {interaction.guild.name.encode('utf-8', 'replace').decode('utf-8')}")
    except UnicodeEncodeError:
        print(f"Purging messages from user {username} in a guild with unsupported characters")

    purged_count = 0
    for channel in interaction.guild.text_channels:
        try:
            async for message in channel.history(limit=None):
                if message.author.name == username:
                    try:
                        await message.delete()
                        purged_count += 1
                        try:
                            print(f"Purged message in {interaction.guild.name.encode('utf-8', 'replace').decode('utf-8')} - {channel.name.encode('utf-8', 'replace').decode('utf-8')}")
                        except UnicodeEncodeError:
                            print(f"Purged message in a guild/channel with unsupported characters")
                    except discord.errors.Forbidden:
                        await interaction.followup.send(f"I don't have permission to purge messages in {channel.name}.", ephemeral=True)
                        try:
                            print(f"No permission to purge messages in {interaction.guild.name.encode('utf-8', 'replace').decode('utf-8')} - {channel.name.encode('utf-8', 'replace').decode('utf-8')}")
                        except UnicodeEncodeError:
                            print(f"No permission to purge messages in a guild/channel with unsupported characters")
                        break  # Move to the next channel
                    except discord.errors.NotFound:
                        # Message was already deleted, continue to the next one
                        try:
                            print(f"Message already purged in {interaction.guild.name.encode('utf-8', 'replace').decode('utf-8')} - {channel.name.encode('utf-8', 'replace').decode('utf-8')}")
                        except UnicodeEncodeError:
                            print(f"Message already purged in a guild/channel with unsupported characters")
                        continue
                    except discord.errors.HTTPException as e:
                        if e.status == 503:
                            await interaction.followup.send(f"Discord service unavailable. Retrying in 60 seconds.", ephemeral=True)
                            try:
                                print(f"HTTP 503 error in {interaction.guild.name.encode('utf-8', 'replace').decode('utf-8')} - {channel.name.encode('utf-8', 'replace').decode('utf-8')}: {str(e)}")
                            except UnicodeEncodeError:
                                print(f"HTTP 503 error in a guild/channel with unsupported characters: {str(e)}")
                            await asyncio.sleep(60)  # Wait 60 seconds before retrying
                            continue
                        elif e.code == 50001:  # Missing Access
                            await interaction.followup.send(f"Missing access to channel {channel.name}. Skipping.", ephemeral=True)
                            try:
                                print(f"Missing access to channel in {interaction.guild.name.encode('utf-8', 'replace').decode('utf-8')} - {channel.name.encode('utf-8', 'replace').decode('utf-8')}")
                            except UnicodeEncodeError:
                                print(f"Missing access to channel in a guild/channel with unsupported characters")
                            break  # Move to the next channel
                        else:
                            await interaction.followup.send(f"An error occurred while purging a message in {channel.name}: {str(e)}", ephemeral=True)
                            try:
                                print(f"Error purging message in {interaction.guild.name.encode('utf-8', 'replace').decode('utf-8')} - {channel.name.encode('utf-8', 'replace').decode('utf-8')}: {str(e)}")
                            except UnicodeEncodeError:
                                print(f"Error purging message in a guild/channel with unsupported characters: {str(e)}")
                            await asyncio.sleep(5)  # Wait 5 seconds before trying the next message
                    except discord.errors.RateLimited as e:
                        retry_after = e.retry_after
                        await interaction.followup.send(f"Rate limited. Waiting for {retry_after:.2f} seconds before continuing.", ephemeral=True)
                        try:
                            print(f"Rate limited in {interaction.guild.name.encode('utf-8', 'replace').decode('utf-8')} - {channel.name.encode('utf-8', 'replace').decode('utf-8')}. Waiting for {retry_after:.2f} seconds.")
                        except UnicodeEncodeError:
                            print(f"Rate limited in a guild/channel with unsupported characters. Waiting for {retry_after:.2f} seconds.")
                        await asyncio.sleep(retry_after)
                    except discord.ConnectionClosed:
                        await interaction.followup.send(f"Connection to Discord closed. Retrying in 30 seconds.", ephemeral=True)
                        try:
                            print(f"Connection closed while purging message in {interaction.guild.name.encode('utf-8', 'replace').decode('utf-8')} - {channel.name.encode('utf-8', 'replace').decode('utf-8')}")
                        except UnicodeEncodeError:
                            print(f"Connection closed while purging message in a guild/channel with unsupported characters")
                        await asyncio.sleep(30)  # Wait 30 seconds before retrying
                        continue
                    except asyncio.TimeoutError:
                        await interaction.followup.send(f"Operation timed out. Retrying in 10 seconds.", ephemeral=True)
                        try:
                            print(f"Timeout while purging message in {interaction.guild.name.encode('utf-8', 'replace').decode('utf-8')} - {channel.name.encode('utf-8', 'replace').decode('utf-8')}")
                        except UnicodeEncodeError:
                            print(f"Timeout while purging message in a guild/channel with unsupported characters")
                        await asyncio.sleep(10)  # Wait 10 seconds before retrying
                        continue
        except discord.errors.Forbidden:
            await interaction.followup.send(f"I don't have permission to access messages in {channel.name}. Skipping this channel.", ephemeral=True)
            try:
                print(f"No permission to access messages in {interaction.guild.name.encode('utf-8', 'replace').decode('utf-8')} - {channel.name.encode('utf-8', 'replace').decode('utf-8')}")
            except UnicodeEncodeError:
                print(f"No permission to access messages in a guild/channel with unsupported characters")
        except discord.errors.HTTPException as e:
            await interaction.followup.send(f"An error occurred while accessing messages in {channel.name}: {str(e)}", ephemeral=True)
            try:
                print(f"Error accessing messages in {interaction.guild.name.encode('utf-8', 'replace').decode('utf-8')} - {channel.name.encode('utf-8', 'replace').decode('utf-8')}: {str(e)}")
            except UnicodeEncodeError:
                print(f"Error accessing messages in a guild/channel with unsupported characters: {str(e)}")
            await asyncio.sleep(5)  # Wait 5 seconds before moving to the next channel

    await interaction.followup.send(f"Purged {purged_count} messages from user {username}.", ephemeral=True)
    try:
        print(f"Purged {purged_count} messages from user {username} in {interaction.guild.name.encode('utf-8', 'replace').decode('utf-8')}")
    except UnicodeEncodeError:
        print(f"Purged {purged_count} messages from user {username} in a guild with unsupported characters")

# Check if the bot is alive
@bot.tree.command(name="ping", description="Check if the bot is responsive")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message('Pong!')

# The below commands are only for fun, and are absolutely not needed in any way, shape or form for the bot to function.
@bot.tree.command(name="marco", description="Play Marco Polo")
async def marco(interaction: discord.Interaction):
    await interaction.response.send_message("Polo!")

@bot.tree.command(name="sneaky", description="Very sneaky...")
async def sneaky(interaction: discord.Interaction):
    await interaction.response.send_message("You fargin sneaky bastage!")

bot.run(cdconfig.BOT_TOKEN)  # Replace with your actual bot token
