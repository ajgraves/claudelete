import discord
from discord import app_commands, HTTPException, NotFound, Forbidden
from discord.app_commands import MissingPermissions
from discord.errors import RateLimited, HTTPException, Forbidden, NotFound
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
from collections import defaultdict
import traceback
import importlib
import cdconfig

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
channel_rate_limits = defaultdict(lambda: {"reset_after": 0, "remaining": 5})

# Global set to keep track of channels currently being processed
channels_in_progress = set()

# Global dict to keep track of long-running tasks
channel_tasks = {}

# Configurable maximum number of concurrent tasks
MAX_CONCURRENT_TASKS = 25

# Semaphore to limit concurrent tasks
task_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

# Global progress queue
progress_queue = asyncio.Queue()

# How often the bot should check channels.
TASK_INTERVAL_SECONDS = getattr(cdconfig, 'TASK_INTERVAL_SECONDS', 60)  # Default to 60 seconds, but you can change this value in cdconfig.py

# Global variable to store the last config reload time
last_config_reload_time = 0 # Was 0, BUT it should be the current time, since we've loaded the configuration on program load
CONFIG_RELOAD_INTERVAL = getattr(cdconfig, 'CONFIG_RELOAD_INTERVAL', 300)  # Reload config every 5 minutes (adjust as needed in cdconfig.py)

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
    if isinstance(error, app_commands.errors.MissingPermissions):
        error_message = f"You don't have permission to use this command."
    else:
        error_message = f"An error occurred while processing the command: {str(error)}"
    
    print(f"Command error: {error_message}")
    
    try:
        if interaction.response.is_done():
            await interaction.followup.send(error_message, ephemeral=True)
        else:
            await interaction.response.send_message(error_message, ephemeral=True)
    except discord.errors.HTTPException:
        # If we can't send a message, log it
        print(f"Failed to send error message to user for command: {interaction.command.name}")

# Database configuration is now in config.py

# Function to reload the configuration
def reload_config():
    global last_config_reload_time, TASK_INTERVAL_SECONDS
    current_time = time.time()
    if current_time - last_config_reload_time > CONFIG_RELOAD_INTERVAL:
        importlib.reload(cdconfig)
        TASK_INTERVAL_SECONDS = getattr(cdconfig, 'TASK_INTERVAL_SECONDS', 60)
        CONFIG_RELOAD_INTERVAL = getattr(cdconfig, 'CONFIG_RELOAD_INTERVAL', 300)
        last_config_reload_time = current_time
        print(f"Configuration reloaded. TASK_INTERVAL_SECONDS is now {TASK_INTERVAL_SECONDS}, CONFIG_RELOAD_INTERVAL is now {CONFIG_RELOAD_INTERVAL}")

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
    reload_config()
    bot.loop.create_task(continuous_delete_old_messages())

async def process_channel(guild, channel, delete_after):
    delete_count = 0
    messages_checked = 0
    utc_now = datetime.now(pytz.utc)
    
    '''
    try:
        print(f"Starting to process channel: {channel.name} (ID: {channel.id}) in guild: {guild.name} (ID: {guild.id})")
    except UnicodeEncodeError:
        print(f"Starting to process channel ID: {channel.id} in guild ID: {guild.id}")
    '''

    try:
        async for message in handle_rate_limits(channel.history(limit=None)):
            messages_checked += 1
            message_time = message.created_at.replace(tzinfo=pytz.utc)
            if utc_now - message_time > delete_after:
                try:
                    await message.delete()
                    delete_count += 1
                    await progress_queue.put(1)
                    
                    # Add a random delay between 0.5 and 2 seconds
                    await asyncio.sleep(random.uniform(0.5, 2))
                    
                except NotFound:
                    print(f"Message not found in channel {channel.id}, guild {guild.id}")
                except Forbidden:
                    print(f"Forbidden to delete message in channel {channel.id}, guild {guild.id}")
                    return delete_count, messages_checked
                except HTTPException as e:
                    if e.status == 429:  # Rate limit error
                        retry_after = e.retry_after
                        print(f"Rate limited when deleting message in channel {channel.id}, guild {guild.id}. Waiting for {retry_after} seconds.")
                        await asyncio.sleep(retry_after)
                    else:
                        print(f"HTTP error when deleting message in channel {channel.id}, guild {guild.id}: {e}")
                        await asyncio.sleep(5)
                except Exception as e:
                    print(f"Error deleting message in channel {channel.id}, guild {guild.id}: {e}")
                    await asyncio.sleep(5)
                
            if delete_count % 10 == 0 and delete_count > 0:
                try:
                    print(f"Progress update - Channel: {channel.name} (ID: {channel.id}), Guild: {guild.name} (ID: {guild.id}), Messages checked: {messages_checked}, Messages deleted: {delete_count}")
                except UnicodeEncodeError:
                    print(f"Progress update - Channel ID: {channel.id}, Guild ID: {guild.id}, Messages checked: {messages_checked}, Messages deleted: {delete_count}")
    
    except Forbidden:
        print(f"No permission to access channel {channel.id} in guild {guild.id}")
    except Exception as e:
        print(f"Error processing channel {channel.id} in guild {guild.id}: {e}")

    '''
    try:
        print(f"Finished processing channel: {channel.name} (ID: {channel.id}) in guild: {guild.name} (ID: {guild.id}). Messages checked: {messages_checked}, Messages deleted: {delete_count}")
    except UnicodeEncodeError:
        print(f"Finished processing channel ID: {channel.id} in guild ID: {guild.id}. Messages checked: {messages_checked}, Messages deleted: {delete_count}")
    '''

    channels_in_progress.remove(channel.id)
    del channel_tasks[channel.id]
    return delete_count, messages_checked

