#!/usr/bin/env python3
import http.client, json, math, os, random, re, socket, sys, threading, time, traceback, urllib.parse, urllib.request, webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import spotify_dump as sp

sys.path.insert(0, str(sp.ROOT))
try:
    import update as up
except ImportError:
    up = None

DATA = sp.DATA
PAGES = sp.ROOT / "pages"
HISTORY = DATA / "history.json"
TODAY = DATA / "today.json"
FEEDBACK = DATA / "feedback.json"
GENRES = DATA / "genres.json"
ARTISTS = DATA / "artists.json"
TRACKS = DATA / "tracks.json"
PREFS = DATA / "prefs.json"
TAGS = DATA / "tags.json"
TOP_GENRES = 5
GENRES_VERSION = 2
DISLIKES_TO_BAN = 3
MAX_REROLLS = 3
UNKNOWN = "unknown"

SCORE_FLOOR = 2.0
ARTIST_SPAN = 4.8
TRACK_SPAN = 5.5
ARTIST_WEIGHT = 0.35
DEEZER_FAN_SCALE = 5
SCORE_WINDOW = 0.8
WINDOW_STEP = 0.6
WINDOW_LIMIT = 1.3
ALBUMS_SAMPLED = 3
DEEP_CUT_NEED = 0.7
TRACKS_SCORED = 25
PLAYS_BATCH = 5
TRACK_CACHE_DAYS = 7
TAG_CACHE_DAYS = 30
ULTRA_LISTENERS = 5000
ULTRA_PLAYS = 10000
ULTRA_TAG_DEPTH = 250
ULTRA_TAG_PAGE = 20
ULTRA_TAG_DIGS = 3
ULTRA_POOL = 24
ULTRA_TAGS = 4
CHART_TARGET = 8.0
CHART_ARTISTS = 40
LANGUAGE_SHARE = 0.05


ACCOUNT_FILES = [sp.TOKENS, DATA / "seeds.json", DATA / "known.json",
                 GENRES, ARTISTS, PREFS, FEEDBACK, HISTORY, TODAY]


def load(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def load_cache(path):
    try:
        return load(path, {})
    except ValueError as e:
        warn(f"unreadable cache {path.name}", e)
        return {}


def write_json(path, data, **kw):
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, **kw), encoding="utf-8")
    tmp.replace(path)


def warn(what, exc):
    print(f"  ! {what}: {type(exc).__name__}: {exc}", file=sys.stderr)


def pmap(fn, items, workers=8):
    items = list(items)
    if not items:
        return []
    with ThreadPoolExecutor(min(workers, len(items))) as ex:
        return list(ex.map(fn, items))


_pool = threading.local()


def http_get(host, path):
    conns = _pool.__dict__.setdefault("conns", {})
    for attempt in (0, 1):
        conn = conns.get(host) or conns.setdefault(host, http.client.HTTPSConnection(host, timeout=20))
        try:
            conn.request("GET", path, headers={"Accept-Language": "en", "User-Agent": "daily-recommendation"})
            r = conn.getresponse()
            body = r.read()
            if r.status >= 400:
                raise urllib.error.HTTPError("https://" + host + path, r.status, r.reason, r.headers, None)
            return json.loads(body)
        except (http.client.HTTPException, OSError) as e:
            conn.close()
            conns.pop(host, None)
            if attempt or isinstance(e, urllib.error.HTTPError):
                raise


def dz(path, **params):
    if params:
        path += "?" + urllib.parse.urlencode(params)
    for attempt in range(5):
        try:
            r = http_get("api.deezer.com", "/" + path)
        except (urllib.error.URLError, OSError, http.client.HTTPException, json.JSONDecodeError) as e:
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
    "italian": "italian", "italiano": "italian",
    "french": "french", "francais": "french", "francophone": "french",
    "spanish": "spanish", "espanol": "spanish", "reggaeton": "spanish",
    "german": "german", "deutsch": "german", "deutschrap": "german",
    "portuguese": "portuguese",
    "japanese": "japanese", "j-pop": "japanese", "j-rock": "japanese",
    "korean": "korean", "k-pop": "korean",
    "english": "english",
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
_lfm_gate = threading.Lock()


