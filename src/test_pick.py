import json, pathlib, random, app

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


def test_taste_source():
    seeds = {"artists": [{"name": "Top"}],
             "tracks": {"short_term": [{"artist": "Top", "title": f"t{i}"} for i in range(3)]},
             "saved": [{"artist": "Saved", "title": f"s{i}"} for i in range(2)],
             "playlists": [{"artist": "Play", "title": "p"}]}
    assert app.play_counts(seeds, "all") == {"Top": 3, "Saved": 2, "Play": 1}
    assert app.play_counts(seeds, "saved") == {"Saved": 2}
    assert [a["name"] for a in app.taste_artists(seeds, "saved")] == ["Saved"]
    assert app.taste_artists(seeds, "all") == seeds["artists"]


def test_track_overrides():
    seeds = {"artists": [{"name": "A"}],
             "tracks": {"short_term": [{"artist": "A", "title": "One"}]},
             "saved": [{"artist": "B", "title": "Two"}], "playlists": []}
    assert app.play_counts(seeds, "all") == {"A": 1, "B": 1}
    assert app.play_counts(seeds, "saved") == {"B": 1}
    off = {app.sp.key("B", "Two"): False}
    assert app.play_counts(seeds, "saved", off) == {}
    on = {app.sp.key("A", "One"): True}
    assert app.play_counts(seeds, "saved", on) == {"A": 1, "B": 1}


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


def test_real_round_network():
    if not (HERE / "config.json").exists() or not (HERE / "seeds.json").exists():
        print("  skipped: no config.json or seeds.json, run the app first")
        return
    reset()
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
    test_neither_moves_on()
    test_reroll_stops_at_the_limit()
    test_dead_end_keeps_the_card()
    test_language_from_tags()
    test_genres_ranked_by_plays()
    test_focus_noise_ignored()
    test_taste_source()
    test_track_overrides()
    test_real_round_network()
    reset()
    print("ok")
