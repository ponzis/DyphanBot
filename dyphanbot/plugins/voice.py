## Rewritten Voice Plugin ##
import asyncio
import textwrap
import datetime

from async_timeout import timeout
from functools import partial
from youtube_dl import YoutubeDL

import discord
import dyphanbot.utils as utils

YTDL_OPTS = {
    'format': 'webm[abr>0]/bestaudio/best',
    'prefer_ffmpeg': True,
    'ignoreerrors': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0'  # ipv6 addresses cause issues sometimes
}

FFMPEG_OPTS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

ytdl = YoutubeDL(YTDL_OPTS)

class YTDLSource(discord.PCMVolumeTransformer):
    """ Playable source object for YTDL """
    def __init__(self, source, *, data, requester):
        super().__init__(source)
        self.requester = requester

        self.title = data.get('title')
        self.description = data.get('description')
        self.web_url = data.get('webpage_url')
        self.views = data.get('view_count')
        self.is_live = bool(data.get('is_live'))
        self.likes = data.get('like_count')
        self.dislikes = data.get('dislike_count')
        self.duration = data.get('duration')
        self.uploader = data.get('uploader')
        self.thumbnail = data.get('thumbnail')

        # upload date handling
        date = data.get('upload_date')
        if date:
            try:
                date = datetime.datetime.strptime(date, '%Y%M%d').date()
            except ValueError:
                date = None

        self.upload_date = date

    def __getitem__(self, item: str):
        """Allows us to access attributes similar to a dict.
        This is only useful when you are NOT downloading.
        """
        return self.__getattribute__(item)

