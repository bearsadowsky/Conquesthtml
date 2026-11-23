"""Conquest multiplayer server (WebSocket version, works on Replit).
Run: python conquest_server.py
Env: PORT (default 8080)
GUN GAME: players click Play (message 'ready'). When two are ready a 10 second countdown
starts (nobody can leave). Every kill on another player moves the killer up the ladder
(CHARACTERS order) with full health and a random respawn within 10 tiles of the spawn point. A kill made
while playing the last character (Thanos) wins: everyone is told, then kicked after 5 seconds.
FREE PLAY: a second, separate room. A player who clicks Free Play (message 'free' with a character
kind) can be any character and switch whenever they like ('fkind'). Free players only see and hurt other
free players, never respawn into the ladder, and have their own kills/deaths table (message 'fp').
Homer Simpson is on the gun game ladder, right before Spider-Man; Mr. Potato Head is on it too, right before Vader (Vader is second to last, Thanos is last). His giant form has 5x health, which the
server tracks (message flag 'bg' in the state message), and his choke is relayed ('choke' / 'choked' / 'chokejump').
Gun game and free play share the server and the map, but nothing else.
The server is authoritative for player HP, kills/deaths and the 8 target dummies.
Clients send their position ~20x/sec and report hits; the server applies
the damage and broadcasts the results.
"""
import asyncio
import json
import math
import os
import random
import time
from http import HTTPStatus

MAX_PLAYERS = 5
CHARACTERS = ('spongebob', 'jack', 'wick', 'homer', 'spiderman', 'koolaid', 'ironman', 'potato', 'warden', 'thanos')
FX_KINDS = ('spray', 'ship', 'shot', 'point', 'med', 'whistle',
            'straw', 'webshot', 'wlstart', 'wlgo', 'webt', 'slam', 'fpull', 'repulsor', 'repcharge', 'unistart',
            'donut', 'eat', 'power', 'kill', 'rocket', 'rhit', 'rdir', 'rwave',
            'tcast', 'tpow', 'tpowx', 'tspace', 'ttime', 'tmind', 'treal', 'tsoul', 'tduck')  # ability visuals we relay (donut / eat / power are Homer's; rocket / rhit / rdir are Mr. Potato Head's guided rocket arm)
FREE_CHARACTERS = CHARACTERS               # free play lets you be anyone on the ladder (Homer sits right before Spider-Man)
TARGET_COUNT = 8
MAX_HP = 100.0
THANOS_HP_MULT = 2.5          # Thanos: 2.5x the health (an average player 10 hp, him 25)
GIANT_HP_MULT = 5.0           # Homer's giant form: 5x health (matches the client's HP.health)
GIANT_BLOCK = 1.0             # seconds after a death during which a stale 'giant' flag is ignored
CHOKE_MAX = 3.5               # a choke lasts 3 seconds on the client; the server allows a little slack
TARGET_REVIVE_DELAY = 3.0
SNAP_INTERVAL = 0.05          # 20 snapshots per second
WEB_TIME = 5.0                # how long a webbed player stays stuck
WEB_DAMAGE_TOTAL = 100 / 3 * 1.7    # electric damage over that time
WEB_TICKS = 10
# Thanos's Infinity Stones: the server keeps its own cooldown floor for the ones that touch other players or health
THANOS_COOLDOWN = {'mind': 20.0, 'reality': 22.0, 'time': 19.0, 'soul': 28.0}   # just under the client's cooldowns (mind 22, reality 25, time 20, soul 30 s), since a hit can reach the server a couple of seconds after the cast
MIND_TIME = 5.0               # Mind Stone: the victim's W/S and A/D are swapped this long, and they are immune to its damage this long
MIND_WINDOW = 3.5             # a Mind Stone beam lasts 2 s: it may keep hitting new players for this long after its first hit
REALITY_TIME = 5.0            # Reality Stone: the victim is a harmless rubber duck this long
SOUL_LOCK = 1.7               # Soul Stone: how long the vines hold someone before the flash (the client's glow lasts the same)
SOUL_FRAC = 0.25              # the flash takes this fraction of the victim's health BAR and gives Thanos this fraction of his
SOUL_MAX_VICTIMS = 4          # most players one cast of the stone can grab
HEAL_MAX = 30.0 * THANOS_HP_MULT               # Soul Stone: the most health one 'heal' message may give...
HEAL_WINDOW = 3.0             # ...and the most (HEAL_WINDOW_MAX) over any HEAL_WINDOW seconds
HEAL_WINDOW_MAX = 60.0 * THANOS_HP_MULT
MAX_DAMAGE = 500.0            # sanity clamp on a single reported hit
LAST_LEVEL = len(CHARACTERS) - 1   # Thanos: a kill with him wins the game
STALE_CAUSE_TIME = 6.0        # seconds after a level-up during which more kills from the same ability don't level you up again
COUNTDOWN = 10                # seconds between the 2nd player clicking Play and the start
WIN_KICK_DELAY = 5            # seconds the winner text shows before everyone is kicked
SPAWN_RADIUS = 10.0           # after a kill you reappear within this many tiles of the spawn point
SPAWN_MIN = 0.0
MAP_LIMIT = 280.0
# what the victim's name is followed by when someone dies, by the cause the attacker's client reports
DEATH_TEXT = {
    'burger': 'was a burger', 'bubble': 'popped', 'sword': 'was chopped', 'ship': 'went overboard',
    'pistol': 'was shot', 'dog': 'was bitten', 'launch': 'got mauled', 'web': 'was electrified',
    'slam': 'was slammed', 'straw': 'got spit on', 'saber': 'was sliced and diced',
    'repulsor': 'was blasted', 'unibeam': 'was vaporized',
    'bonk': 'was bonked', 'donut': 'got donut-ed', 'choke': 'was strangled',
    'spud': 'was mashed', 'rocket': 'was blown up by a rocket arm', 'roll': 'was flattened by a barrel roll', 'rollwave': 'was shaken by a shockwave',
    'hunted': 'was hunted to death',   # killing John Wick's dog
    'uppercut': 'was launched into orbit',   # Thanos's titan uppercut
    'power': 'was disintegrated',            # Thanos: Power Stone blast
    'space': 'got crushed by a portal',      # Thanos: Space Stone portal step
    'soul': 'had their soul drained',        # Thanos: Soul Stone
    'mind': 'had their mind blown',          # Thanos: Mind Stone beam
    'reality': 'was warped by reality',      # Thanos: Reality Stone beam
}
DEFAULT_DEATH = 'was killed'
SPAWN_GHOST = 2.0             # seconds of ghost form (untouchable, see-through) after you die or get a kill and respawn
SUICIDE_HP = 1.0              # gun game: killing yourself (e.g. your own burger) respawns you with a sliver of health, not a full bar