def lfm_wait():
    with _lfm_gate:
        time.sleep(max(0, 0.25 - (time.monotonic() - _lfm_last[0])))
        _lfm_last[0] = time.monotonic()


def lfm(**params):
    query = "/2.0/?" + urllib.parse.urlencode(
        {**params, "api_key": lastfm_key(), "format": "json", "autocorrect": 1})
    for attempt in (0, 1):
        lfm_wait()
        try:
            return http_get("ws.audioscrobbler.com", query)
        except Exception:
            if attempt:
                raise


def lastfm_key():
    return sp.config("lastfm_api_key")


def tags_of(name):
    d = lfm(method="artist.getTopTags", artist=name)
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


def stats_of(name):
    d = lfm(method="artist.getInfo", artist=name).get("artist", {}).get("stats", {})
    return int(d.get("listeners") or 0)


def artists_info(names):
    cache = load_cache(ARTISTS)
    key = lastfm_key()
    todo = [n for n in dict.fromkeys(names)
            if n and (n not in cache or "listeners" not in cache[n])]
    if todo and key and _lfm_fail[0] < 5:
        def fetch(n):
            entry = cache.get(n) or {}
            try:
                if "tags" not in entry:
                    entry = {"tags": tags_of(n)}
                entry["listeners"] = stats_of(n)
                _lfm_fail[0] = 0
                return n, entry
            except Exception as e:
                warn(f"Last.fm artist {n!r}", e)
                _lfm_fail[0] += 1
                return n, None

        for n, entry in pmap(fetch, todo):
            if entry is not None:
                cache[n] = entry
        write_json(ARTISTS, cache, ensure_ascii=False, indent=1, sort_keys=True)
    out = {}
    for n in names:
        entry = {"tags": [], "listeners": 0, **cache.get(n, {})}
        entry["lang"] = language_from_tags(entry["tags"]) or UNKNOWN
        entry["noise"] = is_noise(n, entry["tags"])
        out[n] = entry
    return out


_plays = None
_plays_lock = threading.Lock()


def plays_cache():
    global _plays
    if _plays is None:
        _plays = load_cache(TRACKS)
    return _plays


def track_plays(artist, title):
    cache = plays_cache()
    slot = f"{artist}\t{title}".lower()
    hit = cache.get(slot)
    if hit and time.time() - hit[1] < TRACK_CACHE_DAYS * 86400:
        return hit[0]
    if not lastfm_key():
        return None
    try:
        d = lfm(method="track.getInfo", artist=artist, track=title)
    except Exception as e:
        warn(f"Last.fm plays for {artist!r} - {title!r}", e)
        return None
    plays = int(d["track"].get("playcount") or 0) if "track" in d else 0
    with _plays_lock:
        cache[slot] = [plays, time.time()]
        write_json(TRACKS, cache, ensure_ascii=False)
    return plays


def normalise(value, span):
    if not value or value <= 0:
        return 0.0
    return max(0.0, min(1.0, (math.log10(value) - SCORE_FLOOR) / span))


def score(listeners, plays):
    fame = normalise(listeners, ARTIST_SPAN)
    if plays is None:
        return 10 * fame
    return 10 * (ARTIST_WEIGHT * fame + (1 - ARTIST_WEIGHT) * normalise(plays, TRACK_SPAN))


def target_score(obscurity):
    return 10 - 8 * obscurity / 3


def reachable(listeners, target, window):
    low = 10 * ARTIST_WEIGHT * normalise(listeners, ARTIST_SPAN)
    return low - window <= target <= low + 10 * (1 - ARTIST_WEIGHT) + window


def artist_level(info, deezer_fan=0):
    return max(info.get("listeners", 0) or 0, (deezer_fan or 0) * DEEZER_FAN_SCALE)


