"""Conquest multiplayer server (WebSocket version, works on Replit).
Run: python conquest_server.py
Env: PORT (default 8080)
GUN GAME: players click Play (message 'ready'). When two are ready a 10 second countdown
starts (nobody can leave). Every kill on another player moves the killer up the ladder
(CHARACTERS order) with full health and a random respawn near where they were. A kill made
while playing the last character (Vader) wins: everyone is told, then kicked after 5 seconds.
FREE PLAY: a second, separate room. A player who clicks Free Play (message 'free' with a character
kind) can be any character and switch whenever they like ('fkind'). Free players only see and hurt other
free players, never respawn into the ladder, and have their own kills/deaths table (message 'fp').
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
from http import HTTPStatus

MAX_PLAYERS = 5
CHARACTERS = ('spongebob', 'jack', 'wick', 'spiderman', 'koolaid', 'ironman', 'warden')
FX_KINDS = ('spray', 'ship', 'shot', 'point', 'med', 'whistle',
            'straw', 'webshot', 'wlstart', 'wlgo', 'webt', 'slam', 'fpull', 'repulsor', 'repcharge', 'unistart')  # ability visuals we relay
TARGET_COUNT = 8
MAX_HP = 100.0
TARGET_REVIVE_DELAY = 3.0
SNAP_INTERVAL = 0.05          # 20 snapshots per second
WEB_TIME = 5.0                # how long a webbed player stays stuck
WEB_DAMAGE_TOTAL = 100 / 3 * 1.7    # electric damage over that time
WEB_TICKS = 10
MAX_DAMAGE = 500.0            # sanity clamp on a single reported hit
LAST_LEVEL = len(CHARACTERS) - 1   # Vader: a kill with him wins the game
COUNTDOWN = 10                # seconds between the 2nd player clicking Play and the start
WIN_KICK_DELAY = 5            # seconds the winner text shows before everyone is kicked
SPAWN_RADIUS = 35.0           # after a kill you reappear this far (at most) from where you were
SPAWN_MIN = 6.0
MAP_LIMIT = 280.0
# what the victim's name is followed by when someone dies, by the cause the attacker's client reports
DEATH_TEXT = {
    'burger': 'was a burger', 'bubble': 'popped', 'sword': 'was chopped', 'ship': 'went overboard',
    'pistol': 'was shot', 'dog': 'was bitten', 'launch': 'got mauled', 'web': 'was electrified',
    'slam': 'was slammed', 'straw': 'got spit on', 'saber': 'was sliced and diced',
    'repulsor': 'was blasted', 'unibeam': 'was vaporized',
    'hunted': 'was hunted to death',   # killing John Wick's dog
}
DEFAULT_DEATH = 'was killed'


class Player:
    def __init__(self, pid, name, conn):
        self.id = pid
        self.name = name
        self.conn = conn
        self.hp = MAX_HP
        self.kind = 'spongebob'   # which character they picked
        self.ghost = False        # True while they are in the menu: untouchable
        self.skeleton = False     # True while Jack is in skeleton form: takes no damage
        self.ready = False        # clicked Play: part of the gun game
        self.free = False         # clicked Free Play: part of the free play room
        self.fkills = 0           # free play tally (kept apart from the gun game's)
        self.fdeaths = 0
        self.level = 0            # index into CHARACTERS (the gun game ladder)
        self.kills = 0
        self.deaths = 0
        self.web_task = None      # running web-electricity timer
        self.extra = {}           # weapon in hand, skeleton form, dog position, ghost flag
        # x, y, z, ry, weapon, punch_t, pitch (hp is appended when sending)
        self.state = [(pid - 3) * 2.0, 0.0, 0.0, 0.0, 0, -1, 0.0]

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

    async def remove_player(self, player):
        if self.players.get(player.id) is player:
            was_ready = player.ready
            was_free = player.free
            player.ready = False
            player.free = False
            del self.players[player.id]
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
                if player.ghost:
                    extra['gh'] = 1
                    player.hp = MAX_HP             # ghosts are always topped up
                if m.get('wl') in (1, 2, 3):
                    extra['wl'] = m['wl']
                dg = m.get('dg')
                if isinstance(dg, list) and len(dg) == 3:
                    extra['dg'] = [float(dg[0]), float(dg[1]), float(dg[2])]
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
            player.hp = MAX_HP
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
        elif t == 'web':
            victim = self.players.get(m.get('id'))
            if (victim is not None and victim is not player and not victim.ghost
                    and victim.web_task is None and player.room() is not None
                    and victim.room() == player.room()):
                victim.web_task = asyncio.ensure_future(self.run_web(player, victim))

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
        if victim is None or victim.ghost:       # ghosts (in the menu) can't be hurt
            return
        if victim.skeleton and cause != 'hunted':  # skeletons can't be hurt, except by the dog's curse
            return
        room = attacker.room()
        if room is None or victim.room() != room:
            return                                # you can only hurt people in your own room
        if room == 'gun' and self.phase != 'playing':
            return                                # no damage in the lobby, countdown or after a win
        amount = max(0.0, min(amount, MAX_DAMAGE))
        victim.hp -= amount
        if victim.hp <= 0:
            victim.hp = MAX_HP
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
            await self.broadcast_room(room, {'t': 'died', 'id': victim.id, 'killer': killer, 'text': text})
            if room == 'free':
                await self.send_fp()
            elif victim is not attacker and self.phase == 'playing':
                await self.level_up(attacker)

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
        if kind not in CHARACTERS:
            kind = 'spongebob'
        if not player.free:
            player.free = True
            player.hp = MAX_HP
            print(f'{player.name} joined free play ({len(self.free_players())} there)')
        player.kind = kind
        await self.send_fp()

    async def leave_free(self, player):
        if not player.free:
            return
        if player.web_task:
            player.web_task.cancel()
            player.web_task = None
            await self.broadcast_room('free', {'t': 'unwebbed', 'id': player.id})
        player.free = False
        player.kind = 'spongebob'
        player.hp = MAX_HP
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
        player.hp = MAX_HP
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
        player.hp = MAX_HP
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
            p.hp = MAX_HP
        print('Game started')
        await self.send_gg()

    def random_spawn(self, player):
        x, z = player.state[0], player.state[2]
        others = [p for p in self.active() if p is not player]
        best = (x, z)
        for _ in range(12):
            ang = random.uniform(0, math.tau)
            r = random.uniform(SPAWN_MIN, SPAWN_RADIUS)
            nx, nz = x + math.cos(ang) * r, z + math.sin(ang) * r
            if abs(nx) > MAP_LIMIT or abs(nz) > MAP_LIMIT:
                continue
            best = (nx, nz)
            if all(math.hypot(nx - o.state[0], nz - o.state[2]) > 10 for o in others):
                break                              # not right on top of someone else
        return round(best[0], 2), round(best[1], 2)

    async def level_up(self, killer):
        if killer.level >= LAST_LEVEL:            # that kill was made with Vader
            await self.win(killer)
            return
        killer.level += 1
        killer.kind = CHARACTERS[killer.level]
        killer.hp = MAX_HP
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
            p.hp = MAX_HP
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
            data = {p.id: (p.room(), [round(v, 2) if isinstance(v, float) else v for v in p.state]
                           + [round(p.hp, 1), p.kind, p.extra]) for p in players}
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

