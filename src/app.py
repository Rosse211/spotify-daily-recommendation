#!/usr/bin/env python3
import json, math, random, re, socket, sys, threading, time, traceback, urllib.parse, urllib.request, webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import spotify_dump as sp

DATA = sp.DATA
PAGES = sp.ROOT / "pages"
HISTORY = DATA / "history.json"
TODAY = DATA / "today.json"
FEEDBACK = DATA / "feedback.json"
GENRES = DATA / "genres.json"
ARTISTS = DATA / "artists.json"
PREFS = DATA / "prefs.json"
TOP_GENRES = 5
DISLIKES_TO_BAN = 3
MAX_REROLLS = 3
UNKNOWN = "unknown"


ACCOUNT_FILES = [sp.TOKENS, DATA / "seeds.json", DATA / "known.json",
                 GENRES, ARTISTS, PREFS, FEEDBACK, HISTORY, TODAY]


def load(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def warn(what, exc):
    print(f"  ! {what}: {type(exc).__name__}: {exc}", file=sys.stderr)


def dz(path, **params):
    url = "https://api.deezer.com/" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept-Language": "en"})
    for attempt in range(5):
        try:
            r = json.load(urllib.request.urlopen(req, timeout=15))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            if isinstance(e, urllib.error.HTTPError) and e.code < 500:
                raise
            if attempt == 4:
                raise RuntimeError(f"Deezer unreachable on {path}: {e}") from e
            time.sleep(2 ** attempt)
            continue
        if r.get("error", {}).get("code") != 4:
            return r
        time.sleep(1)
    raise RuntimeError("Deezer quota exhausted on " + path)


LANGUAGES = {
    "italian": "italian", "italiano": "italian", "italy": "italian",
    "french": "french", "francais": "french", "francophone": "french",
    "spanish": "spanish", "espanol": "spanish", "reggaeton": "spanish",
    "latin": "spanish", "latino": "spanish",
    "german": "german", "deutsch": "german", "deutschrap": "german",
    "portuguese": "portuguese", "brazilian": "portuguese", "brasil": "portuguese",
    "japanese": "japanese", "j-pop": "japanese", "j-rock": "japanese",
    "korean": "korean", "k-pop": "korean",
    "english": "english", "british": "english", "american": "english",
    "uk": "english", "usa": "english", "us": "english", "australian": "english",
}
NO_LYRICS = {"instrumental", "instrumentals", "instrumental hip-hop",
             "ambient", "soundtrack", "score", "lo-fi beats"}
SCENE_TAGS = {"french house", "french touch", "italo disco", "italo house",
              "italo dance", "latin jazz", "afro house", "german techno",
              "british invasion", "spanish guitar", "latin freestyle"}
DANCE_TAGS = {"house", "techno", "electro", "electronica", "electronic", "edm", "trance",
              "downtempo", "idm", "minimal", "drum and bass", "dnb", "breakbeat", "dance"}
FOCUS_NOISE = re.compile(
    r"\b(\d+\s*hz|binaural|isochronic|solfeggio|delta waves?|alpha waves?|theta waves?|"
    r"white noise|pink noise|brown noise|rain sounds?|rainfall|thunderstorm|"
    r"ocean waves?|nature sounds?|forest sounds?|fireplace|"
    r"deep sleep|sleep (sounds?|music|vibes|aid)|sleeping vibes|lullab(y|ies)|"
    r"meditation|mindfulness|reiki|asmr|"
    r"study (music|beats|vibes)|focus (music|beats|vibes)|concentration|"
    r"relaxing (music|sounds?|piano)|calm(ing)? (music|sounds?))\b", re.I)
NOISE_TAGS = {"sleep", "sleep music", "meditation", "binaural beats", "white noise",
              "relaxation", "relaxing", "healing", "asmr", "study music", "new age",
              "background music", "nature sounds", "field recording"}
LASTFM = "https://ws.audioscrobbler.com/2.0/?"
_lfm_last = [0.0]
_lfm_fail = [0]


def lastfm_key():
    return sp.config("lastfm_api_key")


def tags_of(name, key):
    url = LASTFM + urllib.parse.urlencode({"method": "artist.getTopTags", "artist": name,
                                           "api_key": key, "format": "json", "autocorrect": 1})
    time.sleep(max(0, 0.25 - (time.monotonic() - _lfm_last[0])))
    _lfm_last[0] = time.monotonic()
    d = json.load(urllib.request.urlopen(url, timeout=20))
    return [t["name"].lower() for t in d.get("toptags", {}).get("tag", [])][:15]


def language_of_tag(tag):
    if tag in SCENE_TAGS:
        return None
    if tag in LANGUAGES:
        return LANGUAGES[tag]
    return next((LANGUAGES[w] for w in tag.replace("-", " ").split() if w in LANGUAGES), None)


def language_from_tags(tags):
    for t in tags:
        if t in NO_LYRICS:
            return None
    if DANCE_TAGS & set(tags[:3]):
        return None
    votes = {}
    for t in tags:
        found = language_of_tag(t)
        if found:
            votes[found] = votes.get(found, 0) + 1
    if not votes:
        return None
    best = max(votes.values())
    winners = [lang for lang, n in votes.items() if n == best]
    return winners[0] if len(winners) == 1 else None


def is_noise(name, tags=()):
    return bool(FOCUS_NOISE.search(name)) or bool(NOISE_TAGS & set(tags))


def artists_info(names):
    cache = load(ARTISTS, {})
    key = lastfm_key()
    todo = [n for n in dict.fromkeys(names) if n and n not in cache]
    if todo and key and _lfm_fail[0] < 5:
        for n in todo:
            try:
                tags = tags_of(n, key)
                cache[n] = {"lang": language_from_tags(tags) or UNKNOWN,
                            "noise": is_noise(n, tags), "tags": tags}
                _lfm_fail[0] = 0
            except Exception as e:
                warn(f"Last.fm tags for {n!r}", e)
                _lfm_fail[0] += 1
        ARTISTS.write_text(json.dumps(cache, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    blank = {"lang": UNKNOWN, "noise": False, "tags": []}
    return {n: {**blank, **cache.get(n, {"noise": is_noise(n)})} for n in names}


def lang_of(name):
    return artists_info([name])[name]["lang"]


DEEZER = DATA / "deezer.json"
SEARCHED = DATA / "deezer_artists.json"
_genre_of = None
_searched = None
_found = {}


def genre_cache():
    global _genre_of
    if _genre_of is None:
        _genre_of = {int(k): v for k, v in load(DEEZER, {}).items()}
    return _genre_of


def save_genre_cache():
    if _genre_of:
        DEEZER.write_text(json.dumps(_genre_of, indent=0), encoding="utf-8")
_gname = {}


def genre_name(gid):
    if not _gname:
        _gname.update({g["id"]: g["name"] for g in dz("genre")["data"] if g["id"]})
    if gid not in _gname:
        try:
            _gname[gid] = dz(f"genre/{gid}").get("name") or str(gid)
        except Exception as e:
            warn(f"Deezer genre {gid}", e)
            _gname[gid] = str(gid)
    return _gname[gid]


def artist_genre(deezer_id):
    cache = genre_cache()
    if deezer_id not in cache:
        ids = [a["genre_id"] for a in dz(f"artist/{deezer_id}/albums", limit=25).get("data", [])
               if a.get("genre_id", -1) > 0]
        cache[deezer_id] = max(set(ids), key=ids.count) if ids else None
    return cache[deezer_id]


def search_cache():
    global _searched
    if _searched is None:
        _searched = load(SEARCHED, {})
    return _searched


def find_artist(name):
    cache = search_cache()
    if name not in cache:
        hit = dz("search/artist", q=name, limit=1).get("data")
        cache[name] = hit[0]["id"] if hit else None
        SEARCHED.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
    return cache[name]


def related_of(deezer_id):
    key = f"related:{deezer_id}"
    if key not in _found:
        _found[key] = dz(f"artist/{deezer_id}/related").get("data", [])
    return _found[key]


def pretty_genre(tag):
    words = tag.replace("-", " ").split()
    return " ".join(w if w != w.lower() else w.capitalize() for w in words)


def bucket_of(genre_name, language):
    return f"{genre_name} · {language}" if language != UNKNOWN else genre_name


def all_tracks(seeds):
    return ([t for r in seeds["tracks"].values() for t in r]
            + seeds.get("saved", []) + seeds.get("playlists", []))


def is_active(track_key, in_source, overrides):
    return overrides.get(track_key, in_source)


def taste_tracks(seeds, source, overrides=None):
    overrides = overrides or {}
    saved = {sp.key(t["artist"], t["title"]) for t in seeds.get("saved", [])}
    return [t for t in all_tracks(seeds)
            if is_active(sp.key(t["artist"], t["title"]),
                         source != "saved" or sp.key(t["artist"], t["title"]) in saved, overrides)]


def play_counts(seeds, source="all", overrides=None):
    counts = {}
    for t in taste_tracks(seeds, source, overrides):
        counts[t["artist"]] = counts.get(t["artist"], 0) + 1
    return counts


def taste_artists(seeds, source, overrides=None):
    if source != "saved" and not overrides:
        return seeds["artists"]
    plays = play_counts(seeds, source, overrides)
    return [{"name": n} for n in sorted(plays, key=plays.get, reverse=True)]


def compute_genres(source="all", overrides=None):
    seeds = json.loads((DATA / "seeds.json").read_text(encoding="utf-8"))
    artists = taste_artists(seeds, source, overrides)
    plays = play_counts(seeds, source, overrides)
    info = artists_info([a["name"] for a in artists])
    counts = {}
    for a in artists:
        if info[a["name"]]["noise"]:
            continue
        found = find_artist(a["name"])
        if not found:
            continue
        g = artist_genre(found)
        if not g:
            continue
        b = bucket_of(genre_name(g), info[a["name"]]["lang"])
        counts[b] = counts.get(b, 0) + max(1, plays.get(a["name"], 0))
    top = sorted(counts, key=counts.get, reverse=True)[:TOP_GENRES]
    data = {"top": top, "counts": dict(sorted(counts.items(), key=lambda kv: -kv[1]))}
    cached = load(GENRES, {})
    cached[source] = data
    GENRES.write_text(json.dumps(cached, ensure_ascii=False, indent=1), encoding="utf-8")
    save_genre_cache()
    return data


def genres_for(source, overrides=None):
    return load(GENRES, {}).get(source) or compute_genres(source, overrides)


def seed_order(seeds, prefs, rnd):
    weights = load(FEEDBACK, {}).get("weights", {})
    weight = lambda a: max(0.25, 1 + weights.get(a["name"], 0))
    artists = sorted(taste_artists(seeds, prefs["source"], prefs["overrides"]),
                     key=lambda a: rnd.random() ** (1 / weight(a)), reverse=True)[:8]
    states = prefs["states"]
    for tag in prefs["extra"]:
        if states.get(pretty_genre(tag), "in") != "out":
            pool = [{"name": n} for n in tag_artists(tag)]
            rnd.shuffle(pool)
            artists += pool[:4]
    return artists


def bucket_for(cand, info, extra):
    for tag in extra:
        if tag in info["tags"]:
            name = pretty_genre(tag)
            return bucket_of(name, info["lang"]), name
    bucket = bucket_of(genre_name(artist_genre(cand["id"])), info["lang"])
    return bucket, bucket


def bucket_allowed(bucket, state_key, strict, prefs, detected):
    hand_picked = state_key != bucket
    state = prefs["states"].get(state_key, "in" if hand_picked else "")
    if state_key in prefs["removed"] or bucket in prefs["removed"] or state == "out":
        return False
    if strict:
        return state == "in"
    return state == "in" or hand_picked or bucket in detected or prefs["new"]


def by_obscurity(items, value_of, rnd, obscurity=2.0):
    exponent = obscurity - 1
    ordered = sorted(items, key=value_of, reverse=True)
    weights = {id(x): (i + 1) ** exponent for i, x in enumerate(ordered)}
    return sorted(ordered, key=lambda x: rnd.random() ** (1 / weights[id(x)]), reverse=True)


def deep_cut_order(tracks, rnd, obscurity):
    return by_obscurity(tracks, lambda t: t.get("rank", 0), rnd, obscurity)


def playable(cand, known_t, seen, rnd, obscurity):
    
    tracks = deep_cut_order(dz(f"artist/{cand['id']}/top", limit=10).get("data", []),
                            rnd, obscurity)
    for t in tracks:
        k = sp.key(t["artist"]["name"], t["title"])
        if k in known_t or k in seen or not t.get("preview"):
            continue
        if FOCUS_NOISE.search(t["title"]):
            continue
        return k, t
    return None, None


def related_to(seed, rnd, obscurity):
    found = find_artist(seed["name"])
    if not found:
        return []
    return by_obscurity(related_of(found), lambda a: a.get("nb_fan", 0), rnd, obscurity)[:6]


def read_prefs():
    stored = load(PREFS, {})
    source = stored.get("source", "all")
    genres = genres_for(source, stored.get("overrides", {}))
    return {"source": source, "overrides": stored.get("overrides", {}),
            "states": stored.get("states", {b: "in" for b in genres["top"]}),
            "removed": set(stored.get("removed", [])),
            "extra": [g.lower() for g in stored.get("extra", [])],
            "obscurity": float(stored.get("obscurity",2.0)),
            "new": stored.get("new", False)}, genres


def pick(avoid_bucket=None):
    seeds = json.loads((DATA / "seeds.json").read_text(encoding="utf-8"))
    known = load(DATA / "known.json", {})
    known_a, known_t = set(known.get("artists", [])), set(known.get("tracks", []))
    seen = set(load(HISTORY, []))
    seen_artists = {k.split(" — ")[0] for k in seen}
    prefs, genres = read_prefs()
    detected = set(genres["counts"])
    dislikes = load(FEEDBACK, {}).get("dislikes", {})
    banned = {a for a, n in dislikes.items() if n >= DISLIKES_TO_BAN}
    off_limits = known_a | banned | seen_artists

    rnd = random.Random(date.today().isoformat())
    order = seed_order(seeds, prefs, rnd)
    seed_noise = artists_info([a["name"] for a in order])

    for strict, seed in [(s, a) for s in (True, False) for a in order]:
        if seed_noise[seed["name"]]["noise"]:
            continue
        related = related_to(seed, rnd, prefs["obscurity"])
        infos = artists_info([c["name"] for c in related])
        for cand in related:
            info = infos[cand["name"]]
            if sp.norm(cand["name"]) in off_limits or info["noise"]:
                continue
            bucket, state_key = bucket_for(cand, info, prefs["extra"])
            if bucket == avoid_bucket:
                continue
            if not bucket_allowed(bucket, state_key, strict, prefs, detected):
                continue
            key, track = playable(cand, known_t, seen, rnd, prefs["obscurity"])
            if key:
                save_genre_cache()
                return {"key": key, "title": track["title"],
                        "artist": track["artist"]["name"],
                        "cover": track["album"].get("cover_xl") or track["album"].get("cover_big"),
                        "preview": track["preview"], "seed": seed["name"], "genre": bucket}
    save_genre_cache()
    return None


def save_pick(card, rerolls=0):
    card["liked"] = False
    card["disliked"] = False
    card["rerolls"] = rerolls
    card["rerolls_left"] = max(0, MAX_REROLLS - rerolls)
    TODAY.write_text(json.dumps({"date": date.today().isoformat(), "card": card},
                                ensure_ascii=False), encoding="utf-8")
    HISTORY.write_text(json.dumps(load(HISTORY, []) + [card["key"]], ensure_ascii=False, indent=1), encoding="utf-8")
    return card


def today(avoid_bucket=None, rerolls=0):
    cached = load(TODAY, None)
    if cached and cached["date"] == date.today().isoformat():
        return cached["card"]
    card = pick(avoid_bucket)
    if not card:
        return {"error": "No candidate found. Try clearing some dislikes."}
    return save_pick(card, rerolls)


def nudge(seed, amount, artist=None, dislikes=0):
    fb = load(FEEDBACK, {})
    weights, counts = fb.setdefault("weights", {}), fb.setdefault("dislikes", {})
    weights[seed] = weights.get(seed, 0) + amount
    if artist and dislikes:
        counts[artist] = max(0, counts.get(artist, 0) + dislikes)
        if not counts[artist]:
            del counts[artist]
    FEEDBACK.write_text(json.dumps(fb, ensure_ascii=False, indent=1), encoding="utf-8")


def current_vote(card):
    return "liked" if card.get("liked") else "disliked" if card.get("disliked") else None


def set_vote(card, target):
    artist = sp.norm(card["artist"])
    was = current_vote(card)
    if was == target:
        target = None
    if was == "liked":
        nudge(card["seed"], -1)
    elif was == "disliked":
        nudge(card["seed"], +1, artist, -1)
    if target == "liked":
        nudge(card["seed"], +1)
    elif target == "disliked":
        nudge(card["seed"], -1, artist, +1)
    card["liked"] = target == "liked"
    card["disliked"] = target == "disliked"
    return card


def vote(action):
    cached = load(TODAY, None)
    if not cached:
        return {"error": "Nothing to vote on."}
    card = cached["card"]

    if action in ("like", "dislike"):
        set_vote(card, {"like": "liked", "dislike": "disliked"}[action])
        TODAY.write_text(json.dumps(cached, ensure_ascii=False), encoding="utf-8")
        return card

    used = card.get("rerolls", 0)
    if used >= MAX_REROLLS:
        return dict(card, error="No rerolls left today.")
    fresh = pick(avoid_bucket=card.get("genre"))
    if not fresh:
        return dict(card, error="Nothing else to offer today.")
    return save_pick(fresh, used + 1)


NON_GENRE = {"seen live", "female vocalists", "male vocalists", "favorites", "favourites",
             "60s", "70s", "80s", "90s", "00s", "10s", "british", "american", "usa", "uk",
             "cool", "awesome", "love", "beautiful", "sexy", "catchy", "albums i own",
             "under 2000 listeners", "spotify", "my favourites", "bookmark",
             "atlanta", "brooklyn", "chicago", "detroit", "london", "new york",
             "los angeles", "toronto", "memphis", "houston", "miami", "compton"}


def lastfm_genres():
    cached = load(ARTISTS, {})
    artist_names = {n.lower() for n in cached}
    seen = {}
    for name, info in cached.items():
        for t in info.get("tags", []):
            seen.setdefault(t.lower(), set()).add(name)
    names = {pretty_genre(t) for t, on in seen.items() if len(on) >= 2}

    key = lastfm_key()
    if key:
        url = LASTFM + urllib.parse.urlencode({"method": "tag.getTopTags", "api_key": key,
                                               "format": "json"})
        try:
            names |= {pretty_genre(t["name"]) for t in
                      json.load(urllib.request.urlopen(url, timeout=20))
                      .get("toptags", {}).get("tag", [])}
        except Exception as e:
            warn("Last.fm top tags", e)

    return sorted(n for n in names if n.lower() not in NON_GENRE
                  and n.lower() not in artist_names and not n.replace(" ", "").isdigit())


_tag_artists = {}


def tag_artists(tag, limit=30):
    if tag not in _tag_artists:
        key = lastfm_key()
        url = LASTFM + urllib.parse.urlencode({"method": "tag.getTopArtists", "tag": tag,
                                               "api_key": key, "format": "json", "limit": limit})
        try:
            d = json.load(urllib.request.urlopen(url, timeout=20))
            _tag_artists[tag] = [a["name"] for a in d.get("topartists", {}).get("artist", [])]
        except Exception as e:
            warn(f"Last.fm artists for tag {tag!r}", e)
            _tag_artists[tag] = []
    return _tag_artists[tag]


def check_lastfm_key(key):
    url = LASTFM + urllib.parse.urlencode({"method": "tag.getInfo", "tag": "rock",
                                           "api_key": key, "format": "json"})
    try:
        return "tag" in json.load(urllib.request.urlopen(url, timeout=20))
    except urllib.error.HTTPError:
        return False
    except Exception as e:
        warn("Last.fm key check", e)
        return None


def check_keys(spotify_id, lastfm):
    return {"spotify": bool(spotify_id.strip()) and sp.check_client_id(spotify_id.strip()),
            "lastfm": bool(lastfm.strip()) and check_lastfm_key(lastfm.strip())}


def tag_exists(name):
    key = lastfm_key()
    if not key:
        return False
    url = LASTFM + urllib.parse.urlencode({"method": "tag.getInfo", "tag": name,
                                           "api_key": key, "format": "json"})
    try:
        d = json.load(urllib.request.urlopen(url, timeout=20))
    except Exception as e:
        warn(f"Last.fm lookup for tag {name!r}", e)
        return False
    return int(d.get("tag", {}).get("reach", 0) or 0) > 0


def settings_data():
    if not (DATA / "seeds.json").exists():
        return {"error": "Log in with Spotify first."}
    prefs = load(PREFS, {})
    source = prefs.get("source", "all")
    overrides = prefs.get("overrides", {})
    genres = genres_for(source, overrides)
    removed = set(prefs.get("removed", []))
    states = prefs.get("states", {b: "in" for b in genres["top"]})
    seeds = json.loads((DATA / "seeds.json").read_text(encoding="utf-8"))
    sources = load(DATA / "known.json", {}).get("sources", {})
    saved = {sp.key(t["artist"], t["title"]) for t in seeds.get("saved", [])}

    rows, seen = [], set()
    for t in all_tracks(seeds):
        k = sp.key(t["artist"], t["title"])
        if k in seen:
            continue
        seen.add(k)
        rows.append([k, f"{t['artist']} \u2014 {t['title']}",
                     is_active(k, source != "saved" or k in saved, overrides),
                     sources.get(k, [])])
    rows.sort(key=lambda r: r[1].lower())
    extra = [pretty_genre(g) for g in prefs.get("extra", [])]
    buckets = [[g, 0, states.get(g, "in")] for g in extra if g not in removed]
    buckets += [[b, n, states.get(b, "")] for b, n in genres["counts"].items()
                if b not in removed and b not in extra]
    return {"source": source, "new": prefs.get("new", False), "extra": extra,
            "obscurity": float(prefs.get("obscurity", 2.0)),
            "buckets": buckets, "tracks": rows}


WRITER = threading.Lock()


def page(name):
    return (PAGES / name).read_text(encoding="utf-8")


def same_origin(handler):
    site = handler.headers.get("Sec-Fetch-Site")
    if site and site not in ("same-origin", "same-site", "none"):
        return False
    origin = handler.headers.get("Origin")
    return not origin or origin in (f"http://{handler.headers.get('Host', '')}",)


class Handler(BaseHTTPRequestHandler):
    def _send(self, body, ctype="application/json"):
        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if not same_origin(self):
            return self.send_error(403)
        try:
            if self.path == "/":
                return self._send(page("app.html"), "text/html")
            if self.path == "/settings":
                return self._send(page("settings.html"), "text/html")
            if self.path == "/api/settings":
                return self._send(json.dumps(settings_data(), ensure_ascii=False))
            if self.path == "/api/logout":
                with WRITER:
                    for f in ACCOUNT_FILES:
                        f.unlink(missing_ok=True)
                return self._send(json.dumps({"ok": True}))
            if self.path == "/api/reset-feedback":
                with WRITER:
                    FEEDBACK.unlink(missing_ok=True)
                    cached = load(TODAY, None)
                    if cached:
                        cached["card"]["liked"] = cached["card"]["disliked"] = False
                        TODAY.write_text(json.dumps(cached, ensure_ascii=False), encoding="utf-8")
                return self._send(json.dumps({"ok": True}))
            if self.path == "/api/reload-library":
                with WRITER:
                    sp.main()
                    GENRES.unlink(missing_ok=True)
                    TODAY.unlink(missing_ok=True)
                return self._send(json.dumps({"ok": True}))
            if self.path == "/api/tags":
                return self._send(json.dumps(lastfm_genres(), ensure_ascii=False))
            if self.path.startswith("/api/addtag"):
                t = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)["t"][0].strip()
                return self._send(json.dumps({"ok": tag_exists(t), "name": pretty_genre(t)}))
            if self.path == "/api/status":
                return self._send(json.dumps(
                    {"keys": bool(sp.config("spotify_client_id")) and bool(lastfm_key()),
                     "ready": sp.TOKENS.exists() and (DATA / "seeds.json").exists(),
                     "configured": PREFS.exists()}))
            if self.path == "/api/keys":
                return self._send(json.dumps(
                    {"spotify_client_id": sp.config("spotify_client_id") or "",
                     "lastfm_api_key": sp.config("lastfm_api_key") or "",
                     "port": str(PORT)}))
            if self.path.startswith("/api/genres"):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                saved = load(PREFS, {})
                source = q.get("source", [saved.get("source", "all")])[0]
                if q.get("peek"):
                    return self._send(json.dumps(
                        {"source": source, "buckets": [], "states": {},
                         "new": saved.get("new", False),
                         "obscurity": float(saved.get("obscurity", 2.0))}))
                g = genres_for(source)
                same = saved.get("source", "all") == source
                states = (saved.get("states") if same else None) or {
                    b: ("in" if b in g["top"] else "") for b in g["counts"]}
                return self._send(json.dumps(
                    {"buckets": list(g["counts"].items()),
                     "states": {b: states.get(b, "") for b in g["counts"]},
                     "new": saved.get("new", False), "source": source,
                     "obscurity": float(saved.get("obscurity", 2.0))},
                    ensure_ascii=False))
            if self.path == "/api/pending":
                return self._send(json.dumps({"url": sp.PENDING["url"]}))
            if self.path == "/api/login":
                if not WRITER.acquire(blocking=False):
                    waiting = sp.PENDING["url"]
                    if waiting:
                        return self._send(json.dumps(
                            {"error": "Finish the login in the Spotify window.",
                             "pending": waiting}))
                    return self._send(json.dumps({"error": "Busy, try again in a moment."}))
                try:
                    sp.main()
                finally:
                    WRITER.release()
                return self._send(json.dumps({"ready": True}))
            if self.path == "/api/today":
                with WRITER:
                    return self._send(json.dumps(today(), ensure_ascii=False))
            if self.path in ("/api/like", "/api/dislike", "/api/reroll"):
                with WRITER:
                    return self._send(json.dumps(vote(self.path.rsplit("/", 1)[1]),
                                                 ensure_ascii=False))
        except SystemExit as e:
            return self._send(json.dumps({"error": str(e)}))
        except Exception as e:
            traceback.print_exc()
            return self._send(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        self.send_error(404)

    def do_POST(self):
        if not same_origin(self):
            return self.send_error(403)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}")
            if self.path == "/api/keys":
                cid, lfm = body.get("spotify_client_id", ""), body.get("lastfm_api_key", "")
                port = str(body.get("port", "")).strip()
                checked = check_keys(cid, lfm)
                if port:
                    checked["port"] = valid_port(port)
                if all(checked.values()):
                    values = {"spotify_client_id": cid, "lastfm_api_key": lfm}
                    if port:
                        values["port"] = port
                    sp.save_config(values)
                return self._send(json.dumps(dict(checked, ok=all(checked.values()))))
            if self.path == "/api/prefs":
                body["obscurity"] = max(0.0, min(3.0, float(body.get("obscurity", 2.0))))
                PREFS.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
                return self._send(json.dumps({"ok": True}))
            if self.path == "/api/settings":
                with WRITER:
                    prefs = load(PREFS, {})
                    prefs["states"] = {b[0]: b[2] for b in body.get("buckets", [])}
                    prefs["removed"] = body.get("removed", [])
                    prefs["extra"] = [g.lower() for g in body.get("extra", [])]
                    prefs["overrides"] = body.get("overrides", {})
                    prefs["new"] = body.get("new", False)
                    prefs["obscurity"] = max(0.0, min(3.0, float(body.get("obscurity",
                                                                          2.0))))
                    PREFS.write_text(json.dumps(prefs, ensure_ascii=False, indent=1), encoding="utf-8")
                    TODAY.unlink(missing_ok=True)
                return self._send(json.dumps({"ok": True}))
        except Exception as e:
            traceback.print_exc()
            return self._send(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        self.send_error(404)

    def log_message(self, *a):
        pass


PORT = int(sp.config("port") or 8080)
URL = f"http://127.0.0.1:{PORT}"
CALLBACK_PORT = urllib.parse.urlparse(sp.REDIRECT).port


def valid_port(value):
    return value.isdigit() and 1024 <= int(value) <= 65535 and int(value) != CALLBACK_PORT


def port_state():
    probe = socket.socket()
    probe.settimeout(0.4)
    try:
        if probe.connect_ex(("127.0.0.1", PORT)) != 0:
            return "free"
    finally:
        probe.close()
    try:
        with urllib.request.urlopen(f"{URL}/api/status", timeout=3) as r:
            return "ours" if "configured" in json.loads(r.read()) else "taken"
    except Exception:
        return "taken"


sp.OPEN_BROWSER = False


def main():
    state = port_state()
    if state == "ours":
        print(f"Already running, opening {URL}")
        webbrowser.open(URL)
        return
    if state == "taken":
        sys.exit(f"Port {PORT} is busy with something else.\n"
                 f"Set a different \"port\" in {sp.CONFIG} and start again.")
    print(URL)
    webbrowser.open(URL)
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