async def handle_rate_limits(history_iterator):
    while True:
        try:
            yield await history_iterator.__anext__()
        except StopAsyncIteration:
            break
        except HTTPException as e:
            if e.status == 429:  # Rate limit error
                retry_after = e.retry_after
                print(f"Rate limited when fetching message history. Waiting for {retry_after} seconds.")
                await asyncio.sleep(retry_after)
            else:
                print(f"HTTP error when fetching message history: {e}")
                await asyncio.sleep(5)
        except Exception as e:
            print(f"Unexpected error when fetching message history: {e}")
            await asyncio.sleep(5)

async def process_channel_wrapper(guild, channel, delete_after):
    async with task_semaphore:
        channels_in_progress.add(channel.id)
        '''
        try:
            print(f"Added channel {channel.id} to channels_in_progress.") # Current set: {channels_in_progress}")
        except UnicodeEncodeError:
            print(f"Added channel {channel.id} to channels_in_progress. Unable to print full set due to encoding error.")
        '''
        return await process_channel(guild, channel, delete_after)

async def update_progress():
    total_deleted = 0
    try:
        while True:
            count = await progress_queue.get()
            total_deleted += count
            if total_deleted % 100 == 0:
                print(f"Total messages deleted so far: {total_deleted}")
            progress_queue.task_done()
    except asyncio.CancelledError:
        print("Progress update task cancelled")

async def delete_old_messages_task():
    print("Starting delete_old_messages_task")
    connection = create_connection()
    if connection:
        try:
            cursor = connection.cursor(MySQLdb.cursors.DictCursor)
            cursor.execute("SELECT * FROM channel_config")
            configs = cursor.fetchall()
            
            new_tasks = []

            for config in configs:
                channel_id = config['channel_id']
                
                # Skip this channel if it's already being processed
                if channel_id in channels_in_progress:
                    print(f"Channel ID: {channel_id} is still being processed from a previous run. Skipping.")
                    continue
                
                guild = bot.get_guild(config['guild_id'])
                if guild is None:
                    print(f"Guild does not exist or bot is not in guild (ID: {config['guild_id']})")
                    continue

                if not guild.me.guild_permissions.view_channel:
                    print(f"Bot doesn't have permission to view channels in guild (ID: {config['guild_id']})")
                    continue

                channel = guild.get_channel(channel_id)
                if channel is None:
                    print(f"Channel does not exist in guild (Guild ID: {config['guild_id']}, Channel ID: {channel_id})")
                    continue

                if not channel.permissions_for(guild.me).read_messages:
                    print(f"Bot doesn't have permission to read messages in channel (Guild ID: {config['guild_id']}, Channel ID: {channel_id})")
                    continue

                if not channel.permissions_for(guild.me).manage_messages:
                    print(f"Bot doesn't have permission to delete messages in channel (Guild ID: {config['guild_id']}, Channel ID: {channel_id})")
                    continue

                delete_after = timedelta(minutes=config['delete_after'])
                
                if channel.id not in channel_tasks:
                    task = asyncio.create_task(process_channel_wrapper(guild, channel, delete_after))
                    channel_tasks[channel.id] = task
                    new_tasks.append(task)
                    '''
                    try:
                        print(f"Created task for channel {channel.name} (ID: {channel.id})")
                    except UnicodeEncodeError:
                        print(f"Created task for channel ID: {channel.id}")
                    '''

            # Wait for new tasks to complete or for TASK_INTERVAL_SECONDS seconds, whichever comes first
            if new_tasks:
                try:
                    done, pending = await asyncio.wait(new_tasks, timeout=TASK_INTERVAL_SECONDS, return_when=asyncio.ALL_COMPLETED)
                    for task in done:
                        try:
                            result = task.result()
                            if isinstance(result, tuple):
                                deleted, checked = result
                                print(f"Task completed. Messages deleted: {deleted}, Messages checked: {checked}")
                        except Exception as e:
                            print(f"Task error: {e}")
                            print(f"Traceback: {traceback.format_exc()}")
                except asyncio.TimeoutError:
                    print(f"Some tasks are still running after {TASK_INTERVAL_SECONDS} seconds. They will continue in the background.")

            print(f"Delete old messages task iteration complete. Channels still being processed: {len(channels_in_progress)}")

        except Error as e:
            print(f"Error reading from database: {e}")
        finally:
            cursor.close()
            connection.close()
    print("Finished delete_old_messages_task iteration")