def lang_of(name):
    return artists_info([name])[name]["lang"]


DEEZER = DATA / "deezer.json"
SEARCHED = DATA / "deezer_artists.json"
_genre_of = None
_searched = None
_search_lock = threading.Lock()
_found = {}


def genre_cache():
    global _genre_of
    if _genre_of is None:
        _genre_of = {int(k): v for k, v in load_cache(DEEZER).items()}
    return _genre_of


def save_genre_cache():
    if _genre_of:
        write_json(DEEZER, _genre_of, indent=0)
GENRE_NAMES = DATA / "genre_names.json"
_gname = {}


def genre_name(gid):
    if not gid:
        return UNKNOWN
    if not _gname:
        _gname.update({int(k): v for k, v in load_cache(GENRE_NAMES).items()})
    if not _gname:
        _gname.update({g["id"]: g["name"] for g in dz("genre")["data"] if g["id"]})
        write_json(GENRE_NAMES, _gname)
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
        _searched = load_cache(SEARCHED)
    return _searched


def find_artist(name, with_fans=False):
    cache = search_cache()
    if name not in cache:
        hit = dz("search/artist", q=name, limit=1).get("data")
        with _search_lock:
            cache[name] = [hit[0]["id"], hit[0].get("nb_fan", 0)] if hit else None
            write_json(SEARCHED, cache, ensure_ascii=False, indent=0)
    got = cache[name]
    if isinstance(got, int):
        got = [got, 0]
    if with_fans:
        return got or [None, 0]
    return got[0] if got else None


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
    cached["version"] = GENRES_VERSION
    GENRES.write_text(json.dumps(cached, ensure_ascii=False, indent=1), encoding="utf-8")
    save_genre_cache()
    return data


def genres_for(source, overrides=None):
    cached = load(GENRES, {})
    if cached.get("version") == GENRES_VERSION and cached.get(source):
        return cached[source]
    return compute_genres(source, overrides)


def language_of_bucket(bucket):
    return bucket.split(" \u00b7 ")[1] if " \u00b7 " in bucket else UNKNOWN


def spoken_languages(genres, prefs):
    heard = {}
    for bucket, n in genres.get("counts", {}).items():
        if bucket in prefs["removed"] or prefs["states"].get(bucket) == "out":
            continue
        lang = language_of_bucket(bucket)
        heard[lang] = heard.get(lang, 0) + n
    total = sum(heard.values())
    if not total:
        return None
    return {UNKNOWN} | {lang for lang, n in heard.items() if n / total >= LANGUAGE_SHARE}


def language_allowed(lang, state, spoken):
    return spoken is None or lang in spoken or state == "in"


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


def album_tracks(deezer_id, rnd):
    try:
        albums = [a for a in dz(f"artist/{deezer_id}/albums", limit=50).get("data", [])
                  if a.get("record_type") == "album"]
    except Exception as e:
        warn(f"Deezer albums for {deezer_id}", e)
        return []
    rnd.shuffle(albums)

    def tracks_of(a):
        try:
            return [dict(t, album=a) for t in
                    dz(f"album/{a['id']}/tracks", limit=100).get("data", [])]
        except Exception as e:
            warn(f"Deezer album {a['id']}", e)
            return []

    return [t for got in pmap(tracks_of, albums[:ALBUMS_SAMPLED], 4) for t in got]


_top = {}


def top_tracks(deezer_id):
    if deezer_id not in _top:
        _top[deezer_id] = dz(f"artist/{deezer_id}/top", limit=10).get("data", [])
    return _top[deezer_id]


def track_need(target, listeners):
    fame = normalise(listeners, ARTIST_SPAN)
    return (target / 10 - ARTIST_WEIGHT * fame) / (1 - ARTIST_WEIGHT)


def search_order(tracks, need):
    return sorted(tracks, key=lambda t: t.get("rank", 0), reverse=need >= 0.5)