class MusicPlayer(object):
    """ Handles fetching and parsing media from URLs using youtube-dl, as well
    as the playlist queue.
    """
    def __init__(self, client, message):
        self.client = client
        self.message = message
        self.guild = message.guild
        self.channel = message.channel

        self.queue = asyncio.Queue()
        self.next = asyncio.Event()

        self.now_playing = None
        self.volume = 0.5
        self.current = None

        self.audio_player = self.client.loop.create_task(self.player_loop())

    async def create_sources(self, message, search: str, *, loop, download=False, silent=False):
        loop = loop or asyncio.get_event_loop()
        if not silent:
            adding_msg = await message.channel.send("Fetching requested song(s)....")
        to_run = partial(ytdl.extract_info, url=search, download=download)
        data = await loop.run_in_executor(None, to_run)

        entries = []
        if 'entries' in data:
            # Load all videos in the playlist if multiple entries are found
            for entry in data['entries']:
                entries.append(entry)
        else:
            # Load the single entry
            entries = [data]

        if not silent:
            mtext = "Adding songs to playlist queue..." if len(entries) > 1 else "Added `{0}` to the playlist queue.".format(entries[0].get("title"))
            await adding_msg.edit(content=mtext)

        for entry in entries:
            # Loop through loaded entries
            if entry is None:
                continue
            if download:
                source = YTDLSource(discord.FFmpegPCMAudio(ytdl.prepare_filename(entry), **FFMPEG_OPTS), data=entry, requester=message.author)
            else:
                source = {'webpage_url': entry['webpage_url'], 'requester': message.author, 'title': entry['title']}

            await self.queue.put(source)
            #await self.channel.send(f'Added `{entry["title"]}` to the Queue.', delete_after=15)
            if len(entries) > 1 and not silent:
                mtext += "\n    **+** `{0}`".format(entry["title"])
                await adding_msg.edit(content=mtext)

    async def regather_stream(self, data, *, loop):
        """Used for preparing a stream, instead of downloading.
        Since Youtube Streaming links expire."""
        loop = loop or asyncio.get_event_loop()
        requester = data['requester']

        to_run = partial(ytdl.extract_info, url=data['webpage_url'], download=False)
        data = await loop.run_in_executor(None, to_run)

        return YTDLSource(discord.FFmpegPCMAudio(data['url'], **FFMPEG_OPTS), data=data, requester=requester)

    def np_embed(self, source):
        """ Generates 'Now Playing'/'Now Streaming'/'Paused' status embed. """
        embed = discord.Embed(
            title=source.title,
            colour=discord.Colour(0x7289DA),
            url=source.web_url,
            description=textwrap.shorten(source.description, 157, placeholder="..."),
            timestamp=self.message.created_at
        )

        if source.thumbnail:
            embed.set_thumbnail(url=source.thumbnail)
        embed.set_author(name=("Now Streaming" if source.is_live else "Now Playing") if self.guild.voice_client.is_playing() else "Paused",
            url="https://github.com/buzzbyte/DyphanBot",
            icon_url=utils.get_user_avatar_url(self.message.guild.me)
        )

        embed.set_footer(text="Requested by: {0.display_name}".format(source.requester), icon_url=utils.get_user_avatar_url(source.requester))
        if source.uploader:
            embed.add_field(name="Uploaded by", value=source.uploader, inline=True)

        duration = source.duration
        if duration:
            min, sec = divmod(int(duration), 60)
            hrs, min = divmod(min, 60)
            dfmtstr = "{0:d}:{1:02d}:{2:02d}" if hrs > 0 else "{1:02d}:{2:02d}"
            embed.add_field(name="Duration", value=dfmtstr.format(hrs, min, sec), inline=True)

        return embed

    def played_embed(self, source):
        """ Generates the 'Played' status embed for previously played audio. """
        embed = discord.Embed(
            title=source.title,
            colour=discord.Colour(0x7289DA),
            url=source.web_url
        )

        embed.set_author(name="Played")

        return embed

    async def player_loop(self):
        """ Main player loop """
        await self.client.wait_until_ready()

        while not self.client.is_closed():
            self.next.clear()

            try:
                async with timeout(300): # 5 minutes
                    source = await self.queue.get()
            except asyncio.TimeoutError:
                return self.destroy()

            if not isinstance(source, YTDLSource):
                # Source was probably a stream
                try:
                    source = await self.regather_stream(source, loop=self.client.loop)
                except Exception as e:
                    await self.channel.send("There was an error processing your song... Sorry!! ```py\n{}: {}\n```".format(type(e).__name__, e))
                    continue

            source.volume = self.volume
            self.current = source

            self.guild.voice_client.play(source, after=lambda _: self.client.loop.call_soon_threadsafe(self.next.set))
            await self.update_now_playing()

            await self.next.wait()

            await self.delete_last_playing(source)
            source.cleanup()
            self.current = None

    async def update_now_playing(self):
        """ Deletes previous playing status embed and sends a new one. """
        await self.delete_last_playing()
        self.now_playing = await self.channel.send(embed=self.np_embed(self.current))

    async def delete_last_playing(self, last_source=None):
        """ Deletes the last playing status embed and, if applicable, replaces
        it with a played status embed.
        """
        if self.now_playing:
            try:
                await self.now_playing.delete()
                if last_source:
                    await self.channel.send(embed=self.played_embed(last_source))
            except discord.HTTPException:
                pass

    def clear_queue(self):
        """ Clears the playlist queue. """
        for entry in self.queue._queue:
            if isinstance(entry, YTDLSource):
                entry.cleanup()
        self.queue._queue.clear()

    async def cleanup(self):
        """ Disconnects from the voice cliend and clears the playlist queue. """
        try:
            await self.guild.voice_client.disconnect()
        except AttributeError:
            pass

        self.clear_queue()

    def destroy(self):
        """ Uninitializes this player (basically disconnects from voice and
        clears the queue).
        """
        return self.client.loop.create_task(self.cleanup())