class Player:
    def __init__(self, pid, name, conn):
        self.id = pid
        self.name = name
        self.conn = conn
        self.hp = MAX_HP
        self.max_hp = MAX_HP      # 5x while Homer is giant
        self.giant = False
        self.no_giant_until = 0.0
        self.ghost_until = 0.0    # spawn protection: ghost form until this time
        self.choked_by = None     # (homer id, deadline) while Homer is choking them
        self.kind = 'spongebob'   # which character they picked
        self.ghost = False        # True while they are in the menu: untouchable
        self.skeleton = False     # True while Jack is in skeleton form: takes no damage
        self.ready = False        # clicked Play: part of the gun game
        self.free = False         # clicked Free Play: part of the free play room
        self.fkills = 0           # free play tally (kept apart from the gun game's)
        self.fdeaths = 0
        self.level = 0            # index into CHARACTERS (the gun game ladder)
        self.stale = {}           # cause -> time until which kills by that cause no longer level you up (the old character's leftover shockwave etc.)
        self.kills = 0
        self.deaths = 0
        self.web_task = None      # running web-electricity timer
        self.tcd = {}             # Thanos: stone ability -> time it may be used again
        self.mind_until = 0.0     # Mind Stone victim until...
        self.reality_until = 0.0  # Reality Stone victim (a harmless duck) until...
        self.heal_log = []        # Thanos: recent (time, amount) Soul Stone heals
        self.soul_n = -1          # Thanos: which cast of the Soul Stone is running, how many it has grabbed and when it began
        self.soul_cnt = 0
        self.soul_t0 = 0.0
        self.soul_until = 0.0     # Soul Stone victim: held until...
        self.mind_n = -1          # Thanos: which beam of the Mind Stone is running, how many it has hit and when it began
        self.mind_cnt = 0
        self.mind_t0 = 0.0
        self.mind_imm_until = 0.0  # hit by the Mind Stone beam: immune to its damage until...
        self.extra = {}           # weapon in hand, skeleton form, dog position, ghost flag
        # x, y, z, ry, weapon, punch_t, pitch (hp is appended when sending)
        self.state = [(pid - 3) * 2.0, 0.0, 0.0, 0.0, 0, -1, 0.0]

    @property
    def kind(self):
        return self._kind

    @kind.setter
    def kind(self, value):
        changed = getattr(self, '_kind', None) != value
        self._kind = value
        if changed and not self.giant:                # becoming another character: that character's full health
            self.max_hp = self.base_hp()
            self.hp = self.max_hp

    def base_hp(self):
        """Full health of the character in use (Thanos has more)."""
        return MAX_HP * (THANOS_HP_MULT if self._kind == 'thanos' else 1.0)

    def refill(self):
        """Full normal health (death, respawn, level up, menu): giant form and any choke end."""
        self.giant = False
        self.max_hp = self.base_hp()
        self.hp = self.max_hp
        self.choked_by = None
        self.mind_until = 0.0
        self.reality_until = 0.0
        self.no_giant_until = time.monotonic() + GIANT_BLOCK

    def set_giant(self, on):
        """Homer grows / shrinks: keep the same fraction of the health bar, like the client does."""
        if on == self.giant:
            return
        frac = self.hp / self.max_hp if self.max_hp else 0.0
        self.giant = on
        self.max_hp = self.base_hp() * GIANT_HP_MULT if on else self.base_hp()
        self.hp = max(1.0, frac * self.max_hp) if self.hp > 0 else 0.0

    def protect(self):
        """Ghost form for a moment after respawning, so nothing left over from the fight (a shockwave...) can hit you."""
        self.ghost_until = time.monotonic() + SPAWN_GHOST

    def protected(self):
        return time.monotonic() < self.ghost_until

    def ducked(self):
        """A Reality Stone rubber duck: can't do anything and nothing can hurt it."""
        return time.monotonic() < self.reality_until

    def room(self):
        """'gun', 'free', or None while they are in the menu."""
        return 'gun' if self.ready else 'free' if self.free else None


