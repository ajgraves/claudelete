import discord
from discord import app_commands, HTTPException, NotFound, Forbidden, CategoryChannel
from discord.app_commands import MissingPermissions
from discord.errors import RateLimited, HTTPException, Forbidden, NotFound
from discord.ext import commands, tasks
from discord.utils import snowflake_time
from datetime import datetime, timedelta
import pytz
import asyncio
from asyncio import TimeoutError
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
from typing import Union
# For debugging purposes, enable these lines
#import logging
#logging.basicConfig(level=logging.DEBUG)
#logger = logging.getLogger('discord')
#logger.setLevel(logging.DEBUG)
# End of debugging lines. Please comment and uncomment as needed.

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
channel_rate_limits = defaultdict(lambda: {"reset_after": 0, "remaining": 5})

# Global set to keep track of channels currently being processed
channels_in_progress = set()

# Global dict to keep track of long-running tasks
channel_tasks = {}

# Global progress queue
progress_queue = asyncio.Queue()

## Class definitions
class ConfigManager:
    def __init__(self):
        self.TASK_INTERVAL_SECONDS = getattr(cdconfig, 'TASK_INTERVAL_SECONDS', 60) # Default to 60 seconds, but you can change this value in cdconfig.py
        self.CONFIG_RELOAD_INTERVAL = getattr(cdconfig, 'CONFIG_RELOAD_INTERVAL', 300) # Reload config every 5 minutes (adjust as needed in cdconfig.py)
        self.MAX_CONCURRENT_TASKS = getattr(cdconfig, 'MAX_CONCURRENT_TASKS', 25) # Configurable maximum number of concurrent tasks
        self.PROCESS_CHANNEL_BATCH_SIZE = getattr(cdconfig, 'PROCESS_CHANNEL_BATCH_SIZE', 250) # Batch size for process_channel()
        self.DELETE_USER_MESSAGES_BATCH_SIZE = getattr(cdconfig, 'DELETE_USER_MESSAGES_BATCH_SIZE', 100) # Batch size for delete_user_messages()
        self.PURGE_CHANNEL_BATCH_SIZE = getattr(cdconfig, 'PURGE_CHANNEL_BATCH_SIZE', 100) # Batch size for purge_channel()
        self.PROCESS_CHANNEL_TIMEOUT = getattr(cdconfig, 'PROCESS_CHANNEL_TIMEOUT', 15) # PROCESS_CHANNEL_TIMEOUT tells Claudelete how long it should wait on a delete operation before it times out in process_channel
        self.CHANNEL_ACCESS_TIMEOUT = getattr(cdconfig, 'CHANNEL_ACCESS_TIMEOUT', 24*60) # CHANNEL_ACCESS_TIMEOUT will allow Claudelete to remove channels it hasn't had access to for the configured amount of time
        self.last_reload_time = time.time()

    def reload_config(self):
        """Reload configuration from cdconfig module"""
        importlib.reload(cdconfig)
        
        # Store old values for comparison
        old_values = self.get_current_values()
        
        # Update values
        self.TASK_INTERVAL_SECONDS = getattr(cdconfig, 'TASK_INTERVAL_SECONDS', 60)
        self.CONFIG_RELOAD_INTERVAL = getattr(cdconfig, 'CONFIG_RELOAD_INTERVAL', 300)
        self.MAX_CONCURRENT_TASKS = getattr(cdconfig, 'MAX_CONCURRENT_TASKS', 25)
        self.PROCESS_CHANNEL_BATCH_SIZE = getattr(cdconfig, 'PROCESS_CHANNEL_BATCH_SIZE', 250)
        self.DELETE_USER_MESSAGES_BATCH_SIZE = getattr(cdconfig, 'DELETE_USER_MESSAGES_BATCH_SIZE', 100)
        self.PURGE_CHANNEL_BATCH_SIZE = getattr(cdconfig, 'PURGE_CHANNEL_BATCH_SIZE', 100)
        self.PROCESS_CHANNEL_TIMEOUT = getattr(cdconfig, 'PROCESS_CHANNEL_TIMEOUT', 15)
        self.CHANNEL_ACCESS_TIMEOUT = getattr(cdconfig, 'CHANNEL_ACCESS_TIMEOUT', 24*60)
        
        # Get new values
        new_values = self.get_current_values()
        
        # Compare and log changes
        changes = self.compare_values(old_values, new_values)
        if changes:
            print("Configuration changes detected:")
            for var, (old, new) in changes.items():
                print(f"  {var}: {old} -> {new}")
        else:
            print("Configuration reloaded - no changes detected")
        
        self.last_reload_time = time.time()
        
        # If MAX_CONCURRENT_TASKS changed, update the semaphore
        if 'MAX_CONCURRENT_TASKS' in changes:
            task_semaphore.resize(self.MAX_CONCURRENT_TASKS)

    def get_current_values(self):
        """Return dictionary of current configuration values"""
        return {
            'TASK_INTERVAL_SECONDS': self.TASK_INTERVAL_SECONDS,
            'CONFIG_RELOAD_INTERVAL': self.CONFIG_RELOAD_INTERVAL,
            'MAX_CONCURRENT_TASKS': self.MAX_CONCURRENT_TASKS,
            'PROCESS_CHANNEL_BATCH_SIZE': self.PROCESS_CHANNEL_BATCH_SIZE,
            'DELETE_USER_MESSAGES_BATCH_SIZE': self.DELETE_USER_MESSAGES_BATCH_SIZE,
            'PURGE_CHANNEL_BATCH_SIZE': self.PURGE_CHANNEL_BATCH_SIZE,
            'PROCESS_CHANNEL_TIMEOUT': self.PROCESS_CHANNEL_TIMEOUT,
            'CHANNEL_ACCESS_TIMEOUT': self.CHANNEL_ACCESS_TIMEOUT
        }

    @staticmethod
    def compare_values(old_values, new_values):
        """Compare old and new values, return dictionary of changes"""
        changes = {}
        for key in old_values:
            if old_values[key] != new_values[key]:
                changes[key] = (old_values[key], new_values[key])
        return changes