async def continuous_delete_old_messages():
    progress_task = asyncio.create_task(update_progress())
    try:
        while True:
            reload_config()  # This will check and reload the config if necessary
            start_time = asyncio.get_event_loop().time()
            print(f"Starting delete_old_messages task...")
            
            await delete_old_messages_task()
            
            end_time = asyncio.get_event_loop().time()
            elapsed_time = end_time - start_time
            
            # If the task completed in less than the interval, wait for the remaining time
            if elapsed_time < TASK_INTERVAL_SECONDS:
                wait_time = TASK_INTERVAL_SECONDS - elapsed_time
                print(f"Waiting for {wait_time:.2f} seconds before next iteration")
                await asyncio.sleep(wait_time)

            print("delete_old_messages task iteration completed.")
    finally:
        progress_task.cancel()
        await progress_task

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

    await interaction.followup.send(f"Starting purge operation for user: {username}. **NOTE:** Due to Discord limitations, you may stop getting progress updates about this process. Rest assured, the process will continue running until it successfully completes.", ephemeral=True)
    
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

    await interaction.followup.send(f"Starting purge operation for channel: {channel}. **NOTE:** Due to Discord limitations, you may stop getting progress updates about this process. Rest assured, the process will continue running until it successfully completes.", ephemeral=True)
    
    try:
        print(f"Starting purge operation for channel: {channel.name.encode('utf-8', 'replace').decode('utf-8')} in {interaction.guild.name.encode('utf-8', 'replace').decode('utf-8')}")
    except UnicodeEncodeError:
        print(f"Starting purge operation for channel (ID: {channel.id}) in guild (ID: {interaction.guild.id})")

    purged_count = 0
    total_messages_checked = 0
    last_message_id = None
    batch_count = 0
    history_rate_limit = {"reset_after": 0, "remaining": 100}  # Adjust these values as needed

    async def delete_with_rate_limit(message):
        nonlocal purged_count
        rate_limit = channel_rate_limits[channel.id]
        
        while True:
            now = time.time()
            if now > rate_limit["reset_after"]:
                rate_limit["remaining"] = 5  # Reset to default limit
                rate_limit["reset_after"] = now + 5  # Reset after 5 seconds
            
            if rate_limit["remaining"] > 0:
                try:
                    await message.delete()
                    rate_limit["remaining"] -= 1
                    purged_count += 1
                    return True
                except discord.errors.NotFound:
                    return True  # Message already deleted
                except discord.errors.Forbidden:
                    print(f"No permission to delete message in channel: {channel.id}")
                    return False
                except discord.errors.HTTPException as e:
                    if e.status == 429:  # Rate limit error
                        retry_after = e.retry_after
                        print(f"Rate limited. Waiting for {retry_after:.2f} seconds.")
                        rate_limit["reset_after"] = now + retry_after
                        rate_limit["remaining"] = 0
                        await asyncio.sleep(retry_after)
                    elif e.code == 50027:  # Invalid Webhook Token
                        print(f"Invalid Webhook Token error. Skipping message.")
                        return False
                    else:
                        print(f"HTTP error while deleting message: {e}")
                        await asyncio.sleep(1)
                except Exception as e:
                    print(f"Unexpected error while deleting message: {e}")
                    await asyncio.sleep(1)
            else:
                wait_time = rate_limit["reset_after"] - now
                if wait_time > 0:
                    await asyncio.sleep(wait_time)

    try:
        while True:
            batch_count += 1
            print(f"Fetching batch #{batch_count} of messages...")
            messages = []
            
            # Rate limiting for message history retrieval
            now = time.time()
            if now > history_rate_limit["reset_after"]:
                history_rate_limit["remaining"] = 100  # Reset to default limit
                history_rate_limit["reset_after"] = now + 60  # Reset after 60 seconds
            
            if history_rate_limit["remaining"] <= 0:
                wait_time = history_rate_limit["reset_after"] - now
                if wait_time > 0:
                    print(f"Rate limit reached for message history. Waiting for {wait_time:.2f} seconds.")
                    await asyncio.sleep(wait_time)
            
            try:
                async for message in channel.history(limit=100, before=discord.Object(id=last_message_id) if last_message_id else None):
                    messages.append(message)
                    history_rate_limit["remaining"] -= 1
                    if history_rate_limit["remaining"] <= 0:
                        break
                    await asyncio.sleep(0.05)  # Small delay to avoid hitting rate limits too quickly
            except discord.errors.HTTPException as e:
                if e.status == 429:  # Rate limit error
                    retry_after = e.retry_after
                    print(f"Rate limited while fetching messages. Waiting for {retry_after:.2f} seconds.")
                    await asyncio.sleep(retry_after)
                    continue  # Retry this batch
                else:
                    raise  # Re-raise the exception if it's not a rate limit error

            if not messages:
                print("No more messages to process. Purge operation complete.")
                break

            last_message_id = messages[-1].id
            total_messages_checked += len(messages)

            print(f"Processing batch #{batch_count} - Messages in batch: {len(messages)}, Total messages checked: {total_messages_checked}")

            for index, message in enumerate(messages, 1):
                success = await delete_with_rate_limit(message)
                if not success:
                    print(f"Failed to delete message. Batch: {batch_count}, Message: {index}/{len(messages)}")
                
                if purged_count % 10 == 0:  # Log every 10 deletions
                    try:
                        print(f"Progress update - Batch: {batch_count}, Messages checked: {total_messages_checked}, Messages deleted: {purged_count}, Channel: {channel.name.encode('utf-8', 'replace').decode('utf-8')}")
                    except UnicodeEncodeError:
                        print(f"Progress update - Batch: {batch_count}, Messages checked: {total_messages_checked}, Messages deleted: {purged_count}, Channel ID: {channel.id}, Guild ID: {interaction.guild.id}")

                if purged_count % 100 == 0:
                    await interaction.followup.send(f"Purged {purged_count} messages so far...", ephemeral=True)

            try:
                print(f"Batch #{batch_count} complete - Messages checked: {total_messages_checked}, Messages deleted: {purged_count}, Channel: {channel.name.encode('utf-8', 'replace').decode('utf-8')}")
            except UnicodeEncodeError:
                print(f"Batch #{batch_count} complete - Messages checked: {total_messages_checked}, Messages deleted: {purged_count}, Channel ID: {channel.id}, Guild ID: {interaction.guild.id}")
            
            # Add a longer delay between batches
            await asyncio.sleep(2)

        await interaction.followup.send(f"Purge operation complete. Purged {purged_count} messages from channel {channel.name}.", ephemeral=True)
        try:
            print(f"Purge operation complete. Channel: {channel.name.encode('utf-8', 'replace').decode('utf-8')}, Total messages checked: {total_messages_checked}, Total messages purged: {purged_count}")
        except UnicodeEncodeError:
            print(f"Purge operation complete. Channel ID: {channel.id}, Guild ID: {interaction.guild.id}, Total messages checked: {total_messages_checked}, Total messages purged: {purged_count}")

    except discord.errors.Forbidden:
        error_message = f"I don't have permission to access or delete messages in channel ID: {channel.id}."
        print(error_message)
        await interaction.followup.send(error_message, ephemeral=True)
    except Exception as e:
        error_message = f"An unexpected error occurred in channel ID: {channel.id}, Guild ID: {interaction.guild.id}: {str(e)}"
        print(error_message)
        await interaction.followup.send(error_message, ephemeral=True)

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