def pickable(track, cand, known_t, seen):
    if sp.norm(track["artist"]["name"]) != sp.norm(cand["name"]):
        return None
    k = sp.key(track["artist"]["name"], track["title"])
    if k in known_t or k in seen or not track.get("preview"):
        return None
    if FOCUS_NOISE.search(track["title"]):
        return None
    return k


def playable(cand, known_t, seen, rnd, listeners, target, window, ultra):
    need = track_need(target, listeners)
    passes = (False,) if need > DEEP_CUT_NEED and not ultra else (False, True)
    for deeper in passes:
        tracks = album_tracks(cand["id"], rnd) if deeper else top_tracks(cand["id"])
        good = [(k, t) for t in search_order(tracks, need)[:TRACKS_SCORED]
                if (k := pickable(t, cand, known_t, seen))]
        for i in range(0, len(good), PLAYS_BATCH):
            batch = good[i:i + PLAYS_BATCH]
            pmap(lambda kt: track_plays(kt[1]["artist"]["name"], kt[1]["title"]), batch)
            for k, t in batch:
                plays = track_plays(t["artist"]["name"], t["title"])
                if plays is None:
                    continue
                if ultra:
                    if listeners < ULTRA_LISTENERS and (plays or 0) < ULTRA_PLAYS:
                        return k, t
                elif abs(score(listeners, plays) - target) <= window:
                    return k, t
    return None, None


def taste_tags(seeds, prefs):
    heard = artists_info([a["name"] for a in
                          taste_artists(seeds, prefs["source"], prefs["overrides"])])
    counts = {}
    for entry in heard.values():
        for t in entry.get("tags", [])[:4]:
            if t not in NON_GENRE and t not in NOISE_TAGS:
                counts[t.replace("-", " ")] = counts.get(t.replace("-", " "), 0) + 1
    return sorted(counts, key=counts.get, reverse=True)[:ULTRA_TAGS]


def obscure_pool(seeds, prefs, rnd, page=ULTRA_TAG_PAGE, want=ULTRA_POOL):
    def tail_of(tag):
        deep = (tag_artists(tag, limit=ULTRA_TAG_DEPTH, page=page)
                or tag_artists(tag, limit=ULTRA_TAG_DEPTH))
        return deep[-want * 4:]

    names = [n for tail in pmap(tail_of, taste_tags(seeds, prefs)) for n in tail]
    names = list(dict.fromkeys(names))
    rnd.shuffle(names)
    names = names[:want]
    found = dict(zip(names, pmap(lambda n: find_artist(n, True), names, 8)))
    names = [n for n in names
             if found[n][0] and found[n][1] * DEEZER_FAN_SCALE < ULTRA_LISTENERS]
    heard = artists_info(names)
    pool = [{"name": n, "id": found[n][0], "level": artist_level(heard[n], found[n][1])}
            for n in names]
    return [a for a in pool if a["level"] < ULTRA_LISTENERS]


def chart_pool(rnd):
    try:
        pool = dz("chart/0/artists", limit=100).get("data", [])
    except Exception as e:
        warn("Deezer chart", e)
        return []
    rnd.shuffle(pool)
    pool = pool[:CHART_ARTISTS]
    infos = artists_info([a["name"] for a in pool])
    for a in pool:
        a["level"] = artist_level(infos[a["name"]])
    return pool


def with_feats(track):
    try:
        guests = dz(f"track/{track['id']}").get("contributors", [])
    except Exception as e:
        warn(f"Deezer contributors for {track.get('id')}", e)
        return track["title"]
    title, main = track["title"], sp.norm(track["artist"]["name"])
    others = [c["name"] for c in guests
              if sp.norm(c["name"]) != main and c["name"].lower() not in title.lower()]
    return f"{title} (feat. {', '.join(others)})" if others else title


def related_to(seed, target, window, rnd, ultra):
    found = find_artist(seed["name"])
    if not found:
        return []
    pool = related_of(found)
    infos = artists_info([a["name"] for a in pool])
    keep = []
    for a in pool:
        a["level"] = artist_level(infos[a["name"]], a.get("nb_fan", 0))
        wanted = (a["level"] < ULTRA_LISTENERS if ultra
                  else reachable(a["level"], target, window))
        if wanted:
            keep.append(a)
    rnd.shuffle(keep)
    return keep[:6]