# Create global config manager instance
botconfig = ConfigManager()

# Replace reload_config function
def reload_config():
    current_time = time.time()
    if current_time - botconfig.last_reload_time > botconfig.CONFIG_RELOAD_INTERVAL:
        botconfig.reload_config()


class ResizableSemaphore:
    def __init__(self, value):
        self._semaphore = asyncio.Semaphore(value)
        self._value = value

    async def acquire(self):
        return await self._semaphore.acquire()

    def release(self):
        return self._semaphore.release()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.release()

    def resize(self, new_value):
        if new_value > self._value:
            # If increasing, release additional permits
            for _ in range(new_value - self._value):
                self._semaphore.release()
        elif new_value < self._value:
            # If decreasing, acquire excess permits
            async def acquire_excess():
                for _ in range(self._value - new_value):
                    await self._semaphore.acquire()
            asyncio.create_task(acquire_excess())
        self._value = new_value

# Semaphore to limit concurrent tasks
#task_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
task_semaphore = ResizableSemaphore(botconfig.MAX_CONCURRENT_TASKS)

class AutoDeleteBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents)

    async def process_commands(self, message):
        return  # Do nothing, effectively ignoring text commands

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
                    guild_name VARCHAR(255),
                    channel_name VARCHAR(255),
                    last_updated DATETIME,
                    UNIQUE KEY guild_channel (guild_id, channel_id)
                )
            """)
            connection.commit()
        except Error as e:
            print(f"Error creating table: {e}")
        finally:
            cursor.close()
            connection.close()

def migrate_database():
    connection = create_connection()
    if connection:
        try:
            cursor = connection.cursor()
            # Check if new columns exist
            cursor.execute("SHOW COLUMNS FROM channel_config LIKE 'guild_name'")
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE channel_config ADD COLUMN guild_name VARCHAR(255)")
                cursor.execute("ALTER TABLE channel_config ADD COLUMN channel_name VARCHAR(255)")
                cursor.execute("ALTER TABLE channel_config ADD COLUMN last_updated DATETIME")
                connection.commit()
        except Error as e:
            print(f"Error migrating database: {e}")
        finally:
            cursor.close()
            connection.close()

def update_channel_info(connection, guild, channel):
    try:
        cursor = connection.cursor()
        cursor.execute("""
            UPDATE channel_config 
            SET guild_name = %s, channel_name = %s, last_updated = NOW() 
            WHERE guild_id = %s AND channel_id = %s
        """, (guild.name, channel.name, guild.id, channel.id))
        connection.commit()
    except Error as e:
        print(f"Error updating channel info: {e}")
    finally:
        cursor.close()

def cleanup_inaccessible_channels(connection):
    try:
        cursor = connection.cursor()
        threshold = datetime.now() - timedelta(minutes=botconfig.CHANNEL_ACCESS_TIMEOUT)
        cursor.execute("DELETE FROM channel_config WHERE last_updated < %s", (threshold,))
        if cursor.rowcount > 0:
            print(f"Removed {cursor.rowcount} channel(s) due to prolonged inaccessibility")
        connection.commit()
    except Error as e:
        print(f"Error cleaning up inaccessible channels: {e}")
    finally:
        cursor.close()

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
    
    async def process_messages_in_thread(thread, is_archived=False):
        nonlocal purged_count, total_messages_checked
        thread_last_message_id = None

        # If thread is archived, try to unarchive it first
        if is_archived:
            try:
                print(f"Attempting to unarchive thread {thread.id} before processing messages")
                await thread.edit(archived=False)
                # No sleep here to avoid potential auto-archiving
                print(f"Successfully unarchived thread {thread.id}")
            except Exception as unarchive_e:
                print(f"Error unarchiving thread {thread.id}: {str(unarchive_e)}")
                errors.append(f"Could not unarchive thread in {channel.name}: {str(unarchive_e)}")
                return  # Skip this thread if we can't unarchive it
        
        while True:
            try:
                message_count = 0
                async for message in thread.history(limit=botconfig.DELETE_USER_MESSAGES_BATCH_SIZE, 
                                                before=discord.Object(id=thread_last_message_id) if thread_last_message_id else None):
                    message_count += 1
                    total_messages_checked += 1
                    thread_last_message_id = message.id

                    if message.author.name.lower() == username.lower():
                        try:
                            async with rate_limiter:
                                await message.delete()
                            purged_count += 1
                            await progress_queue.put(1)
                            
                            if purged_count % 10 == 0:
                                try:
                                    print(f"Progress update - Thread in channel: {channel.name.encode('utf-8', 'replace').decode('utf-8')}, Messages checked: {total_messages_checked}, Messages deleted: {purged_count}")
                                except UnicodeEncodeError:
                                    print(f"Progress update - Thread in channel ID: {channel.id}, Messages checked: {total_messages_checked}, Messages deleted: {purged_count}")
                        
                        except discord.errors.NotFound:
                            pass
                        except discord.errors.Forbidden:
                            errors.append(f"No permission to delete messages in thread in {channel.name}")
                            return
                        except discord.errors.HTTPException as e:
                            if e.status == 429:
                                retry_after = e.retry_after
                                errors.append(f"Rate limited in thread in {channel.name}. Waiting for {retry_after:.2f} seconds.")
                                print(f"Rate limit hit in thread. Waiting for {retry_after:.2f} seconds before continuing.")
                                await asyncio.sleep(retry_after)
                                try:
                                    async with rate_limiter:
                                        await message.delete()
                                    purged_count += 1
                                    await progress_queue.put(1)
                                except Exception as retry_e:
                                    errors.append(f"Error after rate limit in thread in {channel.name}: {str(retry_e)}")
                            else:
                                errors.append(f"HTTP error in thread in {channel.name}: {str(e)}")
                        except Exception as e:
                            errors.append(f"Error in thread in {channel.name}: {str(e)}")

                        await asyncio.sleep(random.uniform(0.5, 1.0))

                if message_count < botconfig.DELETE_USER_MESSAGES_BATCH_SIZE:
                    break

                await asyncio.sleep(random.uniform(1, 2))

            except discord.errors.Forbidden:
                errors.append(f"No permission to access messages in thread in {channel.name}")
                break
            except discord.errors.HTTPException as e:
                if e.status == 429:
                    retry_after = e.retry_after
                    errors.append(f"Rate limited while fetching messages in thread in {channel.name}. Waiting for {retry_after:.2f} seconds.")
                    print(f"Rate limit hit while fetching thread messages. Waiting for {retry_after:.2f} seconds before continuing.")
                    await asyncio.sleep(retry_after)
                else:
                    errors.append(f"HTTP error while fetching messages in thread in {channel.name}: {str(e)}")
                    break
            except Exception as e:
                errors.append(f"Unexpected error in thread in {channel.name}: {str(e)}")
                break

    # Process active threads
    for thread in channel.threads:
        try:
            print(f"Checking active thread: {thread.name.encode('utf-8', 'replace').decode('utf-8')} (ID: {thread.id})")
        except UnicodeEncodeError:
            print(f"Checking active thread with ID: {thread.id}")
        
        await process_messages_in_thread(thread)

    # Process archived threads
    try:
        async for thread in channel.archived_threads():
            try:
                print(f"Checking archived thread: {thread.name.encode('utf-8', 'replace').decode('utf-8')} (ID: {thread.id})")
            except UnicodeEncodeError:
                print(f"Checking archived thread with ID: {thread.id}")
            
            await process_messages_in_thread(thread, is_archived=True)
    except discord.Forbidden:
        errors.append(f"No permission to list archived threads in {channel.name}")
    except Exception as e:
        errors.append(f"Error listing archived threads in {channel.name}: {str(e)}")

    # Now process the main channel messages (existing code)
    last_message_id = None
    while True:
        try:
            message_count = 0
            async for message in channel.history(limit=botconfig.DELETE_USER_MESSAGES_BATCH_SIZE, before=discord.Object(id=last_message_id) if last_message_id else None):
                message_count += 1
                total_messages_checked += 1
                last_message_id = message.id

                if message.author.name.lower() == username.lower():
                    try:
                        # Check for and delete thread if it exists
                        try:
                            thread = message.channel.get_thread(message.id)
                            if thread:
                                try:
                                    await thread.delete()
                                    print(f"Deleted thread {thread.id} attached to message {message.id} in channel {channel.id}")
                                    await asyncio.sleep(0.5)
                                except NotFound:
                                    pass
                                except Forbidden:
                                    errors.append(f"No permission to delete thread in {channel.name}")
                                except HTTPException as e:
                                    if e.status == 429:
                                        retry_after = e.retry_after
                                        errors.append(f"Rate limited when deleting thread in {channel.name}. Waiting for {retry_after:.2f} seconds.")
                                        await asyncio.sleep(retry_after)
                                    else:
                                        errors.append(f"HTTP error when deleting thread in {channel.name}: {str(e)}")
                                        await asyncio.sleep(1)
                        except Exception:
                            # Thread doesn't exist or other error - we can safely ignore this
                            pass

                        # Original message deletion
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
    migrate_database()
    reload_config()
    bot.loop.create_task(continuous_delete_old_messages())

async def process_channel(guild, channel, delete_after):
    delete_count = 0
    messages_checked = 0
    utc_now = datetime.now(pytz.utc)

    if delete_after.total_seconds() <= 0:
        print(f"Invalid delete_after value for channel {channel.id}: {delete_after}")
        return 0, 0
    
    deletion_cutoff = utc_now - delete_after
    last_progress_time = time.time()
    cutoff_snowflake = discord.utils.time_snowflake(deletion_cutoff)

    #print(f"Deletion cutoff time: {deletion_cutoff.isoformat()}")

    async def delete_with_timeout(message, channel, guild):
        async def delete_attempt():
            while True:
                try:
                    # Try to get a thread with the same ID as our message
                    try:
                        thread = message.channel.get_thread(message.id)
                        if thread:
                            print(f"Found thread {thread.id} to delete")
                            try:
                                await thread.delete()
                                print(f"Successfully deleted thread {thread.id}")
                                await asyncio.sleep(0.5)
                            except NotFound:
                                print(f"Thread {thread.id} was already deleted")
                            except Forbidden:
                                print(f"Forbidden to delete thread {thread.id}")
                            except HTTPException as e:
                                if e.status == 429:
                                    retry_after = e.retry_after
                                    print(f"Rate limited when deleting thread. Waiting for {retry_after} seconds.")
                                    await asyncio.sleep(retry_after)
                                else:
                                    print(f"HTTP error when deleting thread: {e}")
                                    await asyncio.sleep(1)
                    except Exception as e:
                        # Thread doesn't exist or other error - we can safely ignore this
                        pass

                    # Original message deletion code
                    await message.delete()
                    return True
                except HTTPException as e:
                    if e.status == 429:  # Rate limit error
                        retry_after = e.retry_after
                        print(f"Rate limited when deleting message {message.id} in channel {channel.id}, guild {guild.id}. Waiting for {retry_after} seconds.")
                        await asyncio.sleep(retry_after)
                    else:
                        print(f"HTTP error when deleting message {message.id} in channel {channel.id}, guild {guild.id}: {e}")
                        return False
                except NotFound:
                    print(f"Message {message.id} not found in channel {channel.id}, guild {guild.id}")
                    return False
                except Forbidden:
                    print(f"Forbidden to delete message {message.id} in channel {channel.id}, guild {guild.id}")
                    return False
                except Exception as e:
                    print(f"Error deleting message {message.id} in channel {channel.id}, guild {guild.id}: {e}")
                    return False

        try:
            return await asyncio.wait_for(delete_attempt(), timeout=botconfig.PROCESS_CHANNEL_TIMEOUT)
        except TimeoutError:
            print(f"Delete operation timed out for message {message.id} in channel {channel.id}, guild {guild.id}")
            return False

    while True:
        try:
            #print(f"Fetching batch for channel {channel.id}, guild {guild.id}")
            fetch_start_time = time.time()
            message_batch = []

            # Use the deletion_cutoff as the initial 'before' parameter
            history_params = {
                'limit': botconfig.PROCESS_CHANNEL_BATCH_SIZE,
                'before': discord.Object(id=cutoff_snowflake),
                'oldest_first': False
            }

            # Log message to show it's working correctly
            #print(f"Asking for messages from channel {channel.id} older than {deletion_cutoff.isoformat()}")
            #print(f"Asking for messages from channel {channel.id} older than {deletion_cutoff.isoformat()}")
            #print(f"History params: {history_params}")

            async for message in handle_rate_limits(channel.history(**history_params)):
                #print(f"Retrieved message with ID {message.id}, created at {message.created_at.isoformat()}")
                message_batch.append(message)
                messages_checked += 1
            fetch_end_time = time.time()
            #print(f"Fetched {len(message_batch)} messages in {fetch_end_time - fetch_start_time:.2f} seconds")
            #print(f"Retrieved {len(message_batch)} messages. Oldest message (if any) is from {message_batch[-1].created_at.isoformat() if message_batch else 'N/A'}")

            if not message_batch:
                #print(f"No more messages to process in channel {channel.id}, guild {guild.id}")
                break

            for message in message_batch:
                delete_start_time = time.time()
                try:
                    delete_success = await delete_with_timeout(message, channel, guild)
                    if delete_success:
                        delete_count += 1
                        await progress_queue.put(1)
                    
                    delete_end_time = time.time()
                    print(f"Delete operation took {delete_end_time - delete_start_time:.2f} seconds")
                    
                    await asyncio.sleep(random.uniform(0.5, 1))
                    
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
            
                current_time = time.time()
                if delete_count % 10 == 0 and delete_count > 0 or current_time - last_progress_time > 60:
                    try:
                        print(f"Progress update - Channel: {channel.name}, Guild: {guild.name}, Messages checked: {messages_checked}, Messages deleted: {delete_count}")
                    except UnicodeEncodeError:
                        print(f"Progress update - Channel ID: {channel.id}, Guild ID: {guild.id}, Messages checked: {messages_checked}, Messages deleted: {delete_count}")
                    last_progress_time = current_time

            # Add a delay between batches
            await asyncio.sleep(random.uniform(0.5, 1))

        except Forbidden:
            print(f"No permission to access channel {channel.id} in guild {guild.id}")
            break
        except Exception as e:
            print(f"Error processing channel {channel.id} in guild {guild.id}: {e}")
            break

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
            elif e.status == 503:  # Service unavailable, shouldn't need this but here we are
                print(f"Service unavailable error when fetching message history. Waiting for 4 seconds.")
                await asyncio.sleep(5)
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
    connection = create_connection()
    if connection:
        try:
            cleanup_inaccessible_channels(connection)
            cursor = connection.cursor(MySQLdb.cursors.DictCursor)
            cursor.execute("SELECT * FROM channel_config")
            configs = cursor.fetchall()
            
            new_tasks = []

            for config in configs:
                channel_id = config['channel_id']
                
                # Skip this channel if it's already being processed, but we still need
                # to try to update its timestamp if we can access it
                already_processing = channel_id in channels_in_progress
                if already_processing:
                    print(f"Channel ID: {channel_id} is still being processed from a previous run. Will only update timestamp if accessible.")
                
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

                if channel.permissions_for(guild.me).read_messages:
                    # Update channel information since we have access, regardless of whether we'll process it
                    update_channel_info(connection, guild, channel)
                    
                    # If we're already processing this channel, skip creating a new task
                    if already_processing:
                        print(f"Updated timestamp for channel {channel_id} but skipping deletion as it's still being processed")
                        continue
                    
                    # Only proceed with deletion if we have the necessary permissions
                    if channel.permissions_for(guild.me).manage_messages:
                        if not channel.permissions_for(guild.me).manage_threads:
                            print(f"Bot doesn't have permission to manage threads in channel (Guild ID: {config['guild_id']}, Channel ID: {channel_id})")
                            continue

                        delete_after = timedelta(minutes=config['delete_after'])
                        task = asyncio.create_task(process_channel_wrapper(guild, channel, delete_after))
                        channel_tasks[channel_id] = task
                        new_tasks.append(task)
                    else:
                        print(f"Bot doesn't have permission to delete messages in channel (Guild ID: {config['guild_id']}, Channel ID: {channel_id})")
                else:
                    print(f"Bot doesn't have permission to read messages in channel (Guild ID: {config['guild_id']}, Channel ID: {channel_id})")
                    continue

            # Wait for new tasks to complete or for TASK_INTERVAL_SECONDS seconds, whichever comes first
            if new_tasks:
                total_deleted = 0
                total_checked = 0
                completed_tasks = 0
                try:
                    done, pending = await asyncio.wait(new_tasks, timeout=botconfig.TASK_INTERVAL_SECONDS, return_when=asyncio.ALL_COMPLETED)
                    for task in done:
                        try:
                            result = task.result()
                            if isinstance(result, tuple):
                                deleted, checked = result
                                total_deleted += deleted
                                total_checked += checked
                                completed_tasks += 1
                        except Exception as e:
                            print(f"Task error: {e}")
                            print(f"Traceback: {traceback.format_exc()}")
                    
                    print(f"{completed_tasks} task(s) completed. Total messages deleted: {total_deleted}, Total messages checked: {total_checked}")
                    
                    if pending:
                        print(f"{len(pending)} tasks are still running and will continue in the background.")
                except asyncio.TimeoutError:
                    print(f"Timeout reached after {botconfig.TASK_INTERVAL_SECONDS} seconds. {completed_tasks} tasks completed, {len(new_tasks) - completed_tasks} tasks are still running and will continue in the background.")

            if len(channels_in_progress) > 0:
                print(f"Delete old messages task iteration complete, however there are {len(channels_in_progress)} Channel(s) still being processed")

        except Error as e:
            print(f"Error reading from database: {e}")
        finally:
            cursor.close()
            connection.close()

async def continuous_delete_old_messages():
    progress_task = asyncio.create_task(update_progress())
    try:
        while True:
            #print("Starting new iteration of continuous_delete_old_messages")
            reload_config()  # This will check and reload the config if necessary
            start_time = asyncio.get_event_loop().time()
            print(f"Starting delete_old_messages_task at {start_time}")
            
            await delete_old_messages_task()
            
            end_time = asyncio.get_event_loop().time()
            elapsed_time = end_time - start_time
            #print(f"Finished delete_old_messages_task. Elapsed time: {elapsed_time:.2f} seconds")
            
            if elapsed_time < botconfig.TASK_INTERVAL_SECONDS:
                wait_time = botconfig.TASK_INTERVAL_SECONDS - elapsed_time
                print(f"Ran for {elapsed_time:.2f} seconds, waiting for {wait_time:.2f} seconds before next iteration")
                await asyncio.sleep(wait_time)
            else:
                print(f"Task took longer than interval ({elapsed_time:.2f} > {botconfig.TASK_INTERVAL_SECONDS}), starting next iteration immediately")
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
async def add_channel(interaction: discord.Interaction, channel: Union[discord.TextChannel, discord.VoiceChannel], time: int, unit: str):
    # Check if the bot has permission to manage messages in the channel
    if not channel.permissions_for(interaction.guild.me).manage_messages:
        await interaction.response.send_message(f"Error: I don't have permission to manage messages in {channel.name}. Please grant me the 'Manage Messages' permission in this channel before adding it.", ephemeral=True)
        return

    try:
        minutes = convert_to_minutes(time, unit)
    except ValueError as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return

    connection = create_connection()
    if connection:
        try:
            cursor = connection.cursor()
            sql = """INSERT INTO channel_config 
                    (guild_id, channel_id, delete_after, guild_name, channel_name, last_updated) 
                    VALUES (%s, %s, %s, %s, %s, NOW()) 
                    ON DUPLICATE KEY UPDATE 
                    delete_after = %s, guild_name = %s, channel_name = %s, last_updated = NOW()"""
            val = (interaction.guild_id, channel.id, minutes, interaction.guild.name, channel.name, 
                minutes, interaction.guild.name, channel.name)
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
async def remove_channel(interaction: discord.Interaction, channel: Union[discord.TextChannel, discord.VoiceChannel]):
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
            val = (interaction.guild.id,)
            cursor.execute(sql, val)
            channels = cursor.fetchall()
            
            if channels:
                message = "Channels with auto-delete in this server:\n"
                for channel_data in channels:
                    channel = interaction.guild.get_channel(channel_data['channel_id'])
                    if channel:
                        if channel.permissions_for(interaction.guild.me).manage_messages:
                            message += f"- {channel.name}: {format_time(channel_data['delete_after'])}\n"
                        else:
                            message += f"- {channel.name}: {format_time(channel_data['delete_after'])} (invalid permissions or no access)\n"
                    else:
                        message += f"- Unknown Channel (ID: {channel_data['channel_id']}): {format_time(channel_data['delete_after'])} (invalid permissions or no access)\n"
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

    await interaction.followup.send(f"Starting purge operation for user: '{username}'. **NOTE:** Due to Discord limitations, you may stop getting progress updates about this process. Rest assured, the process will continue running until it successfully completes.", ephemeral=True)
    
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
    for channel in interaction.guild.channels:  #interaction.guild.text_channels:
        if not hasattr(channel, 'history'):
            continue  # Skip channels without history attribute
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
async def purge_channel(interaction: discord.Interaction, channel: Union[discord.TextChannel, discord.VoiceChannel]):
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
                    # Check for and delete thread if it exists
                    try:
                        thread = message.channel.get_thread(message.id)
                        if thread:
                            try:
                                await thread.delete()
                                print(f"Deleted thread {thread.id} attached to message {message.id} in channel {channel.id}")
                                await asyncio.sleep(0.5)
                            except discord.errors.NotFound:
                                pass  # Thread already deleted
                            except discord.errors.Forbidden:
                                print(f"No permission to delete thread in channel: {channel.id}")
                            except discord.errors.HTTPException as e:
                                if e.status == 429:  # Rate limit error
                                    retry_after = e.retry_after
                                    print(f"Rate limited when deleting thread. Waiting for {retry_after:.2f} seconds.")
                                    await asyncio.sleep(retry_after)
                                else:
                                    print(f"HTTP error while deleting thread: {e}")
                                    await asyncio.sleep(1)
                    except Exception:
                        # Thread doesn't exist or other error - we can safely ignore this
                        pass

                    # Original message deletion
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
                async for message in channel.history(limit=botconfig.PURGE_CHANNEL_BATCH_SIZE, before=discord.Object(id=last_message_id) if last_message_id else None):
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

@bot.tree.command(name="find_orphaned_threads", description="Find threads whose parent messages have been deleted")
@app_commands.describe(
    delete_orphans="Whether to delete the orphaned threads that are found (default: False)"
)
@app_commands.checks.has_permissions(moderate_members=True)
async def find_orphaned_threads(interaction: discord.Interaction, delete_orphans: bool = False):
    await interaction.response.defer(ephemeral=True)
    
    if not interaction.guild.me.guild_permissions.manage_threads:
        await interaction.followup.send("I don't have permission to manage threads in this server.", ephemeral=True)
        return

    try:
        print(f"Starting orphaned thread search in guild: {interaction.guild.name.encode('utf-8', 'replace').decode('utf-8')} (ID: {interaction.guild.id})")
    except UnicodeEncodeError:
        print(f"Starting orphaned thread search in guild with ID: {interaction.guild.id}")
    
    orphaned_threads = []
    threads_checked = 0
    threads_deleted = 0
    channel_errors = []
    last_status_update = 0

    await interaction.followup.send("Searching for orphaned threads. This might take a while...", ephemeral=True)

    for channel in interaction.guild.channels:
        # Skip channels that can't contain threads
        if not isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
            continue

        if not channel.permissions_for(interaction.guild.me).view_channel:
            try:
                channel_errors.append(f"No permission to view channel: {channel.name}")
            except UnicodeEncodeError:
                channel_errors.append(f"No permission to view channel ID: {channel.id}")
            continue

        try:
            try:
                print(f"Checking channel: {channel.name.encode('utf-8', 'replace').decode('utf-8')} (ID: {channel.id})")
            except UnicodeEncodeError:
                print(f"Checking channel with ID: {channel.id}")
            
            # Check active threads
            for thread in channel.threads:
                threads_checked += 1
                try:
                    # Try to fetch the parent message
                    try:
                        parent_message = await channel.fetch_message(thread.id)
                    except (discord.NotFound, discord.HTTPException):
                        # Parent message doesn't exist - this is an orphaned thread
                        orphaned_threads.append((channel, thread))
                        
                        # Status update every 10 orphaned threads found
                        if len(orphaned_threads) % 10 == 0:
                            status_message = f"Found {len(orphaned_threads)} orphaned threads so far... (Checked {threads_checked} total threads)"
                            print(status_message)
                            await interaction.followup.send(status_message, ephemeral=True)
                        
                        if delete_orphans:
                            try:
                                await thread.delete()
                                threads_deleted += 1
                                try:
                                    print(f"Deleted orphaned thread {thread.name.encode('utf-8', 'replace').decode('utf-8')} (ID: {thread.id}) in channel {channel.name.encode('utf-8', 'replace').decode('utf-8')} (ID: {channel.id})")
                                except UnicodeEncodeError:
                                    print(f"Deleted orphaned thread ID: {thread.id} in channel ID: {channel.id}")
                                await asyncio.sleep(0.5)  # Rate limiting
                            except discord.Forbidden:
                                try:
                                    channel_errors.append(f"No permission to delete thread in {channel.name}")
                                except UnicodeEncodeError:
                                    channel_errors.append(f"No permission to delete thread in channel ID: {channel.id}")
                            except discord.HTTPException as e:
                                if e.status == 429:
                                    retry_after = e.retry_after
                                    print(f"Rate limited when deleting thread. Waiting for {retry_after:.2f} seconds.")
                                    await asyncio.sleep(retry_after)
                                    # Retry the deletion
                                    try:
                                        await thread.delete()
                                        threads_deleted += 1
                                    except Exception as retry_e:
                                        try:
                                            channel_errors.append(f"Error deleting thread in {channel.name} after rate limit: {str(retry_e)}")
                                        except UnicodeEncodeError:
                                            channel_errors.append(f"Error deleting thread in channel ID: {channel.id} after rate limit: {str(retry_e)}")
                                else:
                                    try:
                                        channel_errors.append(f"HTTP error deleting thread in {channel.name}: {str(e)}")
                                    except UnicodeEncodeError:
                                        channel_errors.append(f"HTTP error deleting thread in channel ID: {channel.id}: {str(e)}")
                except Exception as e:
                    try:
                        channel_errors.append(f"Error checking thread in {channel.name}: {str(e)}")
                    except UnicodeEncodeError:
                        channel_errors.append(f"Error checking thread in channel ID: {channel.id}: {str(e)}")

            # Also check archived threads
            try:
                async for thread in channel.archived_threads():
                    threads_checked += 1
                    try:
                        # Try to fetch the parent message
                        try:
                            parent_message = await channel.fetch_message(thread.id)
                        except (discord.NotFound, discord.HTTPException):
                            # Parent message doesn't exist - this is an orphaned thread
                            orphaned_threads.append((channel, thread))
                            
                            # Status update every 10 orphaned threads found
                            if len(orphaned_threads) % 10 == 0:
                                status_message = f"Found {len(orphaned_threads)} orphaned threads so far... (Checked {threads_checked} total threads)"
                                print(status_message)
                                await interaction.followup.send(status_message, ephemeral=True)
                            
                            if delete_orphans:
                                try:
                                    print(f"Attempting to delete archived thread ID: {thread.id}")
                                    print(f"Thread state - Archived: {thread.archived}, Locked: {thread.locked}, Type: {thread.type}")
                                    
                                    # Unarchive and immediately delete without delay
                                    try:
                                        print(f"Attempting to unarchive thread {thread.id} before deletion")
                                        await thread.edit(archived=False)
                                        print(f"Successfully unarchived thread {thread.id}, attempting immediate deletion")
                                        # Immediately try to delete without delay
                                        await thread.delete()
                                        threads_deleted += 1
                                        try:
                                            print(f"Successfully deleted formerly-archived orphaned thread {thread.name.encode('utf-8', 'replace').decode('utf-8')} (ID: {thread.id}) in channel {channel.name.encode('utf-8', 'replace').decode('utf-8')} (ID: {channel.id})")
                                        except UnicodeEncodeError:
                                            print(f"Successfully deleted formerly-archived orphaned thread ID: {thread.id} in channel ID: {channel.id}")
                                    except discord.Forbidden as e:
                                        print(f"Forbidden error during unarchive/delete sequence for thread {thread.id}: {str(e)}")
                                        try:
                                            channel_errors.append(f"No permission to unarchive/delete thread in {channel.name}")
                                        except UnicodeEncodeError:
                                            channel_errors.append(f"No permission to unarchive/delete thread in channel ID: {channel.id}")
                                    except discord.HTTPException as e:
                                        print(f"HTTP error during unarchive/delete sequence for thread {thread.id}: {str(e)} (Status: {e.status}, Code: {e.code})")
                                        if e.status == 429:
                                            retry_after = e.retry_after
                                            print(f"Rate limited during unarchive/delete sequence. Waiting for {retry_after:.2f} seconds.")
                                            await asyncio.sleep(retry_after)
                                            # Retry the sequence
                                            try:
                                                await thread.edit(archived=False)
                                                await thread.delete()
                                                threads_deleted += 1
                                            except Exception as retry_e:
                                                print(f"Retry failed for thread {thread.id}: {str(retry_e)}")
                                                try:
                                                    channel_errors.append(f"Error during unarchive/delete retry in {channel.name}: {str(retry_e)}")
                                                except UnicodeEncodeError:
                                                    channel_errors.append(f"Error during unarchive/delete retry in channel ID: {channel.id}: {str(retry_e)}")
                                    except Exception as e:
                                        print(f"Unexpected error during unarchive/delete sequence for thread {thread.id}: {str(e)}")
                                        print(f"Error type: {type(e)}")
                                        print(f"Full error traceback: {traceback.format_exc()}")
                                    
                                    # Rate limiting delay after the entire operation
                                    await asyncio.sleep(0.5)
                                    
                                except Exception as outer_e:
                                    print(f"Outer exception during thread handling {thread.id}: {str(outer_e)}")
                                    print(f"Full outer error traceback: {traceback.format_exc()}")
                    except Exception as e:
                        try:
                            channel_errors.append(f"Error checking thread in {channel.name}: {str(e)}")
                        except UnicodeEncodeError:
                            channel_errors.append(f"Error checking thread in channel ID: {channel.id}: {str(e)}")
            except discord.Forbidden:
                try:
                    channel_errors.append(f"No permission to list archived threads in {channel.name}")
                except UnicodeEncodeError:
                    channel_errors.append(f"No permission to list archived threads in channel ID: {channel.id}")
            except Exception as e:
                try:
                    channel_errors.append(f"Error listing archived threads in {channel.name}: {str(e)}")
                except UnicodeEncodeError:
                    channel_errors.append(f"Error listing archived threads in channel ID: {channel.id}: {str(e)}")

        except discord.Forbidden:
            try:
                channel_errors.append(f"No permission to list threads in {channel.name}")
            except UnicodeEncodeError:
                channel_errors.append(f"No permission to list threads in channel ID: {channel.id}")
            continue
        except Exception as e:
            try:
                channel_errors.append(f"Error processing channel {channel.name}: {str(e)}")
            except UnicodeEncodeError:
                channel_errors.append(f"Error processing channel ID: {channel.id}: {str(e)}")
            continue

    # Prepare the summary message
    summary = f"Found {len(orphaned_threads)} orphaned threads out of {threads_checked} threads checked.\n\n"
    
    if delete_orphans:
        summary += f"Successfully deleted {threads_deleted} orphaned threads.\n\n"

    if orphaned_threads:
        summary += "Orphaned threads found in:\n"
        for channel, thread in orphaned_threads:
            try:
                summary += f"- #{channel.name}: {thread.name} (ID: {thread.id})\n"
            except UnicodeEncodeError:
                summary += f"- Channel ID: {channel.id}, Thread ID: {thread.id}\n"
    
    if channel_errors:
        summary += "\nErrors encountered:\n"
        #for error in channel_errors[:10]:  # Limit to first 10 errors
        #    summary += f"- {error}\n"
        #if len(channel_errors) > 10:
        #    summary += f"... and {len(channel_errors) - 10} more errors."
        for error in channel_errors:
            summary += f"- {error}\n"

    # Print the final summary to console
    print("\nFinal Summary of Orphaned Thread Search:")
    print(summary)

    # Split the message if it's too long
    if len(summary) > 2000:
        chunks = [summary[i:i+1990] for i in range(0, len(summary), 1990)]
        for i, chunk in enumerate(chunks):
            if i == 0:
                await interaction.followup.send(chunk, ephemeral=True)
            else:
                await interaction.followup.send(chunk, ephemeral=True)
    else:
        await interaction.followup.send(summary, ephemeral=True)

    try:
        print(f"Orphaned thread search completed in guild: {interaction.guild.name.encode('utf-8', 'replace').decode('utf-8')} (ID: {interaction.guild.id})")
    except UnicodeEncodeError:
        print(f"Orphaned thread search completed in guild ID: {interaction.guild.id}")

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

@bot.tree.command(name="lookup_channel", description="Look up a channel by its ID")
@app_commands.describe(channel_id="The ID of the channel to look up")
@app_commands.checks.has_permissions(moderate_members=True)
async def lookup_channel(interaction: discord.Interaction, channel_id: str):
    try:
        # Convert the input to an integer
        channel_id = int(channel_id)
        
        # Attempt to fetch the channel
        channel = bot.get_channel(channel_id)
        
        if channel:
            # Channel found, send the name and type
            channel_type = str(channel.type).split('.')[-1]  # Get the channel type as a string
            await interaction.response.send_message(f"The channel with ID {channel_id} is named: {channel.name}\nType: {channel_type}\nGuild: {channel.guild.name}", ephemeral=True)
        else:
            # Channel not found
            await interaction.response.send_message(f"No channel found with ID {channel_id}. The bot might not have access to this channel.", ephemeral=True)
    
    except ValueError:
        # Invalid input (not a number)
        await interaction.response.send_message("Invalid input. Please provide a valid numeric channel ID.", ephemeral=True)
    except Exception as e:
        # Handle any other unexpected errors
        print(f"Error in lookup_channel command: {str(e)}")
        await interaction.response.send_message("An error occurred while processing your request.", ephemeral=True)

@lookup_channel.error
async def lookup_channel_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
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