@bot.tree.command(name="lookup_guild", description="Look up a guild by its ID")
@app_commands.describe(guild_id="The ID of the guild to look up")
@app_commands.checks.has_permissions(moderate_members=True)
async def lookup_guild(interaction: discord.Interaction, guild_id: str):
    try:
        # Convert the input to an integer
        guild_id = int(guild_id)
        
        # Attempt to fetch the guild
        guild = bot.get_guild(guild_id)
        
        if guild:
            # Guild found, send the name
            await interaction.response.send_message(f"The guild with ID {guild_id} is named: {guild.name}", ephemeral=True)
        else:
            # Guild not found
            await interaction.response.send_message(f"No guild found with ID {guild_id}. The bot might not be a member of this guild.", ephemeral=True)
    
    except ValueError:
        # Invalid input (not a number)
        await interaction.response.send_message("Invalid input. Please provide a valid numeric guild ID.", ephemeral=True)
    except Exception as e:
        # Handle any other unexpected errors
        print(f"Error in lookup_guild command: {str(e)}")
        await interaction.response.send_message("An error occurred while processing your request.", ephemeral=True)

@lookup_guild.error
async def lookup_guild_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.errors.MissingPermissions):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
    else:
        await interaction.response.send_message(f"An error occurred: {str(error)}", ephemeral=True)

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
