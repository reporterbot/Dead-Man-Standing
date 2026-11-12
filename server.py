#!/usr/bin/env python3
"""Pump200 server.

Serves the game, keeps the list of claimed usernames in USERNAME_LIST.json so
nobody can take a name someone else already has, lists the shirts/ folder, and
runs online lobbies (create / join / start, then fast state sync while playing).

Accounts are username-only (no password, no email); the browser remembers who
you are with a cookie.

Run:
    python server.py
then open http://localhost:8000

Environment variables:
    PORT  port to listen on (default 8000)
    HOST  interface to bind (default 127.0.0.1; use 0.0.0.0 so other devices
          on your network can connect and play online with you)
"""
import json
import os
import random
import re
import string
import threading
import time
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
LIST_PATH = os.path.join(ROOT, "USERNAME_LIST.json")
SHIRTS_DIR = os.path.join(ROOT, "shirts")
SHIRT_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")
NAME_RULES = "Usernames are 3-16 characters: letters, numbers or _"
COOKIE = "pump200_user"
ONE_YEAR = 60 * 60 * 24 * 365

# lobbies
CODE_RE = re.compile(r"^[A-Z]{5}$")
MAPS = ("baseplate", "city")
MIN_PLAYERS, MAX_PLAYERS = 2, 8
PLAYER_TIMEOUT = 15          # seconds without a request before a player is dropped
MAX_EVENTS = 600

# One lock around read-check-write so two people can't claim the same name at once.
_lock = threading.Lock()
# Everything about lobbies lives in memory and is guarded by this lock.
_lobby_lock = threading.Lock()
lobbies = {}


