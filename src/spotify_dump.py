#!/usr/bin/env python3
import base64, hashlib, http.server, json, os, re, secrets, sys, time, urllib.parse, urllib.request, webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
CONFIG = DATA / "config.json"
def config(key):
    stored = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    return os.environ.get(key.upper()) or (stored.get(key) or "").strip() or None


def check_client_id(client_id):
    data = urllib.parse.urlencode({"grant_type": "authorization_code", "code": "probe",
                                   "redirect_uri": REDIRECT, "client_id": client_id,
                                   "code_verifier": "v" * 64}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(
            "https://accounts.spotify.com/api/token", data), timeout=20)
        return True
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read()).get("error") != "invalid_client"
        except Exception:
            return False
    except Exception:
        return None


def save_config(values):
    stored = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    stored.update({k: v.strip() for k, v in values.items()})
    CONFIG.write_text(json.dumps(stored, indent=2) + "\n", encoding="utf-8")


TOKENS = DATA / "tokens.json"
REDIRECT = "http://127.0.0.1:8888/callback"
SCOPES = "user-top-read playlist-read-private user-library-read"
LOGIN_TIMEOUT = 300
PENDING = {"url": None}
OPEN_BROWSER = True



NOISE = r"(remaster|remastered|live|version|edit|mix|mono|stereo|deluxe|bonus|anniversary|feat|ft|with)"


