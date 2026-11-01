"""The bearer token Alertmanager presents (src.notifications.alertmanager_token)."""

import os
import stat

from src.notifications.alertmanager_token import ensure_token, read_token, token_matches


def test_the_first_boot_creates_a_token_and_later_boots_keep_it(tmp_path):
    path = tmp_path / "volume" / "webhook_token"

    first = ensure_token(path)
    second = ensure_token(path)

    assert first and len(first) >= 40
    assert second == first
    assert read_token(path) == first


def test_each_install_gets_its_own_token(tmp_path):
    assert ensure_token(tmp_path / "a" / "t") != ensure_token(tmp_path / "b" / "t")


def test_the_file_can_be_read_by_the_other_container_but_not_written(tmp_path):
    path = tmp_path / "webhook_token"
    ensure_token(path)

    if os.name != "nt":  # Windows has no group/other bits to look at
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode & stat.S_IROTH
        assert not mode & (stat.S_IWGRP | stat.S_IWOTH)


def test_no_half_written_file_is_left_behind(tmp_path):
    ensure_token(tmp_path / "webhook_token")

    assert [p.name for p in tmp_path.iterdir()] == ["webhook_token"]


def test_a_blank_token_file_is_made_afresh(tmp_path):
    path = tmp_path / "webhook_token"
    path.write_text("  \n", encoding="utf-8")

    assert read_token(path) is None
    token = ensure_token(path)
    assert token and read_token(path) == token


def test_surrounding_whitespace_in_the_file_is_not_part_of_the_token(tmp_path):
    path = tmp_path / "webhook_token"
    path.write_text("abc123\n", encoding="utf-8")

    assert read_token(path) == "abc123"
    assert token_matches("abc123", path)


def test_no_path_means_the_webhook_is_off(tmp_path):
    assert ensure_token("") is None
    assert read_token("") is None
    assert token_matches("anything", "") is False


def test_a_missing_file_matches_nothing(tmp_path):
    assert read_token(tmp_path / "nope") is None
    assert token_matches("anything", tmp_path / "nope") is False


def test_only_the_exact_token_matches(tmp_path):
    path = tmp_path / "webhook_token"
    token = ensure_token(path)
    assert token

    assert token_matches(token, path) is True
    assert token_matches(token + "x", path) is False
    assert token_matches(token[:-1], path) is False
    assert token_matches("", path) is False
    assert token_matches(None, path) is False


def test_an_unwritable_location_is_reported_not_raised(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a folder is needed", encoding="utf-8")

    assert ensure_token(blocker / "webhook_token") is None
