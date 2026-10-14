"""Small explicit registry and bounded, process-local paging for room replies."""
from collections import OrderedDict
from dataclasses import dataclass
import time

from .clock import SessionClock
from .config import COMMANDS, Config, clean_text


MAX_RESPONSE_BYTES = 16384
LOCATION_COMMANDS = ('towns', 'water', 'distance', 'bearing', 'place', 'walktime',
                     'tides', 'sun', 'details')
LOCATION_DESCRIPTIONS = {
    'towns': 'Find nearby towns and localities, measured in a straight line. Example: /bot location towns Hamilton. Streets and coordinates work too.',
    'water': 'Find water near a street, suburb or coordinates. Example: /bot location water Tattersfield Place Hamilton. Drinking safety unknown.',
    'distance': 'Walking distance between two locations. Example: /bot location distance Hamilton to Raglan. Optional mode: road or straight.',
    'bearing': 'Magnetic compass bearing by default. /bot location bearing [compass|true|both] FROM to TO. Example: /bot location bearing both Hamilton to Raglan.',
    'place': 'Look up reference coordinates or a street ID. Example: /bot location place Tattersfield Place Hamilton.',
    'walktime': 'Rough walking hours from distance and assumed pace. Example: /bot location walktime 10 4. Excludes stops and terrain.',
    'tides': 'Saved tide predictions; requires a trusted date and coastal location. Example: /bot location tides Raglan.',
    'sun': 'Sunrise and sunset; requires a trusted date. Example: /bot location sun Hamilton.',
    'details': 'Extra information from your latest place, distance, bearing or water lookup. Example: /bot location details.',
}


def split_utf8(text, budget):
    if not isinstance(text, str) or '\0' in text:
        raise ValueError('Output must be text without NUL')
    chunks, current, used = [], [], 0
    for char in text:
        size = len(char.encode('utf-8'))
        if size > budget:
            raise ValueError('Reply prefix leaves no room for UTF-8 text')
        while used + size > budget:
            # Keep complete result lines together before falling back to words.
            boundary=next((i+1 for i in range(len(current)-1,-1,-1) if current[i]=='\n'),0)
            if not boundary:
                boundary=next((i+1 for i in range(len(current)-1,-1,-1) if current[i].isspace()),0)
            if boundary:
                chunks.append(''.join(current[:boundary]))
                current=current[boundary:]
                used=sum(len(c.encode('utf-8')) for c in current)
            else:
                chunks.append(''.join(current))
                current, used = [], 0
        current.append(char)
        used += size
    if current:
        chunks.append(''.join(current))
    return chunks


class Pager:
    def __init__(self, prefix, limits, monotonic=time.monotonic):
        clean_text(prefix, 'prefix', 100)
        self.budget = 151 - len(prefix.encode('utf-8'))
        if self.budget < 64:
            raise ValueError('Reply prefix must leave at least 64 bytes')
        self.limit, self.ttl = limits.paging_users, limits.page_ttl_seconds
        self.monotonic, self.pages = monotonic, OrderedDict()

    def cleanup(self):
        now = self.monotonic()
        for author, (expires, _) in list(self.pages.items()):
            if expires <= now:
                del self.pages[author]

    def start(self, author, result):
        self.cleanup()
        self.pages.pop(author, None)
        if result is None:
            return None
        values = [result] if isinstance(result, str) else result
        if not isinstance(values, (list, tuple)) or any(not isinstance(v, str) for v in values):
            raise ValueError('Commands must return None, text or a list of text records')
        if sum(len(v.encode('utf-8')) for v in values) > MAX_RESPONSE_BYTES:
            return ['Result too large; narrow your request.']
        chunks = [chunk for value in values for chunk in split_utf8(value, self.budget)]
        if not chunks:
            return None
        if len(chunks) <= 4:
            return chunks
        if len(self.pages) >= self.limit:
            self.pages.popitem(last=False)
        self.pages[author] = (self.monotonic() + self.ttl, chunks)
        return self.next(author)

    def next(self, author):
        self.cleanup()
        saved = self.pages.get(author)
        if saved is None:
            return ['No saved page. Repeat your original query.']
        _, chunks = saved
        count = 3 if len(chunks) > 4 else len(chunks)
        result, remaining = chunks[:count], chunks[count:]
        if remaining:
            self.pages[author] = (self.monotonic() + self.ttl, remaining)
            self.pages.move_to_end(author)
            result.append('More: /bot next (shared room reply)')
        else:
            del self.pages[author]
        return result


@dataclass(frozen=True)
class Context:
    post: object
    config: Config
    clock: SessionClock
    room_key: object = None


@dataclass(frozen=True)
class Command:
    usage: str
    callback: object
    costly: bool = True


