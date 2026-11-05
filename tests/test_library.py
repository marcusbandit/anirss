"""Watching in your own player: episode order, the next episode, what is
still downloading, finding a player, and the status split."""

import os

import pytest

from anirss_lib import library
from anirss_lib.errors import NotFoundError, ValidationError


@pytest.mark.parametrize("name,expected", [
    ("[SubsPlease] Sousou no Frieren - 05 (1080p) [ABCD1234].mkv", (None, 5.0)),
    ("Frieren.S02E07.1080p.WEB-DL.mkv", (2, 7.0)),
    ("[Erai-raws] Show 2nd Season - 12v2 [1080p][HEVC].mkv", (None, 12.0)),
    ("Show Ep 3 [720p].mp4", (None, 3.0)),
    ("[Group] Show [05][1080p].mkv", (None, 5.0)),
    ("Show - 12.5 [1080p].mkv", (None, 12.5)),
    ("[Group] Show S2 - 03 [1080p].mkv", (2, 3.0)),
    ("Movie (2023) [1080p].mkv", (None, None)),
    ("[Group] Mob Psycho 100 [05][1080p].mkv", (None, 5.0)),
    ("[Group] 86 Eighty Six [05].mkv", (None, 5.0)),
    ("Mob Psycho 100 - 07 [1080p].mkv", (None, 7.0)),
])
def test_episode_numbers_come_from_the_release_names(name, expected):
    assert library.parse_episode(name) == expected


def _show(tmp_path, files, name="Frieren"):
    folder = tmp_path / "Anime" / name
    for f in files:
        p = folder / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    return folder


def test_episodes_sort_by_number_and_skip_what_is_not_a_finished_video(tmp_path):
    folder = _show(tmp_path, [
        "Frieren - 10 [1080p].mkv", "Frieren - 02 [1080p].mkv", "Frieren - 01 [1080p].mkv",
        "Frieren - 11 [1080p].mkv.!qB", "notes.txt", "Frieren - 03 [1080p].mkv.part",
        "Batch/Frieren - 04 [1080p].mkv",
    ])
    eps = library.episodes_in(str(folder))
    assert [e.number for e in eps] == [1.0, 2.0, 4.0, 10.0]
    skip = {str(folder / "Frieren - 10 [1080p].mkv")}
    assert [e.number for e in library.episodes_in(str(folder), skip=skip)] == [1.0, 2.0, 4.0]
    assert [e.number for e in library.episodes_in(str(folder), skip={str(folder / "Batch")})] \
        == [1.0, 2.0, 10.0]


def test_next_episode_follows_the_last_one_played(tmp_path):
    _show(tmp_path, ["Frieren - 01.mkv", "Frieren - 02.mkv", "Frieren - 03.mkv"])
    (show,) = library.shows(str(tmp_path / "Anime"))
    assert library.next_episode(show).number == 1.0
    library.mark_played(show, show.episodes[0])
    assert library.next_episode(show).number == 2.0
    library.mark_played(show, show.episodes[2])
    assert library.next_episode(show) is None          # caught up
    _show(tmp_path, ["Frieren - 04.mkv"])
    (show,) = library.shows(str(tmp_path / "Anime"))
    assert library.next_episode(show).number == 4.0    # a new one arrived


def test_shows_are_found_by_name(tmp_path):
    for name in ("Frieren", "Frieren Season 2", "Dandadan"):
        _show(tmp_path, ["x - 01.mkv"], name)
    all_shows = library.shows(str(tmp_path / "Anime"))
    assert library.find_show(all_shows, "frieren").name == "Frieren"
    assert library.find_show(all_shows, "danda").name == "Dandadan"
    assert library.find_show(all_shows, "season 2").name == "Frieren Season 2"
    with pytest.raises(ValidationError):
        library.find_show(all_shows, "e")
    with pytest.raises(NotFoundError):
        library.find_show(all_shows, "bocchi")


def test_unfinished_torrents_under_a_folder_are_skipped(tmp_path):
    base = str(tmp_path / "Anime")
    torrents = [
        {"content_path": os.path.join(base, "Frieren", "a.mkv"), "progress": 0.4},
        {"content_path": os.path.join(base, "Frieren", "b.mkv"), "progress": 1.0},
        {"content_path": "/elsewhere/c.mkv", "progress": 0.1},
    ]
    assert library.incomplete_paths(torrents, base) == {os.path.join(base, "Frieren", "a.mkv")}