class Game:
    def __init__(self):
        self.seed = random.randrange(1, 231)
        self.players = {}
        self.target_pos = []
        for i in range(TARGET_COUNT):
            ang = i / TARGET_COUNT * math.tau + random.uniform(-0.2, 0.2)
            r = random.uniform(18, 45)
            self.target_pos.append([round(math.cos(ang) * r, 2), round(math.sin(ang) * r, 2)])
        self.target_hp = [100.0] * TARGET_COUNT
        self.phase = 'lobby'      # lobby -> countdown -> playing -> ended -> lobby
        self.cd_left = 0
        self.cd_task = None

    # ---------------------------------------------------------- sending
    async def _send(self, player, text):
        try:
            await player.conn.send(text)
        except Exception:
            pass  # that connection is closing; its handler will clean up

    async def broadcast(self, obj, exclude=None):
        text = json.dumps(obj, separators=(',', ':'))
        targets = [p for p in list(self.players.values()) if p is not exclude]
        if targets:
            await asyncio.gather(*(self._send(p, text) for p in targets))

    async def broadcast_room(self, room, obj, exclude=None):
        """Send only to the players who are in that room (gun / free)."""
        if room is None:
            return
        text = json.dumps(obj, separators=(',', ':'))
        targets = [p for p in list(self.players.values()) if p is not exclude and p.room() == room]
        if targets:
            await asyncio.gather(*(self._send(p, text) for p in targets))

    # ---------------------------------------------------------- joining
    def free_id(self):
        for pid in range(1, MAX_PLAYERS + 1):
            if pid not in self.players:
                return pid
        return None

    async def add_player(self, conn, name):
        """Returns the Player, or None if the server is full."""
        pid = self.free_id()
        if pid is None:
            await conn.send(json.dumps({'t': 'full'}))
            return None
        name = (str(name).strip() or f'Player {pid}')[:16]
        player = Player(pid, name, conn)
        welcome = {
            't': 'welcome',
            'id': pid,
            'seed': self.seed,
            'players': [{'id': p.id, 'name': p.name} for p in self.players.values()],
            'stats': {str(p.id): [p.kills, p.deaths] for p in self.players.values()},
            'thp': self.target_hp,
            'targets': self.target_pos,
        }
        await conn.send(json.dumps(welcome, separators=(',', ':')))
        self.players[pid] = player
        await self.broadcast({'t': 'join', 'id': pid, 'name': name}, exclude=player)
        print(f'+ {name} joined as player {pid} ({len(self.players)} online)')
        return player

    async def release_chokes(self, homer):
        """Homer is gone / left the room / stopped being Homer: free everyone he was holding."""
        for v in list(self.players.values()):
            if v.choked_by and v.choked_by[0] == homer.id:
                v.choked_by = None
                await self.broadcast_room(v.room(), {'t': 'choked', 'id': v.id, 'by': homer.id, 'on': 0})

    async def remove_player(self, player):
        if self.players.get(player.id) is player:
            was_ready = player.ready
            was_free = player.free
            player.ready = False
            player.free = False
            del self.players[player.id]
            await self.release_chokes(player)
            if player.web_task:
                player.web_task.cancel()
            await self.broadcast({'t': 'leave', 'id': player.id})
            print(f'- {player.name} left ({len(self.players)} online)')
            if was_ready:
                await self.after_leave()
            if was_free:
                await self.send_fp()

    # ---------------------------------------------------------- messages
    async def handle(self, player, m):
        t = m.get('t')
        if t == 's':
            try:
                player.state = [float(m['x']), float(m['y']), float(m['z']), float(m['ry']),
                                int(m['w']), float(m['pt']), float(m['pp'])]
                # (the character is decided by the gun game level, not by the client)
                extra = {}
                wp = m.get('wp')
                if isinstance(wp, str) and len(wp) <= 16:
                    extra['wp'] = wp
                if m.get('sk'):
                    extra['sk'] = 1
                player.ghost = bool(m.get('gh'))
                player.skeleton = bool(m.get('sk'))   # the client reports skeleton form; the server must ignore damage too
                want_giant = (bool(m.get('bg')) and player.kind == 'homer' and not player.ghost
                              and time.monotonic() >= player.no_giant_until)
                player.set_giant(want_giant)
                if player.giant:
                    extra['bg'] = 1
                if player.ghost:
                    extra['gh'] = 1
                    player.refill()             # ghosts are always topped up
                if m.get('wl') in (1, 2, 3):
                    extra['wl'] = m['wl']
                if m.get('rl') in (1, 2, 3) and player.kind == 'potato':   # Mr. Potato Head's barrel roll stage, relayed so everyone sees it
                    extra['rl'] = m['rl']
                dg = m.get('dg')
                if isinstance(dg, list) and len(dg) == 3:
                    extra['dg'] = [float(dg[0]), float(dg[1]), float(dg[2])]
                ck = m.get('ck')                       # Homer's choke pose, relayed so everyone sees it
                if isinstance(ck, list) and len(ck) == 4 and player.kind == 'homer':
                    extra['ck'] = [float(v) for v in ck]
                player.extra = extra
            except (KeyError, TypeError, ValueError):
                pass
        elif t == 'ready':
            await self.set_ready(player)
        elif t == 'free':
            await self.set_free(player, m.get('kind'))
        elif t == 'fkind':
            if player.free:
                await self.set_free(player, m.get('kind'))
        elif t == 'unready':
            if player.free:
                await self.leave_free(player)
            else:
                await self.set_unready(player)
        elif t == 'reset':
            # the player just left character select: full health, no web
            player.refill()
            if player.web_task:
                player.web_task.cancel()
                player.web_task = None
                await self.broadcast_room(player.room(), {'t': 'unwebbed', 'id': player.id})
        elif t == 'fx':
            if m.get('k') in FX_KINDS:
                out = {'t': 'fx', 'id': player.id, 'k': m['k']}
                for key in ('p', 'd', 'e', 'v', 'i'):
                    if key in m:
                        out[key] = m[key]
                await self.broadcast_room(player.room(), out, exclude=player)
        elif t == 'fire':
            await self.broadcast_room(player.room(),
                                      {'t': 'fire', 'b': m.get('b'), 'p': m.get('p'), 'v': m.get('v')},
                                      exclude=player)
        elif t == 'explode':
            await self.broadcast_room(player.room(),
                                      {'t': 'explode', 'b': m.get('b'), 'p': m.get('p')}, exclude=player)
            await self.apply_hits(player, m.get('h'))
        elif t == 'punch':
            await self.apply_hits(player, m.get('h'))
        elif t == 'choke':
            # Homer grabs (on=1) or lets go of (on=0) a player: tell the victim so their client can pin them
            victim = self.players.get(m.get('id'))
            room = player.room()
            now = time.monotonic()
            if (victim is None or victim is player or victim.ghost or victim.protected() or room is None
                    or victim.room() != room or player.kind != 'homer' or victim.ducked() or player.ducked()):
                return
            if m.get('on'):
                if victim.choked_by and now < victim.choked_by[1]:
                    return                        # already held
                victim.choked_by = (player.id, now + CHOKE_MAX)
            elif victim.choked_by and victim.choked_by[0] == player.id:
                victim.choked_by = None
            else:
                return
            await self.broadcast_room(room, {'t': 'choked', 'id': victim.id, 'by': player.id,
                                             'on': 1 if m.get('on') else 0})
        elif t == 'chokejump':
            # the victim pressed Jump: forward it to Homer, whose client judges the timing window
            cb = player.choked_by
            if cb and time.monotonic() < cb[1]:
                homer = self.players.get(cb[0])
                if homer is not None:
                    await self.send_to([homer], {'t': 'chokejump', 'id': player.id})
        elif t == 'mind' or t == 'reality':
            await self.thanos_status(player, t, m.get('id'), m.get('n'))
        elif t == 'soul':
            await self.thanos_soul(player, m.get('id'), m.get('n'))
        elif t == 'heal':
            self.thanos_heal(player, m.get('a'))
        elif t == 'rewind':
            self.thanos_rewind(player, m.get('hp'))
        elif t == 'web':
            victim = self.players.get(m.get('id'))
            room = player.room()
            if (victim is not None and victim is not player and not victim.ghost and not victim.protected()
                    and victim.web_task is None and room is not None
                    and victim.room() == room
                    and not (room == 'gun' and self.phase != 'playing')   # no webbing in the lobby / countdown / after a win
                    and not victim.skeleton                                # skeletons are untouchable
                    and not victim.ducked() and not player.ducked()):      # and so are rubber ducks
                victim.web_task = asyncio.ensure_future(self.run_web(player, victim))

    def thanos_ready(self, player, key):
        """A Thanos stone ability: only Thanos, only in a live game, and not faster than the server's cooldown floor."""
        room = player.room()
        now = time.monotonic()
        if player.kind != 'thanos' or player.ghost or player.ducked() or room is None:
            return False
        if room == 'gun' and self.phase != 'playing':
            return False
        if now < player.tcd.get(key, 0.0):
            return False
        player.tcd[key] = now + THANOS_COOLDOWN[key]
        return True

    async def thanos_status(self, player, kind, vid, n=None):
        """Mind Stone (reversed controls) / Reality Stone (rubber duck): tell everyone so the victim's client applies it."""
        victim = self.players.get(vid)
        room = player.room()
        if (victim is None or victim is player or victim.ghost or victim.protected() or room is None
                or victim.room() != room or victim.skeleton or victim.ducked()):
            return
        now = time.monotonic()
        if kind == 'mind':
            if not isinstance(n, int) or now < victim.mind_until:
                return
            if n != player.mind_n:                              # a new beam: it needs the stone to be ready
                if not self.thanos_ready(player, 'mind'):
                    return
                player.mind_n, player.mind_cnt, player.mind_t0 = n, 0, now
            elif now - player.mind_t0 > MIND_WINDOW or player.mind_cnt >= SOUL_MAX_VICTIMS:
                return
            player.mind_cnt += 1                                # one beam may catch several players
            victim.mind_until = now + MIND_TIME
            await self.broadcast_room(room, {'t': 'minded', 'id': victim.id, 'dur': MIND_TIME})
            return
        if not self.thanos_ready(player, kind):
            return
        victim.reality_until = now + REALITY_TIME
        await self.broadcast_room(room, {'t': 'realityd', 'id': victim.id, 'dur': REALITY_TIME})

    async def thanos_soul(self, player, vid, n):
        """Soul Stone: Thanos's vines have reached a player. Hold them, and after SOUL_LOCK seconds the flash takes
        a quarter of their health bar and gives Thanos a quarter of his. One cast may grab a few players."""
        victim = self.players.get(vid)
        room = player.room()
        now = time.monotonic()
        if (victim is None or victim is player or victim.ghost or victim.protected() or room is None
                or victim.room() != room or victim.skeleton or victim.ducked() or now < victim.soul_until):
            return
        if not isinstance(n, int):
            return
        if n != player.soul_n:                                  # a new cast: it needs the stone to be ready
            if not self.thanos_ready(player, 'soul'):
                return
            player.soul_n, player.soul_cnt, player.soul_t0 = n, 0, now
        elif now - player.soul_t0 > 4.0 or player.soul_cnt >= SOUL_MAX_VICTIMS:
            return
        player.soul_cnt += 1
        victim.soul_until = now + SOUL_LOCK + 0.5
        await self.broadcast_room(room, {'t': 'souled', 'id': victim.id, 'by': player.id, 'dur': SOUL_LOCK})
        asyncio.ensure_future(self.run_soul(player, victim))

    async def run_soul(self, thanos, victim):
        await asyncio.sleep(SOUL_LOCK)
        room = thanos.room()
        if (self.players.get(victim.id) is not victim or self.players.get(thanos.id) is not thanos
                or room is None or victim.room() != room or victim.protected() or victim.skeleton
                or thanos.kind != 'thanos'):
            return                                              # someone left, died or changed: the flash is off
        await self.broadcast_room(room, {'t': 'soulflash', 'id': victim.id, 'by': thanos.id})
        await self.hit_player(thanos, victim.id, victim.max_hp * SOUL_FRAC, 'soul')
        if self.players.get(thanos.id) is thanos and thanos.kind == 'thanos' and not thanos.protected():
            thanos.hp = min(thanos.max_hp, thanos.hp + thanos.max_hp * SOUL_FRAC)

    def thanos_heal(self, player, amount):
        """Soul Stone: Thanos takes some health back (capped per message and per few seconds)."""
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return
        now = time.monotonic()
        if player.kind != 'thanos' or player.ghost or player.room() is None:
            return
        player.heal_log = [(t, a) for t, a in player.heal_log if now - t < HEAL_WINDOW]
        used = sum(a for _, a in player.heal_log)
        amount = max(0.0, min(amount, HEAL_MAX, HEAL_WINDOW_MAX - used))
        if amount <= 0:
            return
        player.heal_log.append((now, amount))
        player.hp = min(player.max_hp, player.hp + amount)

    def thanos_rewind(self, player, hp):
        """Time Stone: Thanos goes back to the health he had a few seconds ago (never lower than he has now)."""
        try:
            hp = float(hp)
        except (TypeError, ValueError):
            return
        if not self.thanos_ready(player, 'time'):
            return
        player.hp = min(player.max_hp, max(player.hp, hp))

    async def run_web(self, attacker, victim):
        """Pin a player for WEB_TIME seconds and shock them a little every tick."""
        try:
            room = victim.room()
            await self.broadcast_room(room, {'t': 'webbed', 'id': victim.id, 'dur': WEB_TIME})
            step = WEB_TIME / WEB_TICKS
            for _ in range(WEB_TICKS):
                await asyncio.sleep(step)
                if self.players.get(victim.id) is not victim:
                    return
                await self.hit_player(attacker, victim.id, WEB_DAMAGE_TOTAL / WEB_TICKS, 'web')
            await self.broadcast_room(room, {'t': 'unwebbed', 'id': victim.id})
        finally:
            victim.web_task = None

    async def apply_hits(self, attacker, hits):
        if not isinstance(hits, list):
            return
        for h in hits:
            try:
                kind = h[0]
                if kind == 't':
                    if attacker.room() != 'free':
                        continue                      # dummies only exist in Free Play, never in the gun game
                    await self.hit_target(int(h[1]), float(h[2]), float(h[3]), float(h[4]))
                elif kind == 'p':
                    await self.hit_player(attacker, int(h[1]), float(h[2]), h[3] if len(h) > 3 else None)
            except (IndexError, TypeError, ValueError):
                continue

    async def hit_target(self, i, amount, kx, kz):
        if not 0 <= i < TARGET_COUNT or self.target_hp[i] <= 0:
            return
        amount = max(0.0, min(amount, MAX_DAMAGE))
        self.target_hp[i] = max(0.0, self.target_hp[i] - amount)
        await self.broadcast({'t': 'thit', 'i': i, 'hp': round(self.target_hp[i], 1),
                              'kx': kx, 'kz': kz})
        if self.target_hp[i] <= 0:
            asyncio.get_running_loop().call_later(
                TARGET_REVIVE_DELAY, lambda: asyncio.ensure_future(self.revive_target(i)))

    async def revive_target(self, i):
        self.target_hp[i] = 100.0
        await self.broadcast({'t': 'trevive', 'i': i})

    async def hit_player(self, attacker, victim_id, amount, cause=None):
        victim = self.players.get(victim_id)
        if victim is None or victim.ghost or victim.protected():   # ghosts (in the menu, or just respawned) can't be hurt
            return
        if victim.ducked():
            return                                # a Reality Stone duck can't take damage from anything
        if victim.skeleton and cause != 'hunted':  # skeletons can't be hurt, except by the dog's curse
            return
        if attacker.reality_until > time.monotonic() and victim is not attacker:
            return                                # a Reality Stone duck is harmless
        room = attacker.room()
        if room is None or victim.room() != room:
            return                                # you can only hurt people in your own room
        if room == 'gun' and self.phase != 'playing':
            return                                # no damage in the lobby, countdown or after a win
        if cause == 'mind':                       # the Mind Stone beam hurts once, then that player is immune to it for a while
            now = time.monotonic()
            if now < victim.mind_imm_until:
                return
            victim.mind_imm_until = now + MIND_TIME
        amount = max(0.0, min(amount, MAX_DAMAGE))
        victim.hp -= amount
        if victim.hp <= 0:
            victim.refill()
            victim.protect()                      # 2 seconds of ghost form after dying
            sliver = victim is attacker and room == 'gun'
            if sliver:
                victim.hp = SUICIDE_HP            # no suiciding to get a full health bar back
            if room == 'gun':
                victim.deaths += 1
            else:
                victim.fdeaths += 1
            verb = DEATH_TEXT.get(cause, DEFAULT_DEATH) if isinstance(cause, str) else DEFAULT_DEATH
            text = f'{victim.name} {verb}'
            if victim is attacker:
                killer = 0                        # 0 = nobody gets the kill
            else:
                if room == 'gun':
                    attacker.kills += 1
                else:
                    attacker.fkills += 1
                killer = attacker.id
            died = {'t': 'died', 'id': victim.id, 'killer': killer, 'text': text}
            if sliver:
                died['hp'] = SUICIDE_HP           # the client respawns with this much instead of a full bar
            await self.broadcast_room(room, died)
            if room == 'free':
                await self.send_fp()
            elif victim is not attacker and self.phase == 'playing':
                now = time.monotonic()
                if isinstance(cause, str) and attacker.stale.get(cause, 0) > now:
                    pass                          # the same shockwave / blast that already levelled you up: only ONE level per ability use
                else:
                    await self.level_up(attacker, cause)

    # ---------------------------------------------------------- free play
    def free_players(self):
        return [p for p in self.players.values() if p.free]

    async def send_fp(self):
        fp = self.free_players()
        rows = [[p.id, p.fkills, p.fdeaths, p.kind] for p in fp]
        await self.send_to(fp, {'t': 'fp', 'rows': rows})

    async def set_free(self, player, kind):
        if player.ready:
            return                                # in the gun game: leave it first
        if kind not in FREE_CHARACTERS:
            kind = 'spongebob'
        if player.kind == 'homer' and kind != 'homer':
            player.set_giant(False)               # switching away from Homer ends the giant form
            await self.release_chokes(player)
        if not player.free:
            player.free = True
            player.refill()
            print(f'{player.name} joined free play ({len(self.free_players())} there)')
        player.kind = kind
        await self.send_fp()

    async def leave_free(self, player):
        if not player.free:
            return
        await self.release_chokes(player)
        if player.web_task:
            player.web_task.cancel()
            player.web_task = None
            await self.broadcast_room('free', {'t': 'unwebbed', 'id': player.id})
        player.free = False
        player.kind = 'spongebob'
        player.refill()
        await self.send_fp()

    # ---------------------------------------------------------- gun game
    def active(self):
        return [p for p in self.players.values() if p.ready]

    async def send_to(self, players, obj):
        text = json.dumps(obj, separators=(',', ':'))
        if players:
            await asyncio.gather(*(self._send(p, text) for p in players))

    async def send_gg(self):
        act = self.active()
        await self.send_to(act, {'t': 'gg', 'phase': self.phase, 'left': self.cd_left,
                                 'ready': len(act), 'lv': {str(p.id): p.level for p in act}})

    async def set_ready(self, player):
        if player.ready or player.free:
            return
        if self.phase == 'ended':                 # a game just finished: wait for the reset
            await self.send_to([player], {'t': 'kick'})
            return
        player.ready = True
        player.refill()
        player.level = 0
        if self.phase == 'playing':               # joining a running game: start at the lowest level in play
            others = [p.level for p in self.active() if p is not player]
            player.level = min(min(others, default=0), LAST_LEVEL - 1)
        player.kind = CHARACTERS[player.level]
        print(f'{player.name} is ready ({len(self.active())} ready)')
        if self.phase == 'lobby' and len(self.active()) >= 2 and self.cd_task is None:
            self.phase = 'countdown'
            self.cd_task = asyncio.ensure_future(self.run_countdown())
        else:
            await self.send_gg()

    async def set_unready(self, player):
        if not player.ready or self.phase in ('countdown', 'ended'):
            return                                # locked in during the countdown
        player.ready = False
        player.level = 0
        player.kind = 'spongebob'
        player.refill()
        await self.after_leave()

    async def after_leave(self):
        if self.phase == 'playing' and len(self.active()) < 2:
            await self.reset_to_lobby()           # nobody left to play against
        elif self.phase != 'ended':
            await self.send_gg()

    async def run_countdown(self):
        for left in range(COUNTDOWN, 0, -1):
            if len(self.active()) < 2:            # someone dropped out: back to waiting
                self.phase = 'lobby'
                self.cd_left = 0
                self.cd_task = None
                await self.send_gg()
                return
            self.cd_left = left
            await self.send_gg()
            await asyncio.sleep(1)
        self.cd_task = None
        if len(self.active()) < 2:
            self.phase = 'lobby'
            self.cd_left = 0
            await self.send_gg()
            return
        self.phase = 'playing'
        self.cd_left = 0
        for p in self.active():
            p.level = 0
            p.kind = CHARACTERS[0]
            p.refill()
        print('Game started')
        await self.send_gg()

    def random_spawn(self, player):
        x, z = (player.id - 3) * 2.0, 0.0           # the player's spawn point (same as the client's SPAWN_X, 0)
        others = [p for p in self.active() if p is not player]
        best = (x, z)
        for _ in range(12):
            ang = random.uniform(0, math.tau)
            r = math.sqrt(random.uniform(SPAWN_MIN ** 2, SPAWN_RADIUS ** 2))   # even spread over the circle
            nx, nz = x + math.cos(ang) * r, z + math.sin(ang) * r
            if abs(nx) > MAP_LIMIT or abs(nz) > MAP_LIMIT:
                continue
            best = (nx, nz)
            if all(math.hypot(nx - o.state[0], nz - o.state[2]) > 4 for o in others):
                break                              # not right on top of someone else
        return round(best[0], 2), round(best[1], 2)

    async def level_up(self, killer, cause=None):
        if killer.level >= LAST_LEVEL:            # that kill was made with the last character (Thanos)
            await self.win(killer)
            return
        if isinstance(cause, str):
            killer.stale[cause] = time.monotonic() + STALE_CAUSE_TIME   # ...so the old character's ability (still travelling) can't level you up again
        killer.level += 1
        killer.kind = CHARACTERS[killer.level]
        killer.refill()
        killer.protect()                          # ...and after a kill, since you respawn somewhere new
        x, z = self.random_spawn(killer)
        await self.send_to(self.active(), {'t': 'lvl', 'id': killer.id, 'level': killer.level,
                                           'kind': killer.kind, 'x': x, 'z': z})

    async def win(self, player):
        self.phase = 'ended'
        print(f'{player.name} won')
        await self.send_to(self.active(), {'t': 'win', 'id': player.id, 'name': player.name,
                                           'text': f'{player.name.upper()} WON!'})
        asyncio.ensure_future(self.finish_after_win())

    async def finish_after_win(self):
        await asyncio.sleep(WIN_KICK_DELAY)
        await self.reset_to_lobby()

    async def reset_to_lobby(self):
        targets = self.active()
        for p in self.players.values():
            if p.free:
                continue                          # free play carries on whatever the gun game does
            p.ready = False
            p.level = 0
            p.kind = 'spongebob'
            p.refill()
            if p.web_task:
                p.web_task.cancel()
        self.phase = 'lobby'
        self.cd_left = 0
        await self.send_to(targets, {'t': 'kick'})

    # ---------------------------------------------------------- snapshots
    async def snapshot_loop(self):
        while True:
            await asyncio.sleep(SNAP_INTERVAL)
            players = list(self.players.values())
            if not players:
                continue
            def shown(p):                              # a respawned player shows as a ghost to everyone for a moment
                return {**p.extra, 'gh': 1, 'sp': 1} if p.protected() else p.extra
            data = {p.id: (p.room(), [round(v, 2) if isinstance(v, float) else v for v in p.state]
                           + [round(p.hp, 1), p.kind, shown(p)]) for p in players}
            for room in ('gun', 'free'):
                targets = [p for p in players if p.room() == room]
                if not targets:
                    continue
                # your own room, plus the see-through ghosts of whoever is in the menu
                snap = {str(pid): d for pid, (r, d) in data.items() if r in (room, None)}
                await self.send_to(targets, {'t': 'snap', 'p': snap})


game = Game()


# ---------------------------------------------------------------- websocket glue
async def handler(ws):
    player = None
    try:
        first = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if first.get('t') != 'hello':
            return
        player = await game.add_player(ws, first.get('name', ''))
        if player is None:
            return
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if isinstance(msg, dict):
                await game.handle(player, msg)
    except Exception:
        pass
    finally:
        if player is not None:
            await game.remove_player(player)


def health_check(connection, request):
    """Plain web visits get the game page (so one Render link does everything);
    websocket connections fall through to the game."""
    if request.headers.get('Upgrade', '').lower() == 'websocket':
        return None
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ('conquest.html', 'index.html'):
        path = os.path.join(here, name)
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                response = connection.respond(HTTPStatus.OK, f.read())
            del response.headers['Content-Type']
            response.headers['Content-Type'] = 'text/html; charset=utf-8'
            return response
    return connection.respond(HTTPStatus.OK, 'Conquest server is running.\n')


async def main():
    from websockets.asyncio.server import serve
    port = int(os.environ.get('PORT', 8080))
    async with serve(handler, '0.0.0.0', port, process_request=health_check):
        print(f'Conquest server listening on port {port}')
        await game.snapshot_loop()


if __name__ == '__main__':
    asyncio.run(main())