class Dispatcher:
    def __init__(self, config=None, prefix='[Bot] ', clock=None, monotonic=time.monotonic):
        self.config = config or Config()
        self.clock = clock or SessionClock()
        self.pager = Pager(prefix, self.config.limits, monotonic)
        self.monotonic, self.commands = monotonic, OrderedDict()
        self.admissions, self.memo = OrderedDict(), OrderedDict()
        self.detail_records=OrderedDict()
        self.room_key = None
        self.register('ping', '/bot ping', lambda ctx, args: 'pong', False)
        self.register('help', '/bot help', lambda ctx, args: self.help(), False)
        self.register('about', '/bot about', lambda ctx, args:
                      'Squishyware room bot: Python over USB; replies use the room identity.', False)
        self.register('location', '/bot location help', lambda ctx, args: self.location_help(), False)

    def register(self, name, usage, callback, costly=True):
        if name not in COMMANDS or name in self.commands or not callable(callback):
            raise ValueError('Command name must be supported, unique and have a callback')
        clean_text(usage, 'usage')
        self.commands[name] = Command(usage, callback, costly)

    def bind_room(self,key):
        if self.room_key is not None and self.room_key!=key:
            raise ValueError('Dispatcher cannot silently change room identity')
        self.room_key=key

    def remember_details(self,author,details):
        self.detail_records.pop(author,None)
        if not details:return
        if sum(len(t.encode('utf-8')) for t in details)>MAX_RESPONSE_BYTES:
            raise ValueError('Lookup details exceed bounded storage')
        if len(self.detail_records)>=self.config.limits.paging_users:
            self.detail_records.popitem(last=False)
        self.detail_records[author]=(self.monotonic()+self.config.limits.page_ttl_seconds,tuple(details))

    def details(self,author):
        saved=self.detail_records.get(author)
        if saved is None or saved[0]<=self.monotonic():
            self.detail_records.pop(author,None)
            return 'No saved details. Repeat your location or water lookup first.'
        return list(saved[1])

    def enabled(self, name):
        enabled = self.config.enabled_commands
        if name in enabled:
            return True
        if name in LOCATION_COMMANDS and 'location' in enabled:
            return True
        # Old per-feature configurations continue to work during migration.
        return ((name == 'towns' and 'nearby' in enabled) or
                (name == 'nearby' and ('towns' in enabled or 'location' in enabled)))

    def location_names(self):
        return [name for name in LOCATION_COMMANDS
                if name in self.commands and self.enabled(name)]

    def location_help(self, topic=None):
        available = self.location_names()
        if topic is not None:
            topic = topic.casefold()
            if topic not in available:
                return 'Unknown or disabled location command. Try /bot location help.'
            return LOCATION_DESCRIPTIONS[topic]
        if not available:
            return 'No location commands enabled.'
        example = 'towns' if 'towns' in available else available[0]
        return ('Location commands: ' + ', '.join(available) +
                f'. Help: /bot location {example} help.')

    def help(self):
        usages = []
        for name, cmd in self.commands.items():
            if name == 'location':
                if self.location_names():
                    usages.append(cmd.usage)
            elif name not in LOCATION_COMMANDS and name != 'nearby' and self.enabled(name):
                usages.append(cmd.usage)
        return 'Commands: ' + ' | '.join(usages) if usages else 'No commands enabled.'

    def __call__(self, post):
        words = post.text.split()
        if not words or words[0].casefold() != '/bot':
            return None
        self.pager.cleanup()
        now = self.monotonic()
        ttl = self.config.limits.page_ttl_seconds
        for mapping in (self.admissions, self.memo,self.detail_records):
            for key, entry in list(mapping.items()):
                if entry[0] <= now:
                    del mapping[key]
        request = (post.author, post.sequence, post.text)
        if request in self.memo:
            saved = self.memo[request][1]
            return list(saved) if saved is not None else None
        verb = words[1].casefold() if len(words) > 1 else 'help'
        args = words[2:]
        if verb == 'next':
            if args:
                return ['Usage: /bot next']
            callback, costly = lambda: self.pager.next(post.author), True
            already_formatted = True
        else:
            context = Context(post, self.config, self.clock, self.room_key)
            if verb == 'location' or (verb == 'help' and [a.casefold() for a in args] == ['location']):
                if not self.location_names():
                    return ['Location commands are disabled. Try /bot help.']
                if verb == 'help':
                    args = []
                if not args or args[0].casefold() == 'help':
                    if len(args) > 2:
                        return ['Usage: /bot location help [COMMAND]']
                    topic = args[1] if len(args) == 2 else None
                    callback, costly = lambda: self.location_help(topic), False
                elif len(args) == 2 and args[1].casefold() == 'help':
                    callback, costly = lambda: self.location_help(args[0]), False
                else:
                    subcommand = args[0].casefold()
                    if subcommand == 'nearby':
                        return ['Nearby is now towns. Use /bot location towns LOCATION.']
                    if subcommand not in self.location_names():
                        return ['Unknown or disabled location command. Try /bot location help.']
                    cmd = self.commands[subcommand]
                    args = args[1:]
                    callback, costly = lambda: cmd.callback(context, args), cmd.costly
            else:
                cmd = self.commands.get(verb)
                if cmd is None or not self.enabled(verb):
                    return ['Unknown or disabled bot command. Try /bot help.']
                if verb in ('ping', 'help', 'about') and args:
                    return ['Usage: /bot ping | /bot help | /bot about']
                callback, costly = lambda: cmd.callback(context, args), cmd.costly
            already_formatted = False
        cooldown = self.config.limits.request_cooldown_seconds
        previous = self.admissions.get(post.author)
        if costly and previous and previous[1] + cooldown > now:
            return ['Please wait before another lookup.']
        result = callback()
        replies = result if already_formatted else self.pager.start(post.author, result)
        if costly:
            if len(self.admissions) >= self.config.limits.paging_users and post.author not in self.admissions:
                self.admissions.popitem(last=False)
            self.admissions[post.author] = (now + ttl, now)
            self.admissions.move_to_end(post.author)
        if len(self.memo) >= self.config.limits.paging_users * 4:
            self.memo.popitem(last=False)
        self.memo[request] = (now + ttl, tuple(replies) if replies is not None else None)
        return replies