class Music(object):
    """ Commands for playing and controlling music playback.
    Acts as an interface for youtube-dl, basically.
    """

    def __init__(self, dyphanbot):
        self.dyphanbot = dyphanbot
        self.players = {}

    def get_player(self, client, message):
        """Retrieve the guild player, or generate one."""
        try:
            player = self.players[message.guild.id]
        except KeyError:
            player = MusicPlayer(client, message)
            self.players[message.guild.id] = player

        return player

    async def join(self, client, message, args):
        """ Connects to the user's voice channel. """
        v_channel = message.author.voice.channel
        if not v_channel:
            await message.channel.send("You're not in a voice channel... ~~dummy~~")
            return False

        v_client = message.guild.voice_client
        if v_client:
            await v_client.move_to(v_channel)
        else:
            await v_channel.connect()
        return True

    async def play(self, client, message, args):
        """ Plays audio from a URL, if provided. Otherwise, resumes paused
        audio.
        This will also call `join` if the bot is not already connected to a
        voice channel.
        """
        song = " ".join(args)

        v_client = message.guild.voice_client
        if not v_client:
            joined = await self.join(client, message, [])
            if not joined:
                return

        player = self.get_player(client, message)
        if song.strip() == "":
            if player.current and v_client.is_paused():
                v_client.resume()
                await player.update_now_playing()
            else:
                await message.channel.send("Nothing was paused, bruh.")
            return

        await player.create_sources(message, song, loop=client.loop)

    async def pause(self, client, message, args):
        """ Pause currently playing audio, if any. """
        v_client = message.guild.voice_client
        if not v_client or not v_client.is_playing():
            return await message.channel.send("I wasn't even playing anything~~, baka~~!")
        elif v_client.is_paused():
            return await message.channel.send("Already paused, bruh.")

        v_client.pause()
        player = self.get_player(client, message)
        await player.update_now_playing()

    async def stop(self, client, message, args):
        """ Stops playing audio and clears the playlist queue. """
        v_client = message.guild.voice_client
        if not v_client or not v_client.is_connected():
            return await message.channel.send("I wasn't even playing anything!")
        v_client.stop()
        player = self.get_player(client, message)
        player.clear_queue()
        await message.channel.send("Stopped the playlist.")

    async def playlist(self, client, message, args):
        """ Adds a specified list of searches/URLs to the queue. """
        await message.channel.send("Not implemented yet...")

    async def volume(self, client, message, args):
        """ Sets, increases, or decreases the volume. """
        v_client = message.guild.voice_client
        if not v_client or not v_client.is_connected():
            return await message.channel.send("I ain't in a voice chat, bruh..")

        player = self.get_player(client, message)
        if len(args) > 0:
            if args[0].strip() == "up":
                vol = player.volume * 100
                vol += 5
                if vol > 100:
                    vol = 100
            elif args[0].strip() == "down":
                vol = player.volume * 100
                vol -= 5
                if vol < 1:
                    vol = 1
            else:
                try:
                    vol = int(args[0])
                    if not 0 < vol < 101:
                        return await message.channel.send("Volume goes from 1 to 100 ...")
                except ValueError:
                    return await message.channel.send(".. What? Use either `up`, `down`, or a number between 1 to 100.")
        else:
            return await message.channel.send("Volume can be either `up`, `down`, or a number between 1 to 100.")

        if v_client.source:
            v_client.source.volume = vol / 100
        player.volume = vol / 100
        await message.channel.send("**`{0}`**: Set the volume to **{1}%**".format(message.author, vol))

    async def skip(self, client, message, args):
        """ Skip the currently playing audio. """
        v_client = message.guild.voice_client
        if not v_client or not v_client.is_connected():
            return await message.channel.send("I wasn't even playing anything!")

        if v_client.is_paused():
            pass
        elif not v_client.is_playing():
            return

        v_client.stop()

    async def status(self, client, message, args):
        """ Displays the 'Now Playing'/'Now streaming'/'Paused' embed status
        containing the audio info.
        """
        v_client = message.guild.voice_client
        if not v_client or not v_client.is_connected():
            return await message.channel.send("I'm not even connected to a voice channel, dude!!")

        player = self.get_player(client, message)
        if not player.current:
            return await message.channel.send("I'm not playing anything...")

        await player.update_now_playing()

    async def leave(self, client, message, args):
        """ Disconnects from the voice client. """
        v_client = message.guild.voice_client
        if not v_client or not v_client.is_connected():
            return await message.channel.send("I'm not connected to a voice channel, bruh...")

        player = self.get_player(client, message)
        player.destroy()

class Voice(object):
    """ Contains the Voice command which handles the Music sub-commands """

    def __init__(self, dyphanbot):
        self.dyphanbot = dyphanbot
        self.music = Music(dyphanbot)

    async def voice(self, client, message, args):
        """ The Voice command.
        Handles subcommands for playing and controlling audio.
        """
        sub_cmds = ['join', 'play', 'fplay', 'pause', 'stop', 'playlist',
                    'volume', 'skip', 'status', 'queue', 'leave']

        if len(args) > 0:
            scmd = args[0].strip()
            if scmd in sub_cmds:
                if not hasattr(self.music, scmd):
                    await message.channel.send("Not implemented yet...")
                await getattr(self.music, scmd)(client, message, args[1:])
            else:
                await message.channel.send("lol wut?")
        else:
            await message.channel.send("La la la!!")

def plugin_init(dyphanbot):
    """ Plugin entry point. """
    voiceplugin = Voice(dyphanbot)
    dyphanbot.add_command_handler("voice", voiceplugin.voice)