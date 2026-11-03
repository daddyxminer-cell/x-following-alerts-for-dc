"""Offline tests: no real X requests or Discord messages."""
import copy
import json
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import follower_watch as fw


def user(uid, name=None):
    return {"id": str(uid), "username": name or f"user{uid}", "name": "Example",
            "description": "A bio", "profile_image_url": "https://example.com/avatar.png"}


class Source:
    def __init__(self, users=None):
        self.users = users if users is not None else [user(1), user(2)]
        self.modes = []
        self.fail = False

    def get_user(self, name):
        return user(100, name)

    def get_page(self, uid, mode, size, token=None):
        self.modes.append(mode)
        if self.fail:
            raise fw.SourceError("Unavailable")
        offset = int(token or 0)
        data = self.users[offset:offset + size]
        meta = {"result_count": len(data)}
        if offset + size < len(self.users):
            meta["next_token"] = str(offset + size)
        return {"data": data, "meta": meta}


class Notifier:
    def __init__(self):
        self.sent = []
        self.fail = False

    def send(self, webhook, embed):
        if self.fail:
            raise RuntimeError("Discord down")
        self.sent.append(copy.deepcopy(embed))


@pytest.fixture
def setup():
    settings = fw.Settings(page_size=2, announce_new_watch=False)
    state = {"version": 2, "accounts": {}}
    source, notifier = Source(), Notifier()
    def run():
        return fw.process_account(fw.AccountConfig("elon"), settings, state, source,
                                  notifier, "hook")
    return settings, state, source, notifier, run


def test_baseline_then_new_following_even_after_known_id(setup):
    settings, state, source, notifier, run = setup
    assert run() == 0
    assert notifier.sent == []
    source.users = [user(2), user(3), user(1), user(4)]
    assert run() == 2
    assert source.modes == ["following"] * 3
    assert [e["fields"][0]["value"] for e in notifier.sent] == ["3", "4"]
    assert "@elon followed" in notifier.sent[0]["author"]["name"]
    assert run() == 0
    assert len(notifier.sent) == 2


def test_reordering_and_renaming_do_not_notify(setup):
    _, _, source, notifier, run = setup
    run()
    source.users = [user(2, "renamed"), user(1)]
    assert run() == 0
    assert notifier.sent == []


def test_unfollow_then_refollow(setup):
    _, _, source, notifier, run = setup
    run()
    source.users = [user(1)]
    assert run() == 0
    source.users = [user(2), user(1)]
    assert run() == 1
    assert len(notifier.sent) == 1


def test_modes_have_separate_baselines(setup):
    settings, state, source, notifier, run = setup
    run()
    settings.watch_mode = "followers"
    assert run() == 0
    source.users.append(user(3))
    assert run() == 1
    assert "New follower of @elon" == notifier.sent[0]["author"]["name"]
    assert set(state["accounts"]) == {"following:100", "followers:100"}


def test_incomplete_scan_does_not_change_snapshot(setup):
    settings, state, source, notifier, run = setup
    run()
    before = copy.deepcopy(state)
    settings.max_pages_per_run = 1
    source.users.append(user(3))
    with pytest.raises(fw.SourceError, match="max_pages"):
        run()
    assert state == before
    assert notifier.sent == []


def test_failure_then_retry_retains_event(setup):
    _, state, source, notifier, run = setup
    run()
    source.users.append(user(3))
    notifier.fail = True
    with pytest.raises(RuntimeError):
        run()
    record = state["accounts"]["following:100"]
    assert len(record["pending"]) == 1
    notifier.fail = False
    # Pending notifications can be delivered before a failed next list fetch.
    source.fail = True
    with pytest.raises(fw.SourceError):
        run()
    assert len(notifier.sent) == 1
    assert record["pending"] == []


def test_overflow_is_queued_not_dropped(setup):
    settings, state, source, notifier, run = setup
    run()
    settings.max_notifications_per_run = 1
    source.users.extend([user(3), user(4), user(5)])
    assert run() == 3
    assert len(state["accounts"]["following:100"]["pending"]) == 2
    run()
    run()
    assert len(notifier.sent) == 3


