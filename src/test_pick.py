import json, random, shutil, tempfile
from pathlib import Path

import spotify_dump as sp

# work on a copy of data/ so the tests never touch the real history and votes
REAL_DATA = sp.DATA
sp.DATA = Path(tempfile.mkdtemp(prefix="daily-rec-test-"))
shutil.copytree(REAL_DATA, sp.DATA, dirs_exist_ok=True)

import app

HERE = app.DATA
FILES = ("today.json", "history.json", "feedback.json")


def reset():
    for f in FILES:
        (HERE / f).unlink(missing_ok=True)


def put_today(artist, seed="Seed", key="k"):
    (HERE / "today.json").write_text(json.dumps({"date": "1970-01-01", "card": {
        "key": key, "title": "T", "artist": artist, "cover": "", "preview": "", "seed": seed}}), encoding="utf-8")


def test_ban_after_three_dislikes():
    reset()
    for i in range(1, 4):
        put_today("Some Artist")
        app.vote("dislike")
        n = json.loads((HERE / "feedback.json").read_text(encoding="utf-8"))["dislikes"]["some artist"]
        assert n == i, (n, i)
    fb = json.loads((HERE / "feedback.json").read_text(encoding="utf-8"))
    assert fb["dislikes"]["some artist"] >= app.DISLIKES_TO_BAN
    assert fb["weights"]["Seed"] == -3


def band_of(value):
    return min((0, 1, 2, 3), key=lambda o: abs(value - app.target_score(o)))


def test_the_score_spans_the_whole_range():
    assert app.score(8_000_000, 40_000_000) > 9.3
    assert app.score(50, 30) < 0.6
    middle = app.score(200_000, 300_000)
    assert 4 < middle < 7, middle


def test_the_score_survives_the_edges():
    assert app.score(0, 0) == 0
    assert app.score(-5, -5) == 0
    assert app.score(10 ** 12, 10 ** 12) == 10, "past the anchors it must clamp, not overshoot"
    assert app.score(4_000, 0) == app.score(4_000, -1)


def test_a_track_lastfm_never_saw_is_not_proof_of_obscurity():
    unheard = app.score(5_000_000, None)
    silent = app.score(5_000_000, 0)
    assert unheard > silent
    assert abs(unheard - 10 * app.normalise(5_000_000, app.ARTIST_SPAN)) < 1e-9


def test_the_slider_lands_where_you_asked():
    cases = [("Ed Sheeran, Shape of You", 4_326_437, 12_465_814, 0),
             ("Kanye West, a 1.4M track", 8_101_603, 1_400_291, 1),
             ("The Weeknd, a 10k deep cut", 5_488_810, 10_000, 2),
             ("a nobody with 5k plays", 5_000, 5_000, 3)]
    for name, listeners, plays, want in cases:
        got = band_of(app.score(listeners, plays))
        assert got == want, f"{name}: expected {want}, got {got}"


def test_fame_and_obscurity_cancel_out():
    weeknd = 5_488_810
    assert app.reachable(weeknd, app.target_score(2), app.SCORE_WINDOW)
    assert not app.reachable(weeknd, app.target_score(3), app.SCORE_WINDOW)
    assert not app.reachable(5_000, app.target_score(0), app.SCORE_WINDOW)


def test_the_search_starts_from_the_right_end():
    tracks = [{"rank": 100}, {"rank": 900}, {"rank": 500}]
    hits = app.track_need(app.target_score(0), 5_000_000)
    deep = app.track_need(app.target_score(3), 5_000_000)
    assert app.search_order(tracks, hits)[0]["rank"] == 900
    assert app.search_order(tracks, deep)[0]["rank"] == 100


def test_chasing_hits_never_pays_for_album_lookups():
    assert app.track_need(app.target_score(0), 5_000_000) > app.DEEP_CUT_NEED
    assert app.track_need(app.target_score(3), 5_000_000) < app.DEEP_CUT_NEED