def read_prefs():
    stored = load(PREFS, {})
    source = stored.get("source", "all")
    genres = genres_for(source, stored.get("overrides", {}))
    return {"source": source, "overrides": stored.get("overrides", {}),
            "states": stored.get("states", {b: "in" for b in genres["top"]}),
            "removed": set(stored.get("removed", [])),
            "extra": [g.lower() for g in stored.get("extra", [])],
            "obscurity": float(stored.get("obscurity", 2.0)),
            "ultra": bool(stored.get("ultra", False)),
            "new": stored.get("new", False)}, genres


def pick(avoid_bucket=None):
    seeds = json.loads((DATA / "seeds.json").read_text(encoding="utf-8"))
    known = load(DATA / "known.json", {})
    known_a, known_t = set(known.get("artists", [])), set(known.get("tracks", []))
    seen = set(load(HISTORY, []))
    seen_artists = {k.split(" — ")[0] for k in seen}
    prefs, genres = read_prefs()
    detected = set(genres["counts"])
    spoken = spoken_languages(genres, prefs)
    dislikes = load(FEEDBACK, {}).get("dislikes", {})
    banned = {a for a, n in dislikes.items() if n >= DISLIKES_TO_BAN}
    off_limits = known_a | banned | seen_artists

    rnd = random.Random(date.today().isoformat())
    _top.clear()
    order = seed_order(seeds, prefs, rnd)
    seed_noise = artists_info([a["name"] for a in order])
    target = target_score(prefs["obscurity"])
    ultra = prefs["ultra"]
    charts = {"name": "Deezer charts"}
    deep = {"name": "Last.fm tail"}
    boost = chart_pool(rnd) if target > CHART_TARGET and not ultra else []
    page = ULTRA_TAG_PAGE
    tail = obscure_pool(seeds, prefs, rnd) if ultra else []

    def attempt(window):
        rounds = [(s, deep) for s in (True, False)] if tail else []
        rounds += [(True, charts)] if boost else []
        rounds += [(s, a) for s in (True, False) for a in order]
        for strict, seed in rounds:
            if seed is deep:
                related = tail
            elif seed is charts:
                related = [a for a in boost if reachable(a["level"], target, window)]
            elif seed_noise[seed["name"]]["noise"]:
                continue
            else:
                related = related_to(seed, target, window, rnd, ultra)
            infos = artists_info([c["name"] for c in related])
            fresh = [c for c in related if sp.norm(c["name"]) not in off_limits
                     and not infos[c["name"]]["noise"]]
            pmap(lambda c: artist_genre(c["id"]), fresh, 4)

            keep = []
            for cand in fresh:
                bucket, state_key = bucket_for(cand, infos[cand["name"]], prefs["extra"])
                if bucket == avoid_bucket:
                    continue
                if not language_allowed(infos[cand["name"]]["lang"],
                                        prefs["states"].get(state_key), spoken):
                    continue
                if bucket_allowed(bucket, state_key, strict, prefs, detected):
                    keep.append((cand, bucket))

            pmap(lambda cb: top_tracks(cb[0]["id"]), keep, 4)
            for cand, bucket in keep:
                key, track = playable(cand, known_t, seen, rnd,
                                      cand["level"], target, window, ultra)
                if key:
                    return key, track, seed, bucket
        return None

    window, got = SCORE_WINDOW, None
    while True:
        got = attempt(window)
        if got:
            break
        if ultra:
            if page >= ULTRA_TAG_PAGE + ULTRA_TAG_DIGS:
                break
            page += 1
            tail = obscure_pool(seeds, prefs, rnd, page)
            print(f"  ultra: nothing yet, digging to tag page {page} ({len(tail)} artists)",
                  file=sys.stderr)
            continue
        if window >= WINDOW_LIMIT:
            break
        window = min(WINDOW_LIMIT, window + WINDOW_STEP)
        print(f"  nothing within reach, widening the window to {window:.1f}", file=sys.stderr)
    save_genre_cache()
    if not got:
        return None
    key, track, seed, bucket = got
    return {"key": key, "title": with_feats(track), "artist": track["artist"]["name"],
            "cover": track["album"].get("cover_xl") or track["album"].get("cover_big"),
            "preview": track["preview"], "seed": seed["name"], "genre": bucket}


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
        return today() if action == "reroll" else {"error": "Nothing to vote on."}
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


