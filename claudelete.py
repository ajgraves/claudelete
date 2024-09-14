import discord
from discord import app_commands, HTTPException, NotFound, Forbidden
from discord.app_commands import MissingPermissions
from discord.errors import RateLimited, HTTPException
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import pytz
import asyncio
import MySQLdb
from MySQLdb import Error
import time
import random
from typing import List, Tuple
import subprocess
import cdconfig

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

## Class definitions
class AutoDeleteBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        print(f"Synced slash commands for {self.user}")

bot = AutoDeleteBot()

class RateLimiter:
    def __init__(self, max_calls: int, period: float):
        self.max_calls = max_calls
        self.period = period
        self.calls = []
        self.lock = asyncio.Lock()

    async def __aenter__(self):
        async with self.lock:
            now = time.time()
            self.calls = [t for t in self.calls if now - t < self.period]
            if len(self.calls) >= self.max_calls:
                sleep_time = self.period - (now - self.calls[0])
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
            self.calls.append(time.time())

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

# Create a global rate limiter (30 calls per second as an example, adjust as needed)
rate_limiter = RateLimiter(max_calls=30, period=1)

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, MissingPermissions):
        await interaction.response.send_message(f"You don't have permission to use this command.", ephemeral=True)
        try:
            print(f"User {interaction.user.name.encode('utf-8', 'replace').decode('utf-8')} attempted to use command '{interaction.command.name}' without proper permissions in {interaction.guild.name.encode('utf-8', 'replace').decode('utf-8')}")
        except UnicodeEncodeError:
            print(f"User with unsupported characters attempted to use command '{interaction.command.name}' without proper permissions in a guild with unsupported characters")
    else:
        # Handle other types of errors here
        await interaction.response.send_message(f"An error occurred while processing the command.", ephemeral=True)
        print(f"An error occurred: {str(error)}")

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