def test_duplicate_ids_in_pages_only_notify_once(setup):
    _, _, source, notifier, run = setup
    run()
    source.users = [user(1), user(3), user(3), user(2)]
    assert run() == 1
    assert len(notifier.sent) == 1


def test_empty_baseline_then_addition(setup):
    _, _, source, _, run = setup
    source.users = []
    run()
    source.users = [user(1)]
    assert run() == 1


def test_announcement_failure_does_not_initialize(setup):
    settings, state, _, notifier, run = setup
    settings.announce_new_watch = True
    notifier.fail = True
    with pytest.raises(RuntimeError):
        run()
    assert state["accounts"] == {}


@pytest.mark.parametrize("body", [
    {}, {"meta": {"result_count": 1}},
    {"meta": {"result_count": 0}, "errors": [{"title": "partial"}]},
    {"meta": {"result_count": 1}, "data": [{"id": "bad"}]},
])
def test_bad_snapshot_rejected(body):
    class Bad(Source):
        def get_page(self, *args):
            return body
    with pytest.raises(fw.SourceError):
        fw.fetch_snapshot(Bad(), "1", fw.Settings())


def test_repeated_pagination_token():
    class Bad(Source):
        def get_page(self, *args):
            return {"data": [], "meta": {"result_count": 0, "next_token": "again"}}
    with pytest.raises(fw.SourceError, match="pagination"):
        fw.fetch_snapshot(Bad(), "1", fw.Settings())