def test_language_from_tags():
    L = app.language_from_tags
    assert L(["hip-hop", "italian rap", "rap"]) == "italian"
    assert L(["hip-hop", "french", "rap"]) == "french"
    assert L(["instrumental", "italian"]) is None, "instrumental: no language"
    assert L(["ambient", "electronic"]) is None
    assert L(["rock", "alternative"]) is None
    assert L(["k-pop", "pop"]) == "korean"
    assert L(["deutschrap"]) == "german"
    assert L(["british", "indie"]) is None, "a country is not a language"
    assert L(["electronic", "house", "dance", "french"]) is None, "dance scene is not a language"
    assert L(["italo disco", "disco"]) is None
    assert L(["reggaeton", "trap", "latin"]) == "spanish"
    assert L(["hardcore", "portuguese", "melodic hardcore", "italian", "rap"]) is None, \
        "when the tags disagree, admit we do not know"
    assert L(["rap", "italian", "hip-hop", "portuguese", "italian rap"]) == "italian", \
        "a clear majority still wins"
    assert L(["indie pop", "twee", "england", "uk", "italia"]) is None, \
        "where they live says nothing about what they sing in"
    assert L(["american", "hip hop", "usa"]) is None
    assert L(["uk", "italian rap", "italia"]) == "italian", \
        "a language word still counts, the places around it do not"
    assert L(["turkish rap", "hip-hop"]) == "turkish"
    assert L(["mandopop", "pop"]) == "chinese"
    assert L(["dutch house", "house", "edm"]) is None, "a dance scene is not a language"


def test_each_mode_keeps_its_own_genres():
    import threading, urllib.request
    path = HERE / "prefs.json"
    before = path.read_text(encoding="utf-8") if path.exists() else None
    srv = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    post = lambda body: urllib.request.urlopen(urllib.request.Request(
        f"http://127.0.0.1:{srv.server_port}/api/settings", json.dumps(body).encode()))
    try:
        path.write_text(json.dumps({"mode": "library", "states": {"Rock": "in"}}), encoding="utf-8")
        library = {"buckets": [["Rock", 3, "out"]], "extra": []}
        picked = {"buckets": [["Shoegaze", 0, "in"]], "extra": ["Shoegaze"]}
        post(dict(library, mode="library"))
        post(dict(library, mode="manual"))  # switching: the page still shows the library genres
        post(dict(picked, mode="manual"))
        post(dict(picked, mode="library"))  # and back
        p = json.loads(path.read_text(encoding="utf-8"))
        assert p["mode"] == "library"
        assert (p["states"], p["extra"]) == ({"Rock": "out"}, []), p
        assert (p["picked"]["states"], p["picked"]["extra"]) == ({"Shoegaze": "in"}, ["shoegaze"]), p
    finally:
        srv.shutdown()
        if before is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(before, encoding="utf-8")


def test_chosen_languages():
    ok = app.language_allowed
    assert ok("italian", "", None, {"italian", "english"})
    assert not ok("spanish", "in", None, {"italian"}), "a green genre does not beat the language pick"
    assert not ok(app.UNKNOWN, "", None, {"italian"}), "unknown language is skipped once one is picked"
    assert ok(app.UNKNOWN, "", None, ()), "no pick: any language"

    real_info, real_mb = app.artists_info, app.mb_country
    tagged = {"Test Tagged": "italian"}
    app.artists_info = lambda names: {n: {"lang": tagged.get(n, app.UNKNOWN)} for n in names}
    asked = []
    app.mb_country = lambda n: asked.append(n) or {"Test Yank": "US", "Test Swiss": "CH"}.get(n, "")
    try:
        names = ["Test Tagged", "Test Yank", "Test Swiss", "Test Nobody"]
        assert app.sung_in(names) == {"Test Tagged": "italian", "Test Yank": "english",
                                      "Test Swiss": app.UNKNOWN, "Test Nobody": app.UNKNOWN}
        assert "Test Tagged" not in asked, "tags win, no MusicBrainz call"
        asked.clear()
        app.sung_in(names)
        assert not asked, "countries are cached"
    finally:
        app.artists_info, app.mb_country = real_info, real_mb


def test_genres_ranked_by_plays():
    seeds = {"artists": [{"name": "A"}, {"name": "B"}, {"name": "C"}],
             "tracks": {"short_term": [{"artist": "A", "title": f"a{i}"} for i in range(40)]
                        + [{"artist": "B", "title": "b"}, {"artist": "C", "title": "c"}]}}
    plays = app.play_counts(seeds)
    assert plays == {"A": 40, "B": 1, "C": 1}, plays
    buckets = {}
    for a in seeds["artists"]:
        b = "Rap" if a["name"] == "A" else "Pop"
        buckets[b] = buckets.get(b, 0) + max(1, plays.get(a["name"], 0))
    assert buckets == {"Rap": 40, "Pop": 2}
    assert max(buckets, key=buckets.get) == "Rap", "one artist with 40 plays beats two with 1"


def test_focus_noise_ignored():
    for name in ["Pure Sleeping Vibes", "40 Hz Binaural Beats", "Rain Sounds",
                 "Deep Sleep Music", "White Noise Baby", "Ocean Waves for Sleep"]:
        assert app.is_noise(name), name
    for name in ["Radiohead", "Rainbow", "Rain (Beatles cover)", "Sleeping With Sirens",
                 "Fabri Fibra"]:
        assert not app.is_noise(name), name
    assert app.is_noise("Some Artist", ["ambient", "sleep", "chillout"])
    assert not app.is_noise("Some Artist", ["rock", "indie"])