async def delete_user_messages(channel: discord.TextChannel, username: str, progress_queue: asyncio.Queue) -> Tuple[int, List[str]]:
    purged_count = 0
    errors = []
    total_messages_checked = 0
    
    try:
        print(f"Checking channel: {channel.name.encode('utf-8', 'replace').decode('utf-8')} (ID: {channel.id})")
    except UnicodeEncodeError:
        print(f"Checking channel with unsupported characters (ID: {channel.id})")
    
    last_message_id = None
    while True:
        try:
            message_count = 0
            async for message in channel.history(limit=100, before=discord.Object(id=last_message_id) if last_message_id else None):
                message_count += 1
                total_messages_checked += 1
                last_message_id = message.id

                if message.author.name.lower() == username.lower():
                    try:
                        async with rate_limiter:
                            await message.delete()
                        purged_count += 1
                        await progress_queue.put(1)
                        
                        # Print progress every 10 deleted messages
                        if purged_count % 10 == 0:
                            try:
                                print(f"Progress update - Channel: {channel.name.encode('utf-8', 'replace').decode('utf-8')}, Messages checked: {total_messages_checked}, Messages deleted: {purged_count}")
                            except UnicodeEncodeError:
                                print(f"Progress update - Channel ID: {channel.id}, Messages checked: {total_messages_checked}, Messages deleted: {purged_count}")
                    
                    except discord.errors.NotFound:
                        pass
                    except discord.errors.Forbidden:
                        errors.append(f"No permission to delete messages in {channel.name}")
                        return purged_count, errors
                    except discord.errors.HTTPException as e:
                        if e.status == 429:  # Rate limit error
                            retry_after = e.retry_after
                            errors.append(f"Rate limited in {channel.name}. Waiting for {retry_after:.2f} seconds.")
                            print(f"Rate limit hit. Waiting for {retry_after:.2f} seconds before continuing.")
                            await asyncio.sleep(retry_after)
                            try:
                                async with rate_limiter:
                                    await message.delete()
                                purged_count += 1
                                await progress_queue.put(1)
                            except Exception as retry_e:
                                errors.append(f"Error after rate limit in {channel.name}: {str(retry_e)}")
                        else:
                            errors.append(f"HTTP error in {channel.name}: {str(e)}")
                    except Exception as e:
                        errors.append(f"Error in {channel.name}: {str(e)}")

                    await asyncio.sleep(random.uniform(0.5, 1.0))

            if message_count < 100:
                # We've reached the end of the messages
                break

            # Print progress after each batch of 100 messages
            try:
                print(f"Batch complete - Channel: {channel.name.encode('utf-8', 'replace').decode('utf-8')}, Total messages checked: {total_messages_checked}, Total messages deleted: {purged_count}")
            except UnicodeEncodeError:
                print(f"Batch complete - Channel ID: {channel.id}, Total messages checked: {total_messages_checked}, Total messages deleted: {purged_count}")

            await asyncio.sleep(random.uniform(1, 2))

        except discord.errors.Forbidden:
            errors.append(f"No permission to access messages in {channel.name}")
            break
        except discord.errors.HTTPException as e:
            if e.status == 429:  # Rate limit error
                retry_after = e.retry_after
                errors.append(f"Rate limited while fetching messages in {channel.name}. Waiting for {retry_after:.2f} seconds.")
                print(f"Rate limit hit while fetching messages. Waiting for {retry_after:.2f} seconds before continuing.")
                await asyncio.sleep(retry_after)
            else:
                errors.append(f"HTTP error while fetching messages in {channel.name}: {str(e)}")
                break
        except Exception as e:
            errors.append(f"Unexpected error in {channel.name}: {str(e)}")
            break

    try:
        print(f"Channel complete - {channel.name.encode('utf-8', 'replace').decode('utf-8')} (ID: {channel.id}), Total messages checked: {total_messages_checked}, Total messages deleted: {purged_count}")
    except UnicodeEncodeError:
        print(f"Channel complete - Channel ID: {channel.id}, Total messages checked: {total_messages_checked}, Total messages deleted: {purged_count}")

    return purged_count, errors

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
                        try:
                            async for message in channel.history(limit=None):
                                message_time = message.created_at.replace(tzinfo=pytz.utc)
                                if utc_now - message_time > delete_after:
                                    try:
                                        await message.delete()
                                        try:
                                            print(f'Deleted message in {guild.name.encode("utf-8", "replace").decode("utf-8")} - {channel.name.encode("utf-8", "replace").decode("utf-8")}...')
                                        except UnicodeEncodeError:
                                            print(f'Deleted message in a guild/channel with unsupported characters...')
                                    except discord.errors.NotFound:
                                        try:
                                            print(f'Message already deleted in {guild.name.encode("utf-8", "replace").decode("utf-8")} - {channel.name.encode("utf-8", "replace").decode("utf-8")}...')
                                        except UnicodeEncodeError:
                                            print(f'Message already deleted in a guild/channel with unsupported characters...')
                                    except discord.errors.Forbidden:
                                        try:
                                            print(f'Forbidden to delete message in {guild.name.encode("utf-8", "replace").decode("utf-8")} - {channel.name.encode("utf-8", "replace").decode("utf-8")}')
                                        except UnicodeEncodeError:
                                            print(f'Forbidden to delete message in a guild/channel with unsupported characters...')
                                        break  # Stop processing this channel
                                    except discord.errors.HTTPException as e:
                                        if e.status == 429:  # Rate limit error
                                            retry_after = e.retry_after
                                            try:
                                                print(f'Rate limited in {guild.name.encode("utf-8", "replace").decode("utf-8")} - {channel.name.encode("utf-8", "replace").decode("utf-8")}. Waiting for {retry_after} seconds.')
                                            except UnicodeEncodeError:
                                                print(f'Rate limited in a guild/channel with unsupported characters. Waiting for {retry_after} seconds.')
                                            await asyncio.sleep(retry_after)
                                        elif e.status == 503:  # Service Unavailable error
                                            try:
                                                print(f'Discord service unavailable in {guild.name.encode("utf-8", "replace").decode("utf-8")} - {channel.name.encode("utf-8", "replace").decode("utf-8")}. Waiting for 60 seconds before retry.')
                                            except UnicodeEncodeError:
                                                print(f'Discord service unavailable in a guild/channel with unsupported characters. Waiting for 60 seconds before retry.')
                                            await asyncio.sleep(60)  # Wait for 60 seconds before retrying
                                            break  # Stop processing this channel and move to the next
                                        else:
                                            try:
                                                print(f'HTTP error in {guild.name.encode("utf-8", "replace").decode("utf-8")} - {channel.name.encode("utf-8", "replace").decode("utf-8")}: {e}')
                                            except UnicodeEncodeError:
                                                print(f'HTTP error in a guild/channel with unsupported characters: {e}')
                                            await asyncio.sleep(5)  # Wait for 5 seconds before continuing
                                    except Exception as e:
                                        try:
                                            print(f'Error deleting message in {guild.name.encode("utf-8", "replace").decode("utf-8")} - {channel.name.encode("utf-8", "replace").decode("utf-8")}: {e}')
                                        except UnicodeEncodeError:
                                            print(f'Error deleting message in a guild/channel with unsupported characters: {e}')
                                        await asyncio.sleep(5)  # Wait for 5 seconds before continuing
                                    
                                    await asyncio.sleep(1)  # To avoid hitting rate limits
                        except discord.errors.HTTPException as e:
                            if e.status == 429:  # Rate limit error
                                retry_after = e.retry_after
                                try:
                                    print(f'Rate limited while fetching history in {guild.name.encode("utf-8", "replace").decode("utf-8")} - {channel.name.encode("utf-8", "replace").decode("utf-8")}. Waiting for {retry_after} seconds.')
                                except UnicodeEncodeError:
                                    print(f'Rate limited while fetching history in a guild/channel with unsupported characters. Waiting for {retry_after} seconds.')
                                await asyncio.sleep(retry_after)
                            elif e.status == 503:
                                try:
                                    print(f'Discord service unavailable while fetching history in {guild.name.encode("utf-8", "replace").decode("utf-8")} - {channel.name.encode("utf-8", "replace").decode("utf-8")}. Waiting for 60 seconds before moving to next channel.')
                                except UnicodeEncodeError:
                                    print(f'Discord service unavailable while fetching history in a guild/channel with unsupported characters. Waiting for 60 seconds before moving to next channel.')
                                await asyncio.sleep(60)  # Wait for 60 seconds before moving to the next channel
                            else:
                                try:
                                    print(f'HTTP error while fetching history in {guild.name.encode("utf-8", "replace").decode("utf-8")} - {channel.name.encode("utf-8", "replace").decode("utf-8")}: {e}')
                                except UnicodeEncodeError:
                                    print(f'HTTP error while fetching history in a guild/channel with unsupported characters: {e}')
                        except Exception as e:
                            try:
                                print(f'Error fetching history in {guild.name.encode("utf-8", "replace").decode("utf-8")} - {channel.name.encode("utf-8", "replace").decode("utf-8")}: {e}')
                            except UnicodeEncodeError:
                                print(f'Error fetching history in a guild/channel with unsupported characters: {e}')
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
@app_commands.checks.has_permissions(manage_channels=True)
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
@app_commands.checks.has_permissions(moderate_members=True)
async def purge_user(interaction: discord.Interaction, username: str):
    await interaction.response.defer(ephemeral=True)
    
    progress_queue = asyncio.Queue()
    total_purged = 0
    all_errors = []

    async def update_progress():
        nonlocal total_purged
        last_update = 0
        while True:
            count = await progress_queue.get()
            total_purged += count
            if total_purged - last_update >= 100:  # Update every 100 messages
                try:
                    await interaction.followup.send(f"Purging in progress... {total_purged} messages deleted so far.", ephemeral=True)
                    last_update = total_purged
                except discord.errors.HTTPException as e:
                    print(f"Failed to send progress update: {e}")
            progress_queue.task_done()

    progress_task = asyncio.create_task(update_progress())

    tasks = []
    for channel in interaction.guild.text_channels:
        if channel.permissions_for(interaction.guild.me).manage_messages:
            task = asyncio.create_task(delete_user_messages(channel, username, progress_queue))
            tasks.append(task)

    try:
        print(f"Starting purge operation for user: {username.encode('utf-8', 'replace').decode('utf-8')}")
    except UnicodeEncodeError:
        print(f"Starting purge operation for a user with unsupported characters")
    
    print(f"Number of channels to check: {len(tasks)}")

    results = await asyncio.gather(*tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, Exception):
            all_errors.append(str(result))
        else:
            count, errors = result
            total_purged += count
            all_errors.extend(errors)

    progress_task.cancel()
    try:
        await progress_task
    except asyncio.CancelledError:
        pass

    print(f"Purge operation complete. Total messages purged: {total_purged}")

    try:
        if total_purged > 0:
            await interaction.followup.send(f"Purged {total_purged} messages from user '{username}'.", ephemeral=True)
        else:
            await interaction.followup.send(f"No messages found from user '{username}' to purge.", ephemeral=True)
    except discord.errors.HTTPException as e:
        print(f"Failed to send final update: {e}")

    if all_errors:
        error_message = "\n".join(all_errors[:10])  # Limit to first 10 errors
        if len(all_errors) > 10:
            error_message += f"\n... and {len(all_errors) - 10} more errors."
        try:
            await interaction.followup.send(f"Encountered some errors during purge:\n{error_message}", ephemeral=True)
        except discord.errors.HTTPException as e:
            print(f"Failed to send error message: {e}")

    try:
        print(f"Purged {total_purged} messages from user '{username.encode('utf-8', 'replace').decode('utf-8')}' in {interaction.guild.name.encode('utf-8', 'replace').decode('utf-8')}")
    except UnicodeEncodeError:
        print(f"Purged {total_purged} messages from a user with unsupported characters in a guild with unsupported characters")