_tag_artists = None
_tags_lock = threading.Lock()


def tag_cache():
    global _tag_artists
    if _tag_artists is None:
        _tag_artists = load_cache(TAGS)
    return _tag_artists


def tag_artists(tag, limit=30, page=1):
    cache = tag_cache()
    slot = f"{tag}\t{limit}\t{page}"
    hit = cache.get(slot)
    if hit and time.time() - hit[1] < TAG_CACHE_DAYS * 86400:
        return hit[0]
    try:
        d = lfm(method="tag.getTopArtists", tag=tag, limit=limit, page=page)
        names = [a["name"] for a in d.get("topartists", {}).get("artist", [])]
    except Exception as e:
        warn(f"Last.fm artists for tag {tag!r}", e)
        return []
    if names:
        with _tags_lock:
            cache[slot] = [names, time.time()]
            write_json(TAGS, cache, ensure_ascii=False)
    return names


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
            "ultra": bool(prefs.get("ultra", False)),
            "removed": sorted(removed), "overrides": overrides,
            "buckets": buckets, "tracks": rows}


UPDATE = {"state": "idle", "percent": 0, "available": None, "tag": None,
          "installed": "--restarted" in sys.argv}
_release = [None]


def update_status():
    if UPDATE["available"] is None:
        UPDATE["available"] = False
        if up and not up.cloned():
            try:
                _release[0] = up.latest()
                tag = _release[0].get("tag_name")
                UPDATE["tag"] = tag
                UPDATE["available"] = bool(tag) and tag != up.installed()
            except Exception as e:
                warn("update check", e)
    return UPDATE


def restart():
    os.execv(sys.executable, [sys.executable, str(sp.HERE / "app.py"), "--restarted"])


def update_run():
    def go():
        try:
            up.apply(_release[0], lambda got, total: UPDATE.update(
                percent=int(100 * got / total) if total else 0))
            UPDATE.update(state="done", percent=100, available=False)
            threading.Timer(1.0, restart).start()
        except Exception as e:
            warn("update", e)
            UPDATE.update(state="failed", percent=0)

    UPDATE.update(state="downloading", percent=0)
    threading.Thread(target=go, daemon=True).start()


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
                         "ultra": bool(saved.get("ultra", False)),
                         "obscurity": float(saved.get("obscurity", 2.0))}))
                g = genres_for(source)
                same = saved.get("source", "all") == source
                states = (saved.get("states") if same else None) or {
                    b: ("in" if b in g["top"] else "") for b in g["counts"]}
                return self._send(json.dumps(
                    {"buckets": list(g["counts"].items()),
                     "states": {b: states.get(b, "") for b in g["counts"]},
                     "new": saved.get("new", False), "source": source,
                     "ultra": bool(saved.get("ultra", False)),
                     "obscurity": float(saved.get("obscurity", 2.0))},
                    ensure_ascii=False))
            if self.path == "/api/dump":
                return self._send(json.dumps(sp.PROGRESS))
            if self.path == "/api/update":
                status = json.dumps(update_status())
                UPDATE["installed"] = False
                return self._send(status)
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
            if self.path == "/api/update":
                if _release[0] and UPDATE["state"] != "downloading":
                    update_run()
                return self._send(json.dumps(UPDATE))
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
                    prefs["ultra"] = bool(body.get("ultra", False))
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
    if "--restarted" not in sys.argv:
        webbrowser.open(URL)
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
