# Treebard

Treebard is an IRC/Discord bot written in pure python with various features.
Both the Discord and IRC clients are written using asyncio.

## Code overview

* `discord.py` contains the Discord client code
* `pybot.py` contains the IRC client code
* `markov.py` is a n-gram probability based Markov Chain text generator
* `nntextgen.py` uses a LSTM-based neural network for text generation
* `srl_approve.py` is used for automating user moderation on a VBulitin forum

## Basic usage

First create a bot object with either:

`b = discord.DiscordBot('BotTokenHere')`

or

`b = pybot.IRCBot()`

where you can fill in various keyword arguments.

Finally, await on `b.connect(...)` with appropriate arugments. 