def test_the_player_is_the_configured_one_then_a_known_one(monkeypatch):
    monkeypatch.setattr(library.shutil, "which",
                        lambda name: f"/usr/bin/{name}" if name in ("vlc", "xdg-open") else None)
    assert library.player_command("mpv --fs") == (["mpv", "--fs"], "mpv")
    assert library.player_command("") == (["/usr/bin/vlc"], "vlc")
    started = []
    monkeypatch.setattr(library.subprocess, "Popen", lambda argv, **kw: started.append(argv))
    assert library.play("/a/b.mkv", "vlc --fullscreen {file} --no-osd") == "vlc"
    assert library.play("/a/b.mkv", "mpv") == "mpv"
    assert started == [["vlc", "--fullscreen", "/a/b.mkv", "--no-osd"], ["mpv", "/a/b.mkv"]]


def test_status_splits_what_is_coming_from_what_finished_lately():
    now = 1_800_000_000
    roots = ["/dl/Anime", "/dl/Movies"]
    torrents = [
        {"name": "a", "save_path": "/dl/Anime/Frieren", "progress": 0.5},
        {"name": "b", "save_path": "/dl/Anime/Frieren", "progress": 0.9},
        {"name": "c", "save_path": "/dl/Anime/Frieren", "progress": 1.0, "completion_on": now - 3600},
        {"name": "d", "save_path": "/dl/Movies", "progress": 1.0, "completion_on": now - 60},
        {"name": "e", "save_path": "/dl/Anime/Old", "progress": 1.0, "completion_on": now - 30 * 86400},
        {"name": "f", "save_path": "/other/Linux ISOs", "progress": 0.2},
    ]
    act = library.activity(torrents, roots, now=now)
    assert [t["name"] for t in act.downloading] == ["b", "a"]
    assert [t["name"] for t in act.finished] == ["d", "c"]
    assert library.show_of(torrents[0], "/dl/Anime") == "Frieren"
    assert library.show_of(torrents[3], "/dl/Anime") == ""


def test_a_symlinked_library_still_skips_an_unfinished_batch_folder(tmp_path):
    real = tmp_path / "real"
    _show(real, ["Show - 01.mkv", "Batch/Show - 02.mkv"], "Show")
    link = tmp_path / "link"
    link.symlink_to(real)
    folder = link / "Anime" / "Show"
    eps = library.episodes_in(str(folder), skip={str(real / "Anime" / "Show" / "Batch")})
    assert [e.number for e in eps] == [1.0]


def test_a_dangling_link_is_left_out_not_a_crash(tmp_path):
    folder = _show(tmp_path, ["Show - 01.mkv"], "Show")
    (folder / "Show - 02.mkv").symlink_to(tmp_path / "gone.mkv")
    (show,) = library.shows(str(tmp_path / "Anime"))
    assert [e.number for e in show.episodes] == [1.0]
    assert library.show_names([str(tmp_path / "Anime"), str(tmp_path / "missing")]) == ["Show"]


def test_labels_come_from_every_show_root():
    t = {"save_path": "/srv/hidden/Show"}
    assert library.show_of(t, ["/dl/Anime", "/srv/hidden"]) == "Show"
    assert library.show_of({"save_path": "/dl/Movies"}, ["/dl/Anime"]) == ""


def test_status_refuses_a_window_of_zero_days(capsys):
    from anirss_lib.cli import library as library_cli
    with pytest.raises(SystemExit):
        library_cli.cmd_status({}, ["--days=0"])
    assert "above 0" in capsys.readouterr().err


def test_play_without_qbittorrent_leaves_out_files_still_being_written(tmp_path, monkeypatch, capsys):
    from anirss_lib.cli import library as library_cli
    folder = _show(tmp_path, ["Show - 01.mkv", "Show - 02.mkv"], "Show")
    old = 1_000_000_000
    os.utime(folder / "Show - 01.mkv", (old, old))      # long finished
    cfg = {"qbittorrent": {"name": "default", "url": "http://localhost:1"},
           "downloads": {"save_base": str(tmp_path / "Anime"),
                         "movie_path": str(tmp_path / "Movies"),
                         "hidden_base": str(tmp_path / "Hidden")},
           "endpoint": [], "player": {"command": "true"}}
    monkeypatch.setattr(library_cli, "_incomplete", lambda cfg, roots: None)
    opened = []
    monkeypatch.setattr(library, "play", lambda path, cmd: opened.append(path) or "true")
    library.mark_played(library.shows(str(tmp_path / "Anime"))[0],
                        library.episodes_in(str(folder))[0])
    library_cli.cmd_play(cfg, ["show"])     # 02 is fresh, so it is left out
    out = capsys.readouterr().out
    assert opened == []
    assert "two minutes" in out and "caught up" in out