@bot.tree.command(name="purge_channel", description="Purge all messages from a specific channel")
@app_commands.describe(channel="The channel to purge messages from")
@app_commands.checks.has_permissions(moderate_members=True)
async def purge_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    
    try:
        print(f"Starting purge operation for channel: {channel.name.encode('utf-8', 'replace').decode('utf-8')} in {interaction.guild.name.encode('utf-8', 'replace').decode('utf-8')}")
    except UnicodeEncodeError:
        print(f"Starting purge operation for a channel in a guild with unsupported characters")

    purged_count = 0
    total_messages_checked = 0
    last_message_id = None
    rate_limit_delay = 0.5  # Start with a 0.5 second delay
    batch_count = 0

    while True:
        batch_count += 1
        try:
            print(f"Fetching batch #{batch_count} of messages...")
            messages = []
            async for message in channel.history(limit=100, before=discord.Object(id=last_message_id) if last_message_id else None):
                messages.append(message)
                # Add a small delay between each message fetch to avoid rate limiting
                await asyncio.sleep(0.05)

            if not messages:
                print("No more messages to process. Purge operation complete.")
                break

            last_message_id = messages[-1].id
            total_messages_checked += len(messages)

            print(f"Processing batch #{batch_count} - Messages in batch: {len(messages)}, Total messages checked: {total_messages_checked}")

            for index, message in enumerate(messages, 1):
                while True:
                    try:
                        await message.delete()
                        purged_count += 1
                        if purged_count % 10 == 0:  # Log every 10 deletions
                            try:
                                print(f"Progress update - Batch: {batch_count}, Messages checked: {total_messages_checked}, Messages deleted: {purged_count}, Channel: {channel.name.encode('utf-8', 'replace').decode('utf-8')}")
                            except UnicodeEncodeError:
                                print(f"Progress update - Batch: {batch_count}, Messages checked: {total_messages_checked}, Messages deleted: {purged_count}, Channel: [Encoding Error]")
                        
                        # Gradually decrease the delay if successful
                        rate_limit_delay = max(0.5, rate_limit_delay * 0.95)
                        
                        await asyncio.sleep(rate_limit_delay)
                        break  # Break the inner loop if successful
                    except discord.errors.NotFound:
                        try:
                            print(f"Message already deleted. Batch: {batch_count}, Message: {index}/{len(messages)}, Channel: {channel.name.encode('utf-8', 'replace').decode('utf-8')}")
                        except UnicodeEncodeError:
                            print(f"Message already deleted. Batch: {batch_count}, Message: {index}/{len(messages)}, Channel: [Encoding Error]")
                        break  # Break the inner loop if message not found
                    except discord.errors.Forbidden:
                        try:
                            print(f"No permission to delete message. Batch: {batch_count}, Message: {index}/{len(messages)}, Channel: {channel.name.encode('utf-8', 'replace').decode('utf-8')}")
                        except UnicodeEncodeError:
                            print(f"No permission to delete message. Batch: {batch_count}, Message: {index}/{len(messages)}, Channel: [Encoding Error]")
                        await interaction.followup.send(f"I don't have permission to delete messages in {channel.name}.", ephemeral=True)
                        return
                    except discord.errors.HTTPException as e:
                        if e.status == 429:  # Rate limit error
                            retry_after = e.retry_after
                            try:
                                print(f"Rate limited. Batch: {batch_count}, Message: {index}/{len(messages)}, Channel: {channel.name.encode('utf-8', 'replace').decode('utf-8')}. Waiting for {retry_after:.2f} seconds.")
                            except UnicodeEncodeError:
                                print(f"Rate limited. Batch: {batch_count}, Message: {index}/{len(messages)}, Channel: [Encoding Error]. Waiting for {retry_after:.2f} seconds.")
                            await asyncio.sleep(retry_after)
                            rate_limit_delay = min(5, rate_limit_delay * 1.5)  # Increase delay, max 5 seconds
                        elif e.status == 503:
                            try:
                                print(f"Discord service unavailable. Batch: {batch_count}, Message: {index}/{len(messages)}, Channel: {channel.name.encode('utf-8', 'replace').decode('utf-8')}. Retrying in 60 seconds.")
                            except UnicodeEncodeError:
                                print(f"Discord service unavailable. Batch: {batch_count}, Message: {index}/{len(messages)}, Channel: [Encoding Error]. Retrying in 60 seconds.")
                            await asyncio.sleep(60)
                        elif e.code == 50027:  # Invalid Webhook Token
                            try:
                                print(f"Invalid Webhook Token error. Batch: {batch_count}, Message: {index}/{len(messages)}, Channel: {channel.name.encode('utf-8', 'replace').decode('utf-8')}. Skipping this message.")
                            except UnicodeEncodeError:
                                print(f"Invalid Webhook Token error. Batch: {batch_count}, Message: {index}/{len(messages)}, Channel: [Encoding Error]. Skipping this message.")
                            await interaction.followup.send("Encountered a message with an invalid webhook token. Skipping this message.", ephemeral=True)
                            break  # Move to the next message
                        else:
                            try:
                                print(f"HTTP error. Batch: {batch_count}, Message: {index}/{len(messages)}, Channel: {channel.name.encode('utf-8', 'replace').decode('utf-8')}: {str(e)}")
                            except UnicodeEncodeError:
                                print(f"HTTP error. Batch: {batch_count}, Message: {index}/{len(messages)}, Channel: [Encoding Error]: {str(e)}")
                            await asyncio.sleep(5)
                    except Exception as e:
                        try:
                            print(f"Unexpected error. Batch: {batch_count}, Message: {index}/{len(messages)}, Channel: {channel.name.encode('utf-8', 'replace').decode('utf-8')}: {str(e)}")
                        except UnicodeEncodeError:
                            print(f"Unexpected error. Batch: {batch_count}, Message: {index}/{len(messages)}, Channel: [Encoding Error]: {str(e)}")
                        await asyncio.sleep(5)

                if purged_count % 100 == 0:
                    await interaction.followup.send(f"Purged {purged_count} messages so far...", ephemeral=True)

            try:
                print(f"Batch #{batch_count} complete - Messages checked: {total_messages_checked}, Messages deleted: {purged_count}, Channel: {channel.name.encode('utf-8', 'replace').decode('utf-8')}")
            except UnicodeEncodeError:
                print(f"Batch #{batch_count} complete - Messages checked: {total_messages_checked}, Messages deleted: {purged_count}, Channel: [Encoding Error]")
            # Add a longer delay between batches
            await asyncio.sleep(2)

        except discord.errors.Forbidden:
            try:
                print(f"No permission to access messages in the channel: {channel.name.encode('utf-8', 'replace').decode('utf-8')}. Purge operation stopped.")
            except UnicodeEncodeError:
                print(f"No permission to access messages in the channel: [Encoding Error]. Purge operation stopped.")
            await interaction.followup.send(f"I don't have permission to access messages in {channel.name}.", ephemeral=True)
            return
        except discord.errors.HTTPException as e:
            if e.status == 429:  # Rate limit error
                retry_after = e.retry_after
                try:
                    print(f"Rate limited while fetching messages. Batch: {batch_count}, Channel: {channel.name.encode('utf-8', 'replace').decode('utf-8')}. Waiting for {retry_after:.2f} seconds.")
                except UnicodeEncodeError:
                    print(f"Rate limited while fetching messages. Batch: {batch_count}, Channel: [Encoding Error]. Waiting for {retry_after:.2f} seconds.")
                await asyncio.sleep(retry_after)
                continue  # Retry this batch
            elif e.code == 50027:  # Invalid Webhook Token
                try:
                    print(f"Invalid Webhook Token error while fetching messages. Batch: {batch_count}, Channel: {channel.name.encode('utf-8', 'replace').decode('utf-8')}. Skipping this batch.")
                except UnicodeEncodeError:
                    print(f"Invalid Webhook Token error while fetching messages. Batch: {batch_count}, Channel: [Encoding Error]. Skipping this batch.")
                await interaction.followup.send("Encountered messages with invalid webhook tokens. Skipping this batch.", ephemeral=True)
                continue  # Move to the next batch
            else:
                try:
                    print(f"Error accessing messages. Batch: {batch_count}, Channel: {channel.name.encode('utf-8', 'replace').decode('utf-8')}: {str(e)}")
                except UnicodeEncodeError:
                    print(f"Error accessing messages. Batch: {batch_count}, Channel: [Encoding Error]: {str(e)}")
                await asyncio.sleep(5)
                continue  # Retry this batch
        except Exception as e:
            try:
                print(f"Unexpected error while fetching messages. Batch: {batch_count}, Channel: {channel.name.encode('utf-8', 'replace').decode('utf-8')}: {str(e)}")
            except UnicodeEncodeError:
                print(f"Unexpected error while fetching messages. Batch: {batch_count}, Channel: [Encoding Error]: {str(e)}")
            await asyncio.sleep(5)
            continue  # Retry this batch

    await interaction.followup.send(f"Purge operation complete. Purged {purged_count} messages from channel {channel.name}.", ephemeral=True)
    try:
        print(f"Purge operation complete. Channel: {channel.name.encode('utf-8', 'replace').decode('utf-8')}, Total messages checked: {total_messages_checked}, Total messages purged: {purged_count}")
    except UnicodeEncodeError:
        print(f"Purge operation complete. Channel: [Encoding Error], Total messages checked: {total_messages_checked}, Total messages purged: {purged_count}")

