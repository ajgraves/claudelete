# Welcome to Claudelete!
Claudelete is a Discord bot that allows you to configure time-based auto delete rules for channels on your Discord server.

It was written mostly by Claude.ai, hence the name ***Claude***lete.

## Setting configuration options
The bot needs you to configure a few variables, to do so, simply rename `cdconfig.py.dist` to `cdconfig.py` and fill out the options for database information, and your bot token from Discord.

## What data is stored in the database?
The database holds a single table with 4 columns, they are:
1. **id** - This is a primary key on the table, and increments with each addition to the table. This is how the row is referenced for update and delete options.
2. **guild_id** - Claudelete supports being used by multiple servers, this column holds the Discord internal numeric value for the Server it is in.
3. **channel_id** - This is the Discord internal numeric value for the Channel that you've set rules for.
4. **delete_after** - This is a number, in minutes, that messages should be deleted after.
