import asyncio
import datetime
import logging
import time
from collections import defaultdict
from contextlib import suppress
from datetime import timedelta
from io import BytesIO
from typing import Any, Iterable, NoReturn, Optional, Set

import discord
import prettytable
import pytz
from redbot.core import Config, checks, commands
from redbot.core.utils.chat_formatting import box, pagify
from tsutils.enums import Server, StarterGroup
from tsutils.formatting import normalize_server_name
from tsutils.helper_classes import DummyObject
from tsutils.helper_functions import conditional_iterator, repeating_timer

from padevents.autoevent_mixin import AutoEvent
from padevents.enums import DungeonType, EventLength
from padevents.events import Event, EventList, SERVER_TIMEZONES

logger = logging.getLogger('red.padbot-cogs.padevents')

SUPPORTED_SERVERS = ["JP", "NA", "KR"]
GROUPS = ['red', 'blue', 'green']


class PadEvents(commands.Cog, AutoEvent):
    """Pad Event Tracker"""

    def __init__(self, bot, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bot = bot

        self.config = Config.get_conf(self, identifier=940373775)
        self.config.register_global(sent={}, last_daychange=None)
        self.config.register_guild(pingroles={})
        self.config.register_channel(guerrilla_servers=[], daily_servers=[], do_aep_post=True)
        self.config.register_user(dmevents=[])

        # Load event data
        self.events = set()
        self.started_events = set()

        self.fake_uid = -time.time()

        self._event_loop = bot.loop.create_task(self.reload_padevents())
        self._refresh_loop = bot.loop.create_task(self.do_loop())
        self._daily_event_loop = bot.loop.create_task(self.show_daily_info())

    async def red_get_data_for_user(self, *, user_id):
        """Get a user's personal data."""
        aeds = await self.config.user_from_id(user_id).dmevents()
        if aeds:
            data = f"You have {len(aeds)} AEDs stored.  Use" \
                   f" {(await self.bot.get_valid_prefixes())[0]}aed list to see what they are.\n"
        else:
            data = f"No data is stored for user with ID {user_id}."
        return {"user_data.txt": BytesIO(data.encode())}

    async def red_delete_data_for_user(self, *, requester, user_id):
        """Delete a user's personal data."""
        await self.config.user_from_id(user_id).clear()

    def cog_unload(self):
        # Manually nulling out database because the GC for cogs seems to be pretty shitty
        self.events = set()
        self.started_events = set()
        self._event_loop.cancel()
        self._refresh_loop.cancel()
        self._daily_event_loop.cancel()

    async def reload_padevents(self) -> NoReturn:
        await self.bot.wait_until_ready()
        with suppress(asyncio.CancelledError):
            async for _ in repeating_timer(60 * 60):
                try:
                    await self.refresh_data()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Error in loop:")

    async def do_loop(self) -> NoReturn:
        await self.bot.wait_until_ready()
        with suppress(asyncio.CancelledError):
            async for _ in repeating_timer(10):
                try:
                    await self.do_autoevents()
                    await self.do_eventloop()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Error in loop:")

    async def show_daily_info(self) -> NoReturn:
        async def is_day_change():
            curserver = self.get_most_recent_day_change()
            oldserver = self.config.last_daychange
            if curserver != await oldserver():
                await oldserver.set(curserver)
                return curserver

        await self.bot.wait_until_ready()
        with suppress(asyncio.CancelledError):
            async for server in conditional_iterator(is_day_change, poll_interval=10):
                try:
                    await self.do_daily_post(server)
                    await self.do_autoevent_summary(server)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Error in loop:")

    async def refresh_data(self):
        dbcog: Any = self.bot.get_cog('DBCog')
        await dbcog.wait_until_ready()
        scheduled_events = dbcog.database.get_all_events()

        new_events = set()
        for se in scheduled_events:
            try:
                new_events.add(Event(se))
            except Exception as ex:
                logger.exception("Refresh error:")

        self.events = self.coalesce_event_data(new_events)
        self.started_events = {ev.key for ev in new_events if ev.is_started()}
        async with self.config.sent() as seen:
            for key, value in [*seen.items()]:
                if value < time.time() - 60 * 60:
                    del seen[key]

    async def do_eventloop(self):
        events = filter(lambda e: e.is_started() and e.key not in self.started_events, self.events)
        daily_refresh_servers = set()
        for event in events:
            self.started_events.add(event.key)
            if event.event_length != EventLength.limited:
                continue
            for cid, data in (await self.config.all_channels()).items():
                if (channel := self.bot.get_channel(cid)) is None \
                        or event.server not in data['guerrilla_servers']:
                    continue
                role_name = f'{event.server}_group_{event.group_long_name()}'
                role = channel.guild.get_role(role_name)
                if role and role.mentionable:
                    message = f"{role.mention} {event.clean_dungeon_name} is starting"
                else:
                    message = box(f"Server {event.server}, group {event.group_long_name()}:"
                                  f" {event.clean_dungeon_name}")
                with suppress(discord.Forbidden):
                    await channel.send(message, allowed_mentions=discord.AllowedMentions(roles=True))

    async def do_daily_post(self, server):
        msg = self.make_active_text(server)
        for cid, data in (await self.config.all_channels()).items():
            if (channel := self.bot.get_channel(cid)) is None \
                    or server not in data['daily_servers']:
                continue
            for page in pagify(msg, delims=['\n\n']):
                with suppress(discord.Forbidden):
                    await channel.send(box(page))

    async def do_autoevent_summary(self, server):
        events = EventList(self.events).with_server(server).today_only('NA')
        for gid, data in (await self.config.all_guilds()).items():
            if (guild := self.bot.get_guild(gid)) is None:
                continue
            channels = defaultdict(list)
            for key, aep in data.get('pingroles', {}).items():
                for channel in aep['channels']:
                    if channel is not None:
                        channels[channel].append(aep)
            for cid, aeps in channels.items():
                if (channel := self.bot.get_channel(cid)) is None:
                    continue
                if not await self.config.channel(channel).do_aep_post():
                    continue
                aepevents = events.with_func(lambda e: any(self.event_matches_autoevent(e, ae) for ae in aeps))
                if not aepevents:
                    continue
                msg = self.make_full_guerrilla_output('AEP Event', aepevents)
                for page in pagify(msg, delims=['\n\n']):
                    with suppress(discord.Forbidden):
                        await channel.send(box(page))

    @commands.group(aliases=['pde'])
    @checks.mod_or_permissions(manage_guild=True)
    async def padevents(self, ctx):
        """PAD event tracking"""

    @padevents.command()
    @checks.is_owner()
    async def testevent(self, ctx, server: Server, seconds: int = 0, group='red'):
        server = server.value
        if group.lower() not in ('red', 'blue', 'green'):
            group = None

        dbcog: Any = self.bot.get_cog('DBCog')
        await dbcog.wait_until_ready()
        # TODO: Don't use this awful importing hack
        dg_module = __import__('.'.join(dbcog.__module__.split('.')[:-1]) + ".models.scheduled_event_model")
        timestamp = int((datetime.datetime.now(pytz.utc) + timedelta(seconds=seconds)).timestamp())
        self.fake_uid -= 1

        te = dg_module.models.scheduled_event_model.ScheduledEventModel(
            event_id=self.fake_uid,
            server_id=SUPPORTED_SERVERS.index(server),
            event_type_id=-1,
            start_timestamp=timestamp,
            end_timestamp=timestamp + 60,
            group_name=group and group.lower(),
            dungeon_model=DummyObject(
                name_en='fake_dungeon_name',
                clean_name_en='fake_dungeon_name',
                dungeon_type=DungeonType.ThreePlayer,
                dungeon_id=1,
            )
        )
        self.events.add(Event(te))
        await ctx.tick()

    @padevents.command()
    @commands.guild_only()
    @checks.mod_or_permissions(manage_guild=True)
    async def addchannel(self, ctx, channel: Optional[discord.TextChannel], server: Server):
        server = server.value

        async with self.config.channel(channel or ctx.channel).guerrilla_servers() as guerillas:
            if server in guerillas:
                return await ctx.send("Channel already active.")
            guerillas.append(server)
        await ctx.send("Channel now active.")

    @padevents.command()
    @commands.guild_only()
    @checks.mod_or_permissions(manage_guild=True)
    async def rmchannel(self, ctx, channel: Optional[discord.TextChannel], server: Server):
        server = server.value

        async with self.config.channel(channel or ctx.channel).guerrilla_servers() as guerillas:
            if server not in guerillas:
                return await ctx.send("Channel already inactive.")
            guerillas.remove(server)
        await ctx.send("Channel now inactive.")

    @padevents.command()
    @commands.guild_only()
    @checks.mod_or_permissions(manage_guild=True)
    async def addchanneldaily(self, ctx, channel: Optional[discord.TextChannel], server: Server):
        server = server.value

        async with self.config.channel(channel or ctx.channel).daily_servers() as dailies:
            if server in dailies:
                return await ctx.send("Channel already active.")
            dailies.append(server)
        await ctx.send("Channel now active.")

    @padevents.command()
    @commands.guild_only()
    @checks.mod_or_permissions(manage_guild=True)
    async def rmchanneldaily(self, ctx, channel: Optional[discord.TextChannel], server: Server):
        server = server.value

        async with self.config.channel(channel or ctx.channel).daily_servers() as dailies:
            if server not in dailies:
                return await ctx.send("Channel already inactive.")
            dailies.remove(server)
        await ctx.send("Channel now inactive.")

    @padevents.command()
    @checks.mod_or_permissions(manage_guild=True)
    async def active(self, ctx, server: Server):
        server = server.value

        msg = self.make_active_text(server)
        for page in pagify(msg, delims=['\n\n']):
            await ctx.send(box(page))

    def make_active_text(self, server):
        server = normalize_server_name(server)

        server_events = EventList(self.events).with_server(server)
        active_events = server_events.active_only()
        events_today = server_events.today_only(server)

        active_special = active_events.with_dungeon_type(DungeonType.Special)

        msg = server + " Events - " + datetime.datetime.now(SERVER_TIMEZONES[server]).strftime('%A, %B %-e')

        ongoing_events = active_events.with_length(EventLength.weekly, EventLength.special)
        if ongoing_events:
            msg += "\n\n" + self.make_active_output('Ongoing Events', ongoing_events)

        active_dailies_events = active_events.with_length(EventLength.daily)
        if active_dailies_events:
            msg += "\n\n" + self.make_daily_output('Daily Dungeons', active_dailies_events)

        limited_events = events_today.with_length(EventLength.limited)
        if limited_events:
            msg += "\n\n" + self.make_full_guerrilla_output('Limited Events', limited_events)

        return msg

    def make_daily_output(self, table_name, event_list):
        tbl = prettytable.PrettyTable([table_name])
        tbl.hrules = prettytable.HEADER
        tbl.vrules = prettytable.NONE
        tbl.align[table_name] = "l"
        for e in event_list:
            tbl.add_row([e.clean_dungeon_name])
        return tbl.get_string()

    def make_active_output(self, table_name, event_list):
        tbl = prettytable.PrettyTable(["Time", table_name])
        tbl.hrules = prettytable.HEADER
        tbl.vrules = prettytable.NONE
        tbl.align[table_name] = "l"
        tbl.align["Time"] = "r"
        for e in event_list:
            tbl.add_row([e.end_from_now_full_min().strip(), e.clean_dungeon_name])
        return tbl.get_string()

    def make_active_guerrilla_output(self, table_name: str, event_list: EventList) -> str:
        tbl = prettytable.PrettyTable([table_name, "Group", "Time"])
        tbl.hrules = prettytable.HEADER
        tbl.vrules = prettytable.NONE
        tbl.align[table_name] = "l"
        tbl.align["Time"] = "r"
        for e in event_list:
            tbl.add_row([e.clean_dungeon_name, e.group, e.end_from_now_full_min().strip()])
        return tbl.get_string()

    def make_full_guerrilla_output(self, table_name, event_list):
        events_by_name = defaultdict(set)
        for event in event_list:
            events_by_name[event.clean_dungeon_name].add(event)

        rows = []
        for name, events in events_by_name.items():
            events = sorted(events, key=lambda e: e.open_datetime)

            events_by_group = {group: [] for group in GROUPS}
            for event in events:
                if event.group is not None:
                    events_by_group[event.group].append(event)
                else:
                    for group in GROUPS:
                        events_by_group[group].append(event)

            while True:
                row = []
                for group in GROUPS:
                    if len(events_by_group[group]) == 0:
                        row.append('')
                    else:
                        # Get the timestamp of the earliest event in this group in PST
                        start = events_by_group[group].pop(0).open_datetime.astimezone(pytz.timezone('US/Pacific'))
                        row.append(start.strftime("%H:%M"))

                if not any(row):
                    break

                if row[0] == row[1] == row[2]:
                    rows.append([name, row[0], '=', '='])
                else:
                    rows.append([name] + row)

        header = "Times are shown in Pacific Time\n= means same for all groups\n"
        table = prettytable.PrettyTable([table_name, 'Red', 'Blue', 'Green'])
        table.align[table_name] = "l"
        table.hrules = prettytable.HEADER
        table.vrules = prettytable.ALL
        for r in rows:
            table.add_row(r)

        return header + table.get_string() + "\n"

    @commands.command(aliases=['events'])
    async def eventsna(self, ctx, group: StarterGroup = None):
        """Display upcoming daily events for NA."""
        await self.do_partial(ctx, Server.NA, group)

    @commands.command()
    async def eventsjp(self, ctx, group: StarterGroup = None):
        """Display upcoming daily events for JP."""
        await self.do_partial(ctx, Server.JP, group)

    @commands.command()
    async def eventskr(self, ctx, group: StarterGroup = None):
        """Display upcoming daily events for KR."""
        await self.do_partial(ctx, Server.KR, group)

    async def do_partial(self, ctx, server: Server, group: StarterGroup = None):
        server = server.value

        if group is not None:
            group = GROUPS[group.value]

        events = EventList(self.events)
        events = events.with_server(server)
        events = events.with_dungeon_type(DungeonType.SoloSpecial, DungeonType.Special)
        events = events.with_length(EventLength.limited)

        active_events = sorted(events.active_only(), key=lambda e: (e.open_datetime, e.dungeon_name), reverse=True)
        pending_events = sorted(events.pending_only(), key=lambda e: (e.open_datetime, e.dungeon_name), reverse=True)

        if group is not None:
            active_events = [e for e in active_events if e.group == group.lower()]
            pending_events = [e for e in pending_events if e.group == group.lower()]

        group_to_active_event = {e.group: e for e in active_events}
        group_to_pending_event = {e.group: e for e in pending_events}

        active_events.sort(key=lambda e: (GROUPS.index(e.group or 'red'), e.open_datetime))
        pending_events.sort(key=lambda e: (GROUPS.index(e.group or 'red'), e.open_datetime))

        if len(active_events) == 0 and len(pending_events) == 0:
            await ctx.send("No events available for " + server)
            return

        output = "**Events for {}**".format(server)

        if len(active_events) > 0:
            output += "\n\n" + "`  Remaining Dungeon       - Ending Time`"
            for e in active_events:
                output += "\n" + e.to_partial_event(self)

        if len(pending_events) > 0:
            output += "\n\n" + "`  Dungeon                 - ETA`"
            for e in pending_events:
                output += "\n" + e.to_partial_event(self)

        for page in pagify(output):
            await ctx.send(page)

    def get_most_recent_day_change(self):
        now = datetime.datetime.utcnow().time()
        if now < datetime.time(8):
            return "JP"
        elif now < datetime.time(15):
            return "NA"
        elif now < datetime.time(16):
            return "KR"
        else:
            return "JP"

    def coalesce_event_data(self, events: Iterable[Event]) -> Set[Event]:
        all_events = set()

        grouped = defaultdict(lambda: {})
        for event in events:
            if event.group is None:
                all_events.add(event)
                continue
            key = (event.open_datetime, event.close_datetime, event.server, event.dungeon.dungeon_id)
            grouped[key][event.group] = event

        for _, grouped_events in grouped.items():
            if len(grouped_events) != 3:
                all_events.update(grouped_events.values())
                continue
            grouped_events['red'].group = None
            all_events.add(grouped_events['red'])

        return all_events