# ---------------------------------------------------------------- usernames
def load_names():
    """Return the list of claimed usernames (empty if the file doesn't exist yet)."""
    try:
        with open(LIST_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    if not isinstance(data, list):
        raise ValueError("USERNAME_LIST.json must contain a JSON list of names")
    return [n for n in data if isinstance(n, str)]


def save_names(names):
    """Write the list atomically so a crash mid-write can't corrupt it."""
    tmp = LIST_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(names, f, indent=2)
        f.write("\n")
    os.replace(tmp, LIST_PATH)


def find_name(names, name):
    """Case-insensitive lookup: 'Bob' and 'bob' are the same account."""
    lowered = name.lower()
    return next((n for n in names if n.lower() == lowered), None)


def registered(name):
    """The canonical spelling of a registered username, or None."""
    if not isinstance(name, str) or not NAME_RE.match(name):
        return None
    with _lock:
        return find_name(load_names(), name)


# ---------------------------------------------------------------- lobbies
def new_code():
    while True:
        code = "".join(random.choice(string.ascii_uppercase) for _ in range(5))
        if code not in lobbies:
            return code


def add_event(lobby, kind, who, data):
    lobby["next_id"] += 1
    lobby["events"].append({"id": lobby["next_id"], "type": kind, "from": who, "data": data})
    if len(lobby["events"]) > MAX_EVENTS:
        del lobby["events"][: MAX_EVENTS // 3]


def remove_player(lobby, name):
    if lobby["players"].pop(name, None) is None:
        return
    add_event(lobby, "leave", name, {})
    if lobby["host"] == name and lobby["players"]:
        lobby["host"] = next(iter(lobby["players"]))      # longest-standing player takes over
        lobby["npcs"] = None
        add_event(lobby, "host", lobby["host"], {})


def prune_lobbies():
    t = time.time()
    for code in list(lobbies):
        lobby = lobbies[code]
        for name in [n for n, p in lobby["players"].items() if t - p["seen"] > PLAYER_TIMEOUT]:
            remove_player(lobby, name)
        if not lobby["players"]:
            del lobbies[code]


def lobby_public(lobby, profiles=False):
    players = []
    for name, p in lobby["players"].items():
        item = {"name": name, "host": name == lobby["host"]}
        if profiles:
            item["pfp"] = p["pfp"]
            item["look"] = p["look"]
        players.append(item)
    return {
        "code": lobby["code"], "host": lobby["host"], "max": lobby["max"],
        "map": lobby["map"], "state": lobby["state"], "players": players,
    }


def clean_look(look):
    if not isinstance(look, dict) or len(json.dumps(look)) > 4000:
        return {}
    return look


def clean_pfp(pfp):
    if isinstance(pfp, str) and pfp.startswith("data:image/") and len(pfp) < 250_000:
        return pfp
    return ""


def new_player(look, pfp):
    return {"seen": time.time(), "look": clean_look(look), "pfp": clean_pfp(pfp), "state": None}


# ---------------------------------------------------------------- HTTP
class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"        # keep-alive: the game syncs many times a second

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def log_message(self, fmt, *args):
        # the lobby sync runs ~12 times a second per player; don't flood the console with it
        if "/api/lobby/sync" in str(args[0] if args else ""):
            return
        super().log_message(fmt, *args)

    # Allow the API to be called from index.html opened straight from disk (file://).
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_json(self, status, payload, cookie=None):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def read_json(self, limit):
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            self.rfile.read(length)
            return None, (413, "Request too large")
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return None, (400, "Bad request")
        if not isinstance(data, dict):
            return None, (400, "Bad request")
        return data, None

    def fail(self, status, error):
        return self.send_json(status, {"ok": False, "error": error})

    # ------------------------------------------------------------ GET
    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)

        if url.path == "/api/check":
            name = query.get("username", [""])[0].strip()
            if not NAME_RE.match(name):
                return self.fail(400, NAME_RULES)
            with _lock:
                taken = find_name(load_names(), name) is not None
            return self.send_json(200, {"ok": True, "available": not taken})

        if url.path == "/api/shirts":
            # every image in the shirts/ folder becomes a shirt in Customize → Accessories → Shirt
            try:
                files = sorted(
                    f for f in os.listdir(SHIRTS_DIR)
                    if f.lower().endswith(SHIRT_EXTS) and os.path.isfile(os.path.join(SHIRTS_DIR, f))
                )
            except FileNotFoundError:
                files = []
            return self.send_json(200, {"ok": True, "shirts": files})

        if url.path == "/api/me":
            jar = SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie", ""))
            except Exception:
                pass
            name = jar[COOKIE].value if COOKIE in jar else ""
            with _lock:
                match = find_name(load_names(), name) if name else None
            return self.send_json(200, {"ok": True, "username": match})

        if url.path == "/api/lobby/newcode":
            with _lobby_lock:
                prune_lobbies()
                return self.send_json(200, {"ok": True, "code": new_code()})

        if url.path == "/api/lobby/info":
            code = query.get("code", [""])[0].strip().upper()
            with _lobby_lock:
                prune_lobbies()
                lobby = lobbies.get(code)
                if not lobby:
                    return self.fail(404, "That lobby doesn't exist anymore.")
                return self.send_json(200, {"ok": True, "lobby": lobby_public(lobby, profiles=True)})

        # don't hand out the server's own files
        if url.path.lstrip("/") in ("USERNAME_LIST.json", "server.py") or url.path.endswith(".tmp"):
            return self.send_error(404)
        return super().do_GET()

    # ------------------------------------------------------------ POST
    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/register":
            return self.register()
        if path.startswith("/api/lobby/"):
            return self.lobby_action(path[len("/api/lobby/"):])
        return self.fail(404, "Not found")

    def register(self):
        data, err = self.read_json(2048)
        if err:
            return self.fail(*err)
        name = str(data.get("username", "")).strip()
        if not NAME_RE.match(name):
            return self.fail(400, NAME_RULES)
        with _lock:
            names = load_names()
            if find_name(names, name) is not None:
                return self.fail(409, "That username is already taken.")
            names.append(name)
            save_names(names)
        cookie = f"{COOKIE}={name}; Max-Age={ONE_YEAR}; Path=/; SameSite=Lax"
        return self.send_json(201, {"ok": True, "username": name}, cookie)

    def lobby_action(self, action):
        data, err = self.read_json(300_000)
        if err:
            return self.fail(*err)
        name = registered(str(data.get("username", "")).strip())
        if not name:
            return self.fail(403, "You need to sign up first.")
        code = str(data.get("code", "")).strip().upper()

        with _lobby_lock:
            prune_lobbies()

            if action == "create":
                if not CODE_RE.match(code):
                    return self.fail(400, "Lobby codes are 5 capital letters.")
                if code in lobbies:
                    return self.fail(409, "That code is already in use, regenerate it.")
                try:
                    max_players = int(data.get("max", 4))
                except (TypeError, ValueError):
                    max_players = 4
                map_id = data.get("map") if data.get("map") in MAPS else MAPS[0]
                self.leave_everywhere(name)
                lobbies[code] = {
                    "code": code, "host": name, "max": max(MIN_PLAYERS, min(MAX_PLAYERS, max_players)),
                    "map": map_id, "state": "waiting", "players": {name: new_player(data.get("look"), data.get("pfp"))},
                    "events": [], "next_id": 0, "npcs": None,
                }
                return self.send_json(201, {"ok": True, "lobby": lobby_public(lobbies[code], profiles=True)})

            lobby = lobbies.get(code)
            if not lobby:
                return self.fail(404, "No lobby with that code.")

            if action == "join":
                if name not in lobby["players"]:
                    if len(lobby["players"]) >= lobby["max"]:
                        return self.fail(409, "That lobby is full.")
                    self.leave_everywhere(name)
                    lobby["players"][name] = new_player(data.get("look"), data.get("pfp"))
                    add_event(lobby, "join", name, {})
                else:
                    lobby["players"][name]["seen"] = time.time()
                return self.send_json(200, {"ok": True, "lobby": lobby_public(lobby, profiles=True)})

            player = lobby["players"].get(name)
            if not player:
                return self.fail(404, "You're not in that lobby.")
            player["seen"] = time.time()

            if action == "info":
                # the waiting room refreshes with this every second, which also keeps you in the lobby
                return self.send_json(200, {"ok": True, "lobby": lobby_public(lobby, profiles=True)})

            if action == "leave":
                remove_player(lobby, name)
                if not lobby["players"]:
                    del lobbies[code]
                return self.send_json(200, {"ok": True})

            if action in ("map", "start") and lobby["host"] != name:
                return self.fail(403, "Only the host can do that.")

            if action == "map":
                if data.get("map") not in MAPS:
                    return self.fail(400, "Unknown map.")
                lobby["map"] = data["map"]
                add_event(lobby, "map", name, {"map": lobby["map"]})
                return self.send_json(200, {"ok": True, "lobby": lobby_public(lobby)})

            if action == "start":
                lobby["state"] = "playing"
                lobby["npcs"] = None
                add_event(lobby, "start", name, {"map": lobby["map"]})
                return self.send_json(200, {"ok": True, "lobby": lobby_public(lobby)})

            if action == "sync":
                # your latest pose in; everyone else's poses + new events (+ the host's NPCs) out
                state = data.get("state")
                if isinstance(state, dict):
                    player["state"] = state
                events = data.get("events")
                if isinstance(events, list):
                    for ev in events[:40]:
                        if isinstance(ev, dict) and ev.get("type") in ("hit",) and isinstance(ev.get("data"), dict):
                            add_event(lobby, ev["type"], name, ev["data"])
                npcs = data.get("npcs")
                if name == lobby["host"] and isinstance(npcs, list) and len(npcs) <= 64:
                    lobby["npcs"] = npcs
                try:
                    since = int(data.get("since", 0))
                except (TypeError, ValueError):
                    since = 0
                resp = {
                    "ok": True,
                    "lobby": lobby_public(lobby),
                    "others": {n: q["state"] for n, q in lobby["players"].items() if n != name and q["state"]},
                    "events": [e for e in lobby["events"] if e["id"] > since],
                    "last": lobby["next_id"],
                }
                if name != lobby["host"] and lobby["npcs"] is not None:
                    resp["npcs"] = lobby["npcs"]
                return self.send_json(200, resp)

        return self.fail(404, "Not found")

    @staticmethod
    def leave_everywhere(name):
        """A player can only be in one lobby at a time (call with _lobby_lock held)."""
        for code in list(lobbies):
            remove_player(lobbies[code], name)
            if not lobbies[code]["players"]:
                del lobbies[code]


def main():
    if not os.path.exists(LIST_PATH):
        save_names([])
    load_names()  # fail fast if the file is broken
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print(f"Pump200 running at http://localhost:{PORT}")
    print(f"Usernames are stored in {LIST_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