@bot.tree.command(name="show_logs", description="Show recent logs for the Claudelete bot")
@app_commands.checks.has_permissions(moderate_members=True)
async def show_logs(interaction: discord.Interaction):
    try:
        # Run the journalctl command and capture its output
        result = subprocess.run(
            ["journalctl", "--user-unit", "claudelete-bot", "-n", "20", "--no-pager"],
            capture_output=True,
            text=True,
            check=True
        )
        
        # Format the output as a code block
        #formatted_logs = f"```\n{result.stdout}\n```"
        formatted_logs = result.stdout
        
        # If the logs are too long, split them into chunks
        if len(formatted_logs) > 2000:
            chunks = [formatted_logs[i:i+1990] for i in range(0, len(formatted_logs), 1990)]
            await interaction.response.send_message("Logs are too long. Sending in multiple messages:", ephemeral=True)
            for chunk in chunks:
                await interaction.followup.send(f"```\n{chunk}\n```", ephemeral=True)
        else:
            await interaction.response.send_message(f"```\n{formatted_logs}\n```", ephemeral=True)
    
    except subprocess.CalledProcessError as e:
        error_message = f"An error occurred while fetching logs: {e}"
        await interaction.response.send_message(error_message, ephemeral=True)
    except Exception as e:
        error_message = f"An unexpected error occurred: {e}"
        await interaction.response.send_message(error_message, ephemeral=True)

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