def test_taste_counts_every_library_source():
    seeds = {"artists": [{"name": "Top"}],
             "tracks": {"short_term": [{"artist": "Top", "title": f"t{i}"} for i in range(3)]},
             "saved": [{"artist": "Saved", "title": f"s{i}"} for i in range(2)],
             "playlists": [{"artist": "Play", "title": "p"}]}
    assert app.play_counts(seeds) == {"Top": 3, "Saved": 2, "Play": 1}
    assert app.taste_artists(seeds) == seeds["artists"]


def test_track_overrides():
    seeds = {"artists": [{"name": "A"}],
             "tracks": {"short_term": [{"artist": "A", "title": "One"}]},
             "saved": [{"artist": "B", "title": "Two"}], "playlists": []}
    off = {app.sp.key("B", "Two"): False}
    assert app.play_counts(seeds, off) == {"A": 1}
    assert [a["name"] for a in app.taste_artists(seeds, off)] == ["A"]


def manual_prefs(**kw):
    return {"manual": True, "overrides": {}, "removed": set(), "new": False, "languages": [],
            "extra": ["jazz rap", "shoegaze"], "states": {"Shoegaze": "out", "Jazz": "in"}, **kw}


def test_manual_mode_only_uses_the_chosen_genres():
    prefs = manual_prefs()
    seeds = {"artists": [{"name": "Library Artist"}], "tracks": {}}
    real = app.tag_artists
    app.tag_artists = lambda tag, **kw: [f"{tag} artist {i}" for i in range(10)]
    try:
        names = [a["name"] for a in app.seed_order(seeds, prefs, random.Random(1))]
    finally:
        app.tag_artists = real
    assert names and all(n.startswith("jazz rap artist") for n in names), names
    assert app.taste_tags(seeds, prefs) == ["jazz rap"], "a genre set to never is not dug"
    # a library genre coloured green in the other mode does not leak in, even in the strict pass
    assert not app.bucket_allowed("Jazz", "Jazz", True, prefs, {"Jazz"})
    assert not app.bucket_allowed("Jazz", "Jazz", False, manual_prefs(new=True), {"Jazz"})
    assert app.bucket_allowed("Jazz Rap", "Jazz Rap", True, prefs, set())


def test_like_and_dislike_are_one_switch():
    reset()
    put_today("Some Artist")
    w = lambda: json.loads((HERE / "feedback.json").read_text(encoding="utf-8"))["weights"]["Seed"]
    d = lambda: json.loads((HERE / "feedback.json").read_text(encoding="utf-8"))["dislikes"]

    app.vote("like")
    assert w() == 1 and d() == {}
    app.vote("dislike")
    assert (w(), d()) == (-1, {"some artist": 1}), (w(), d())
    app.vote("like")
    assert (w(), d()) == (1, {}), "switching back must clear the dislike"
    app.vote("like")
    assert w() == 0, "clicking the same one twice turns it off"
    app.vote("dislike")
    app.vote("dislike")
    assert (w(), d()) == (0, {}), "and so does a second dislike"


def test_votes_are_listed_newest_first():
    reset()
    put_today("First", key="a")
    app.vote("like")
    put_today("Second", key="b")
    app.vote("like")
    assert [t["artist"] for t in app.voted()["liked"]] == ["Second", "First"]
    put_today("First", key="a")
    (HERE / "today.json").write_text(json.dumps({"date": "1970-01-01", "card": dict(
        json.loads((HERE / "today.json").read_text(encoding="utf-8"))["card"], liked=True)}),
        encoding="utf-8")
    app.vote("dislike")
    v = app.voted()
    assert [t["artist"] for t in v["liked"]] == ["Second"], "a switched vote leaves the like list"
    assert [t["artist"] for t in v["disliked"]] == ["First"]
    app.vote("dislike")
    assert app.voted() == {"liked": [{"vote": "liked", "title": "T", "artist": "Second", "genre": "",
                                      "seed": "Seed", "key": "b"}],
                           "disliked": []}, "un-voting removes it"


def test_removing_a_vote_from_the_list_undoes_it():
    reset()
    fb = lambda: json.loads((HERE / "feedback.json").read_text(encoding="utf-8"))
    put_today("Old Artist", seed="Old Seed", key="old")
    app.vote("dislike")
    put_today("Today Artist", key="today")
    app.vote("like")
    assert app.unvote("old") is None, "not today's card"
    assert fb()["dislikes"] == {} and fb()["weights"]["Old Seed"] == 0
    card = app.unvote("today")
    assert card["key"] == "today" and not card["liked"], "today's card loses its heart too"
    assert json.loads((HERE / "today.json").read_text(encoding="utf-8"))["card"]["liked"] is False
    assert fb()["weights"]["Seed"] == 0 and app.voted() == {"liked": [], "disliked": []}
    assert app.unvote("missing") is None