def norm(s):
    s = s.lower()
    s = re.sub(r"\s+-\s+[^-]*\b" + NOISE + r"\b.*$", "", s)
    s = re.sub(r"\s*[(\[][^)\]]*\b" + NOISE + r"\b[^)\]]*[)\]]", "", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return " ".join(s.split())


def key(artist, title):
    return f"{norm(artist)} — {norm(title)}"




TICK = '<svg viewBox="0 0 24 24"><path d="M9 16.2 4.8 12l-1.4 1.4L9 19 21 7l-1.4-1.4z"/></svg>'
CROSS = ('<svg viewBox="0 0 24 24"><path d="M19 6.4 17.6 5 12 10.6 6.4 5 5 6.4 10.6 12 5 17.6'
         ' 6.4 19 12 13.4 17.6 19 19 17.6 13.4 12z"/></svg>')


def app_url():
    return f"http://127.0.0.1:{config('port') or 8080}"


def _result_page(title, note, ok=True):
    template = ROOT / "pages" / "logged_in.html"
    if not template.exists():
        return f"<h1>{title}</h1><p>{note}</p>"
    return (template.read_text(encoding="utf-8")
            .replace("{{title}}", title)
            .replace("{{note}}", note)
            .replace("{{icon}}", TICK if ok else CROSS)
            .replace("{{tint}}", "#1db954" if ok else "#e0455c")
            .replace("{{app}}", app_url())
            .replace("{{fallback}}",
                     f"setTimeout(() => location.href = '{app_url()}', 1800);"
                     if ok else ""))


def _capture_code(url):
    got = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            got.update(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            failed = got.get("error")
            page = _result_page(
                "Spotify said no" if failed else "You're in",
                f"It replied: {failed[0]}." if failed
                else "Reading your library now. Taking you back to the app\u2026",
                ok=not failed)
            self.wfile.write(page.encode())

        def log_message(self, *a):
            pass

    with http.server.HTTPServer(("127.0.0.1", 8888), H) as srv:
        srv.timeout = LOGIN_TIMEOUT
        print("Opening the browser to log in\u2026")
        PENDING["url"] = url
        if OPEN_BROWSER:
            webbrowser.open(url)
        print(f"If it does not open, go to:\n{url}\n")
        try:
            srv.handle_request()
        finally:
            PENDING["url"] = None
    if not got:
        sys.exit("Timed out waiting for the Spotify login.")
    if "error" in got:
        sys.exit(f"Spotify replied: {got['error'][0]}")
    return got["code"][0]


def _post_token(data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request("https://accounts.spotify.com/api/token", body)
    fresh = json.load(urllib.request.urlopen(req))
    tok = json.loads(TOKENS.read_text(encoding="utf-8")) if TOKENS.exists() else {}
    tok.update(fresh)
    tok["expires_at"] = time.time() + fresh["expires_in"] - 60
    TOKENS.write_text(json.dumps(tok, indent=2), encoding="utf-8")
    return tok


def token(client_id):
    tok = json.loads(TOKENS.read_text(encoding="utf-8")) if TOKENS.exists() else None
    if tok and tok["expires_at"] > time.time():
        return tok["access_token"]
    if tok and "refresh_token" in tok:
        try:
            new = _post_token({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                               "client_id": client_id})
            return new["access_token"]
        except urllib.error.HTTPError as e:
            print(f"  refresh token rejected ({e.code}), logging in again", file=sys.stderr)

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(16)
    auth = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT,
        "scope": SCOPES, "code_challenge_method": "S256", "code_challenge": challenge, "state": state})
    code = _capture_code(auth)
    return _post_token({"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                        "client_id": client_id, "code_verifier": verifier})["access_token"]




def get(path, tok, **params):
    url = "https://api.spotify.com/v1/" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok})
    for attempt in range(5):
        try:
            return json.load(urllib.request.urlopen(req))
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            wait = int(e.headers.get("Retry-After", 2))
            print(f"  rate limited, waiting {wait}s")
            time.sleep(wait)
    raise RuntimeError("too many 429s on " + path)


def paged(path, tok, limit=50, cap=2000, **params):
    out, offset = [], 0
    while offset < cap:
        page = get(path, tok, limit=limit, offset=offset, **params)
        out += page["items"]
        if not page.get("next"):
            break
        offset += limit
    return out


def flatten(t):
    if not t or not t.get("id") or not t.get("artists"):
        return None
    return {"id": t["id"], "title": t["name"], "artist": t["artists"][0]["name"],
            "artists": [a["name"] for a in t["artists"]],
            "url": t["external_urls"]["spotify"]}




def main():
    client_id = config("spotify_client_id")
    if not client_id:
        sys.exit("No Spotify client ID yet. Add it in the app's settings.")
    tok = token(client_id)
    me = get("me", tok)
    print(f"Logged in as {me['display_name']}")


    tops = {}
    for rng in ("short_term", "medium_term", "long_term"):
        tops[rng] = [f for f in map(flatten, paged("me/top/tracks", tok, cap=200, time_range=rng)) if f]
        print(f"  top tracks {rng}: {len(tops[rng])}")
    top_artists = {}
    for rng in ("short_term", "medium_term", "long_term"):
        for a in paged("me/top/artists", tok, cap=200, time_range=rng):

            top_artists.setdefault(a["name"], {"name": a["name"], "id": a["id"]})
    distinct = {key(t["artist"], t["title"]) for r in tops.values() for t in r}
    print(f"seeds.json: {len(distinct)} distinct tracks, {len(top_artists)} artists")


    sources = {}

    def note(items, label):
        for t in items:
            sources.setdefault(key(t["artist"], t["title"]), []).append(label)

    tracks = [t for r in tops.values() for t in r]
    note(tracks, "top tracks")
    saved = [f for f in (flatten(i.get("track")) for i in paged("me/tracks", tok)) if f]
    note(saved, "saved")
    tracks += saved
    playlist_tracks = []
    print(f"  saved tracks included, {len(tracks)} total")
    for pl in paged("me/playlists", tok):
        if not pl:
            continue
        try:
            items = paged(f"playlists/{pl['id']}/items", tok, cap=1000)
        except urllib.error.HTTPError as e:
            print(f"  playlist {pl['name']!r}: skipped ({e.code})")
            continue
        got = [f for f in (flatten(i.get("item") or i.get("track")) for i in items) if f]
        note(got, pl["name"])
        playlist_tracks += got
        tracks += got
        print(f"  playlist {pl['name']!r}: {len(got)}")

    seeds = {"tracks": tops, "artists": list(top_artists.values()),
             "saved": saved, "playlists": playlist_tracks}
    (DATA / "seeds.json").write_text(json.dumps(seeds, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  taste sources: {len(saved)} saved, {len(playlist_tracks)} from playlists")

    known_file = DATA / "known.json"
    before = json.loads(known_file.read_text(encoding="utf-8")) if known_file.exists() else {}
    known_tracks = set(before.get("tracks", [])) | {key(t["artist"], t["title"]) for t in tracks}
    known_artists = set(before.get("artists", [])) | {norm(a) for t in tracks for a in t["artists"]}
    known_artists |= {norm(a) for a in top_artists}
    merged = {k: set(v) for k, v in before.get("sources", {}).items()}
    for k, v in sources.items():
        merged.setdefault(k, set()).update(v)
    known_file.write_text(json.dumps(
        {"tracks": sorted(known_tracks), "artists": sorted(known_artists),
         "sources": {k: sorted(v) for k, v in sorted(merged.items())}},
        indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"known.json: {len(known_tracks)} known tracks, {len(known_artists)} known artists")

    if len(distinct) < 100:
        print("\nWarning: fewer than 100 distinct tracks, the seed set is thin.")


def test():
    assert norm("Bohemian Rhapsody - 2011 Remaster") == "bohemian rhapsody"
    assert norm("Hey Jude (feat. Someone)") == "hey jude"
    assert norm("Sultans of Swing [Live]") == "sultans of swing"
    assert norm("Björk") == "björk"
    assert norm("Song  —  Test!") == "song test"
    assert norm("Marquee Moon - Part 1") == "marquee moon part 1"
    assert key("The Beatles", "Hey Jude (feat. X)") == key("the beatles", "Hey Jude")
    print("ok")


if __name__ == "__main__":
    test() if "--test" in sys.argv else main()