def test_config_accounts_and_override(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text("accounts: ['@elon', 'https://x.com/elon', {username: nasa, enabled: false}]\n")
    settings = fw.load_settings(path)
    assert [a.username for a in settings.accounts] == ["elon", "nasa"]
    assert settings.accounts[1].enabled is False
    assert [a.username for a in fw.load_settings(path, "OpenAI, @SpaceX").accounts] == ["OpenAI", "SpaceX"]


@pytest.mark.parametrize("text", ["page_size: 0", "watch_mode: both", "accounts: elon",
    "accounts: ['bad/name']", "announce_new_watch: 'false'", "max_notifications_per_run: -1"])
def test_invalid_config(tmp_path, text):
    path = tmp_path / "config.yml"
    path.write_text(text)
    with pytest.raises(ValueError):
        fw.load_settings(path)


def test_state_roundtrip_and_invalid(tmp_path):
    path = tmp_path / "state.json"
    state = fw.load_state(path)
    fw.save_state(path, state)
    assert fw.load_state(path) == state
    path.write_text("{broken")
    with pytest.raises(ValueError, match="restore"):
        fw.load_state(path)


def test_old_draft_state_is_not_reused(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"accounts":{"elon":{"recent_follower_ids":["1"]}}}')
    assert fw.load_state(path) == {"version": 2, "accounts": {}}


def test_embed_identifies_both_accounts():
    embed = fw.build_embed(user(9, "alice"), user(100, "elon"), "following")
    assert embed["url"] == "https://x.com/alice"
    assert "@alice" in embed["title"]
    assert embed["fields"][0]["value"] == "9"
    assert embed["fields"][1]["value"] == "@elon (ID 100)"


class Response:
    def __init__(self, status, body):
        self.status_code, self.body = status, body
    def json(self):
        return self.body


class Session:
    def __init__(self, responses):
        self.responses, self.calls, self.headers = iter(responses), [], {}
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result
    post = get


def test_discord_retries_and_disables_mentions(monkeypatch):
    monkeypatch.setattr(fw.time, "sleep", lambda _: None)
    session = Session([Response(429, {"retry_after": 0}), Response(200, {})])
    fw.DiscordNotifier(session=session).send("hook", {"title": "Hi @everyone"})
    assert len(session.calls) == 2
    assert session.calls[1][1]["json"]["allowed_mentions"] == {"parse": []}
    assert session.calls[1][1]["params"] == {"wait": "true"}


def test_discord_network_errors_do_not_expose_webhook():
    session = Session([requests.ConnectionError("SECRET_WEBHOOK")])
    with pytest.raises(RuntimeError) as error:
        fw.DiscordNotifier(session=session).send("SECRET_WEBHOOK", {})
    assert "SECRET_WEBHOOK" not in str(error.value)


def test_webhook_validation(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.com/stolen")
    with pytest.raises(ValueError):
        fw.resolve_webhook(fw.AccountConfig("elon"), False)
    monkeypatch.setenv("CUSTOM_HOOK", "https://discord.com/api/webhooks/123/token")
    assert fw.resolve_webhook(fw.AccountConfig("elon", webhook_env="CUSTOM_HOOK"), False).endswith("token")


def test_main_baseline_new_follow_and_dry_run(tmp_path, monkeypatch):
    config = tmp_path / "config.yml"
    config.write_text("accounts: [elon]\n")
    path = tmp_path / "state.json"
    source, notifier = Source(), Notifier()
    monkeypatch.setenv("CONFIG_PATH", str(config))
    monkeypatch.setenv("STATE_PATH", str(path))
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/123/token")
    monkeypatch.delenv("WATCH_ACCOUNTS", raising=False)
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.setattr(fw, "load_cookies_from_env", lambda: {"auth_token": "fake", "ct0": "fake"})
    monkeypatch.setattr(fw, "XSource", lambda _: source)
    monkeypatch.setattr(fw, "DiscordNotifier", lambda _: notifier)
    assert fw.main() == 0
    saved = path.read_bytes()
    source.users.append(user(3))
    monkeypatch.setenv("DRY_RUN", "1")
    assert fw.main() == 0
    assert path.read_bytes() == saved
    monkeypatch.delenv("DRY_RUN")
    assert fw.main() == 0
    assert json.loads(path.read_text())["accounts"]["following:100"]["ids"] == ["1", "2", "3"]
    source.fail = True
    assert fw.main() == 1


# Cookie adapter tests use twifork's real Result class, not live X requests.
from types import SimpleNamespace
from twikit.utils import Result


def tw_user(uid, name=None):
    return SimpleNamespace(id=str(uid), screen_name=name or f"user{uid}", name="Example",
                           description="Bio", protected=False)


class CookieClient:
    def __init__(self, page):
        self.page, self.calls = page, []

    def set_cookies(self, cookies):
        self.cookies = cookies

    async def get_user_by_screen_name(self, name):
        return tw_user(100, name)

    async def get_user_following(self, uid, count):
        self.calls.append(("following", uid, count))
        return self.page

    async def get_user_followers(self, uid, count):
        self.calls.append(("followers", uid, count))
        return self.page


def test_cookie_source_keeps_buffered_users_and_uses_following():
    client = CookieClient(Result([tw_user(1)], overflow=[tw_user(2), tw_user(3)], page_size=1))
    src = fw.XSource({"auth_token": "fake", "ct0": "fake"}, client, page_delay=0)
    try:
        assert src.get_user("elon")["username"] == "elon"
        snapshot = fw.fetch_snapshot(src, "100", fw.Settings(page_size=1))
        assert set(snapshot) == {"1", "2", "3"}
        assert client.calls == [("following", "100", 1)]
        fw.fetch_snapshot(src, "100", fw.Settings(watch_mode="followers", page_size=1))
        assert client.calls[-1][0] == "followers"
    finally:
        src.close()


def test_cookie_source_fetches_all_pages_with_shared_cursor():
    calls = []
    async def next_page():
        calls.append(1)
        return Result([tw_user(4)])
    page = Result([tw_user(1)], next_page, "cursor", overflow=[tw_user(2), tw_user(3)], page_size=1)
    src = fw.XSource({}, CookieClient(page), page_delay=0)
    try:
        assert set(fw.fetch_snapshot(src, "100", fw.Settings(page_size=1))) == {"1", "2", "3", "4"}
        assert calls == [1]
    finally:
        src.close()


def test_cookie_source_repeated_nonempty_page_fails():
    async def same():
        return page
    page = Result([tw_user(1)], same, "cursor")
    src = fw.XSource({}, CookieClient(page), page_delay=0)
    try:
        with pytest.raises(fw.SourceError, match="Repeated"):
            fw.fetch_snapshot(src, "100", fw.Settings())
    finally:
        src.close()


@pytest.mark.parametrize("kind,expected", [
    ("Unauthorized", fw.SessionError), ("InvalidSession", fw.SessionError),
    ("AccountLocked", fw.SessionError), ("AccountSuspended", fw.SessionError),
    ("TooManyRequests", fw.RateLimited), ("Forbidden", fw.SourceError),
    ("UserUnavailable", fw.SourceError), ("ServerError", fw.SourceError),
])
def test_cookie_errors_are_safe(kind, expected):
    from twikit import errors
    class Bad(CookieClient):
        async def get_user_by_screen_name(self, name):
            raise getattr(errors, kind)("SECRET_COOKIE")
    src = fw.XSource({}, Bad(Result([])))
    try:
        with pytest.raises(expected) as error:
            src.get_user("elon")
        assert "SECRET_COOKIE" not in str(error.value)
    finally:
        src.close()


@pytest.mark.parametrize("body", [{}, {"data": {}}, {"errors": [{"message": "partial"}]},
                                   {"entries": None}])
def test_malformed_raw_timeline_rejected(body):
    with pytest.raises(fw.SourceError):
        fw.validate_timeline(body)


def test_empty_raw_timeline_is_valid():
    fw.validate_timeline({"data": {"timeline": {"instructions": [{"entries": []}]}}})


def test_false_empty_snapshot_preserves_baseline(setup):
    _, state, source, _, run = setup
    run()
    before = copy.deepcopy(state)
    source.get_user = lambda name: {**user(100, name), "following_count": 2}
    source.users = []
    with pytest.raises(fw.SourceError, match="nonzero"):
        run()
    assert state == before


def test_cookie_loading_and_export(monkeypatch):
    monkeypatch.delenv("X_COOKIES", raising=False)
    monkeypatch.setenv("X_AUTH_TOKEN", " auth ")
    monkeypatch.setenv("X_CT0", " csrf ")
    assert fw.load_cookies_from_env() == {"auth_token": "auth", "ct0": "csrf"}
    monkeypatch.setenv("X_COOKIES", json.dumps([
        {"name": "auth_token", "value": "a"}, {"name": "ct0", "value": "c"}]))
    assert fw.load_cookies_from_env() == {"auth_token": "a", "ct0": "c"}


@pytest.mark.parametrize("raw", ["", "bad JSON", "null", "[]", '[1]', '{"auth_token": "DO_NOT_LEAK_COOKIE"}',
                                 '{"auth_token": 123, "ct0": "DO_NOT_LEAK_COOKIE"}'])
def test_missing_or_invalid_cookies_do_not_leak(monkeypatch, raw):
    monkeypatch.delenv("X_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("X_CT0", raising=False)
    monkeypatch.setenv("X_COOKIES", raw)
    with pytest.raises(ValueError) as error:
        fw.load_cookies_from_env()
    assert "DO_NOT_LEAK_COOKIE" not in str(error.value)


def test_multiple_accounts_independent_following_notifications():
    class Multi(Source):
        def __init__(self):
            self.lists = {"100": [user(1)], "200": [user(2)]}
        def get_user(self, name):
            return user(100 if name == "elon" else 200, name)
        def get_page(self, uid, mode, size, token=None):
            assert mode == "following"
            data = self.lists[uid]
            return {"data": data, "meta": {"result_count": len(data)}}
    src, notifier = Multi(), Notifier()
    state = {"version": 2, "accounts": {}}
    settings = fw.Settings(announce_new_watch=False)
    def run_all():
        for name in ("elon", "nasa"):
            fw.process_account(fw.AccountConfig(name), settings, state, src, notifier, "hook")
    run_all()
    assert notifier.sent == []
    src.lists["100"].append(user(99, "tesla"))
    run_all()
    assert [e["author"]["name"] for e in notifier.sent] == ["@elon followed @tesla"]
    assert notifier.sent[0]["url"] == "https://x.com/tesla"
    src.lists["200"].append(user(99, "tesla"))
    run_all()
    assert [e["author"]["name"] for e in notifier.sent] == ["@elon followed @tesla", "@nasa followed @tesla"]
    run_all()
    assert len(notifier.sent) == 2


def test_actual_dry_run_does_not_post_or_write(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yml"
    cfg.write_text("accounts: [elon]\n")
    state_path = tmp_path / "state.json"
    monkeypatch.setenv("CONFIG_PATH", str(cfg))
    monkeypatch.setenv("STATE_PATH", str(state_path))
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.delenv("WATCH_ACCOUNTS", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(fw, "load_cookies_from_env", lambda: {})
    monkeypatch.setattr(fw, "XSource", lambda _: Source())
    def forbidden(*args, **kwargs):
        pytest.fail("Dry run tried to post to Discord")
    monkeypatch.setattr(requests.Session, "post", forbidden)
    assert fw.main() == 0
    assert not state_path.exists()


@pytest.mark.parametrize("exception", [fw.SessionError, fw.RateLimited])
def test_main_stops_shared_session_and_closes_source(tmp_path, monkeypatch, exception):
    cfg = tmp_path / "config.yml"
    cfg.write_text("accounts: [elon, nasa]\n")
    monkeypatch.setenv("CONFIG_PATH", str(cfg))
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/123/token")
    monkeypatch.delenv("WATCH_ACCOUNTS", raising=False)
    monkeypatch.delenv("DRY_RUN", raising=False)
    class Broken(Source):
        closed = False
        calls = []
        def get_user(self, name):
            self.calls.append(name)
            raise exception("Stop")
        def close(self):
            self.closed = True
    source = Broken()
    monkeypatch.setattr(fw, "load_cookies_from_env", lambda: {})
    monkeypatch.setattr(fw, "XSource", lambda _: source)
    assert fw.main() == 1
    assert source.calls == ["elon"]
    assert source.closed


def test_protected_account_is_rejected():
    class Protected(CookieClient):
        async def get_user_by_screen_name(self, name):
            u = tw_user(100, name)
            u.protected = True
            return u
    source = fw.XSource({}, Protected(Result([])))
    try:
        with pytest.raises(fw.SourceError, match="Protected"):
            source.get_user("elon")
    finally:
        source.close()


def test_real_twifork_parser_and_guard_without_network(monkeypatch):
    from twikit.client.gql import GQLClient
    calls = []
    body = {"data": {"user": {"result": {"timeline": {"instructions": [{"entries": [
        {"entryId": "user-99", "content": {"itemContent": {"user_results": {"result": {
            "__typename": "User", "rest_id": "99", "core": {"screen_name": "tesla", "name": "Tesla"}
        }}}}}
    ]}]}}}}}
    async def following(self, user_id, count, cursor):
        calls.append(user_id)
        return body, None
    monkeypatch.setattr(GQLClient, "following", following)
    source = fw.XSource({"auth_token": "fake", "ct0": "fake"}, page_delay=0)
    try:
        snapshot = fw.fetch_snapshot(source, "100", fw.Settings())
        assert snapshot["99"]["username"] == "tesla"
        assert calls == ["100"]
        # The installed raw guard must reject errors before they become emptiness.
        body.clear()
        with pytest.raises(fw.SourceError, match="timeline"):
            fw.fetch_snapshot(source, "100", fw.Settings())
    finally:
        source.close()
    assert source.loop.is_closed()


def test_workflow_uses_cookie_secrets_and_public_runner_only():
    import yaml
    root = Path(__file__).resolve().parent.parent
    workflow = yaml.safe_load((root / "workflow-templates/follower-watch.yml").read_text())
    job = workflow["jobs"]["check"]
    assert "repository.private == false" in job["if"]
    assert job["runs-on"] == "ubuntu-latest"
    env = next(step["env"] for step in job["steps"] if "env" in step)
    assert set(("X_AUTH_TOKEN", "X_CT0", "DISCORD_WEBHOOK_URL")) <= set(env)
    assert "X_BEARER_TOKEN" not in env
    assert not any("upload-artifact" in step.get("uses", "") for step in job["steps"])