def test_neither_moves_on():
    reset()
    for action in ("like", "dislike"):
        put_today("Some Artist")
        card = app.vote(action)
        assert card["artist"] == "Some Artist", f"{action} must stay on the same track"
    assert not (HERE / "history.json").exists(), "voting must not consume tracks"



def test_reroll_stops_at_the_limit():
    reset()
    put_today("Some Artist")
    real_pick = app.pick
    app.pick = lambda avoid_bucket=None: {"key": f"k{random.random()}", "title": "T",
                                          "artist": "Next", "seed": "Seed", "cover": "",
                                          "preview": "", "genre": "G"}
    try:
        for expected in (2, 1, 0):
            card = app.vote("reroll")
            assert card["rerolls_left"] == expected, card
        blocked = app.vote("reroll")
    finally:
        app.pick = real_pick
    assert blocked["error"] == "No rerolls left today.", blocked
    assert blocked["rerolls_left"] == 0


def test_dead_end_keeps_the_card():
    reset()
    put_today("Some Artist")
    real_pick = app.pick
    app.pick = lambda avoid_bucket=None: None
    try:
        card = app.vote("reroll")
        assert card["error"], card
        assert card["artist"] == "Some Artist", "the current card must survive"
        assert (HERE / "today.json").exists(), "today.json must not be deleted"
    finally:
        app.pick = real_pick


def test_a_cache_survives_parallel_writers():
    import threading
    path = HERE / "write_test.json"
    path.unlink(missing_ok=True)
    blew_up = []

    def hammer(n):
        payload = {f"k{i}": "x" * 200 for i in range(400 + n * 900)}
        for _ in range(12):
            try:
                app.write_json(path, payload, ensure_ascii=False)
            except Exception as e:
                blew_up.append(e)

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not blew_up, blew_up[:2]
    json.loads(path.read_text(encoding="utf-8"))
    assert not list(HERE.glob("write_test.json.*.tmp")), "temp files left behind"
    path.unlink()


def test_real_round_network():
    if not (HERE / "config.json").exists() or not (HERE / "seeds.json").exists():
        print("  skipped: no config.json or seeds.json, run the app first")
        return
    reset()
    prefs = json.loads((HERE / "prefs.json").read_text(encoding="utf-8")) if (HERE / "prefs.json").exists() else {}
    prefs.update(mode="library", languages=[])  # the user's own mode and languages may leave nothing to find
    (HERE / "prefs.json").write_text(json.dumps(prefs), encoding="utf-8")
    first = app.today()
    assert first["preview"].startswith("http") and first["cover"], first
    assert app.today()["key"] == first["key"], "same day, same track"
    second = app.vote("reroll")
    assert second["key"] != first["key"], "a reroll must change the track"
    assert second["genre"] != first["genre"], "a reroll must change the genre too"
    seen = json.loads((HERE / "history.json").read_text(encoding="utf-8"))
    assert len(seen) == len(set(seen)) == 2
    assert "why" not in first, "the similar-to line must be gone"
    print(f"  picked: {first['artist']} - {first['title']}  [{first['genre']}]")
    print(f"  rerolled to: {second['artist']} - {second['title']}  [{second['genre']}]")


if __name__ == "__main__":
    test_ban_after_three_dislikes()
    test_the_score_spans_the_whole_range()
    test_the_score_survives_the_edges()
    test_a_track_lastfm_never_saw_is_not_proof_of_obscurity()
    test_the_slider_lands_where_you_asked()
    test_fame_and_obscurity_cancel_out()
    test_the_search_starts_from_the_right_end()
    test_chasing_hits_never_pays_for_album_lookups()
    test_like_and_dislike_are_one_switch()
    test_votes_are_listed_newest_first()
    test_removing_a_vote_from_the_list_undoes_it()
    test_neither_moves_on()
    test_reroll_stops_at_the_limit()
    test_dead_end_keeps_the_card()
    test_language_from_tags()
    test_chosen_languages()
    test_each_mode_keeps_its_own_genres()
    test_genres_ranked_by_plays()
    test_focus_noise_ignored()
    test_taste_counts_every_library_source()
    test_track_overrides()
    test_manual_mode_only_uses_the_chosen_genres()
    test_a_cache_survives_parallel_writers()
    test_real_round_network()
    shutil.rmtree(HERE, ignore_errors=True)
    print("ok")
