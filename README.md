# Welcome to Claudelete!
Claudelete is a Discord bot that allows you to configure time-based auto delete rules for channels on your Discord server.

It was written mostly by Claude.ai, hence the name ***Claude***lete.

## Setting configuration options
The bot needs you to configure a few variables, to do so, simply rename `cdconfig.py.dist` to `cdconfig.py` and fill out the options for database information, and your bot token from Discord.

## Running as a service
You can easily run Claudelete as a system service with systemd. Simply modify the `claudelete-bot.service` file to point at the instance you installed, then move this file to `~/.config/systemd/user/`, then run the commands:

```
systemd --user enable claudelete-bot.service
systemd --user start claudelete-bot.service
```

## Checking the logs
If you run Claudelete from the command line, then logs are being printed to STDOUT. If you run Claudelete using the systemd service, then use the command `journalctl --user-unit claudelete-bot.service` to view the logs. You can also "tail" the logs by adding the `-f` switch to the previous command, so it would be `journalctl -f --user-unit claudelete-bot.service`.

## What data is stored in the database?
The database holds a single table with 4 columns, they are:
1. **id** - This is a primary key on the table, and increments with each addition to the table. This is how the row is referenced for update and delete options.
2. **guild_id** - Claudelete supports being used by multiple servers, this column holds the Discord internal numeric value for the Server it is in.
3. **channel_id** - This is the Discord internal numeric value for the Channel that you've set rules for.
4. **delete_after** - This is a number, in minutes, that messages should be deleted after.

## Command Reference
### IMPORTANT NOTE
You will need to add the "Claudelete" role to every private channel where you want the bot to have access. For public channels, the bot will automatically have access.

### Add a channel to be monitored
To add a channel to monitor for auto deleting, simply use the `/add_channel` command. You will be prompted for channel name, time, and unit (Minutes, Hours, Days, Weeks).

### Remove a channel from monitoring
To remove a channel from monitoring, use the `/remove_channel` command. You will be prompted for which channel to remove from monitoring.

### Update the timeframe for auto deleting
If you want to change the amount of time before posts get deleted, use the `/update_time` command. You will be prompted for channel name, time, and unit (Minutes, Hours, Days, Weeks).

### Show which channels are being monitored
To see the channels that are being monitored on your server, use the `/list_channels` command.

### Purge a channel of all messages
You can purge a channel of all messages using the `/purge_channel` command. This will prompt you to select the channel where messages will be purged.

### Purge a user from your server
You can also purge a user from your server, this will delete all messages sent from that user in all channels where the bot has access. Use the `/purge_user` command. You will be prompted for the user name (Note: This is not the display name, nor is it the numeric user ID assigned by discord).