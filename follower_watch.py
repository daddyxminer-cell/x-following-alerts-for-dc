#!/usr/bin/env python3
"""Poll X follow relationships and deliver identifiable Discord notifications."""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
import yaml


class SourceError(RuntimeError):
    """No complete, trustworthy X snapshot is available."""


class SessionError(SourceError):
    """The shared X cookie session needs attention; stop checking accounts."""


class RateLimited(SourceError):
    """Stop the run instead of retrying or rotating accounts."""


@dataclass
class AccountConfig:
    username: str
    enabled: bool = True
    webhook_env: str | None = None
    max_pages_per_run: int | None = None


@dataclass
class Settings:
    accounts: list[AccountConfig] = field(default_factory=list)
    watch_mode: str = "following"
    page_size: int = 100
    max_pages_per_run: int = 300
    max_notifications_per_run: int = 50
    announce_new_watch: bool = True
    incremental_scan: bool = False
    early_stop_known_pages: int = 2
    max_new_baselines_per_run: int = 1000
    page_delay: float = 1.0
    account_delay: float = 3.0


def normalise_username(value: str) -> str:
    value = value.strip()
    if value.startswith(("https://", "http://")):
        parsed = urlparse(value)
        if parsed.hostname not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
            raise ValueError("Account URLs must use x.com or twitter.com")
        value = parsed.path.strip("/")
    value = value.lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", value):
        raise ValueError("Accounts must be X usernames, @usernames, or profile URLs")
    return value


def load_settings(path: str | Path, env_accounts: str | None = None) -> Settings:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("Configuration must be a mapping")
    settings = Settings()
    settings.watch_mode = raw.get("watch_mode", settings.watch_mode)
    if settings.watch_mode not in {"following", "followers"}:
        raise ValueError("watch_mode must be following or followers")
    for name, maximum in (
        ("page_size", 100),
        ("max_pages_per_run", 10000),
        ("max_notifications_per_run", 1000),
        ("early_stop_known_pages", 100),
        ("max_new_baselines_per_run", 1000),
    ):
        value = raw.get(name, getattr(settings, name))
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f"{name} must be an integer between 1 and {maximum}")
        setattr(settings, name, value)
    for bool_name in ("announce_new_watch", "incremental_scan"):
        val = raw.get(bool_name, getattr(settings, bool_name))
        if not isinstance(val, bool):
            raise ValueError(f"{bool_name} must be true or false")
        setattr(settings, bool_name, val)
    for float_name in ("page_delay", "account_delay"):
        val = raw.get(float_name, getattr(settings, float_name))
        if isinstance(val, bool) or not isinstance(val, (int, float)) or not 0 <= float(val) <= 60:
            raise ValueError(f"{float_name} must be a number between 0 and 60")
        setattr(settings, float_name, float(val))
    entries = raw.get("accounts", [])
    if env_accounts and env_accounts.strip():
        entries = [s.strip() for s in re.split(r"[,\n]", env_accounts) if s.strip()]
    if not isinstance(entries, list):
        raise ValueError("accounts must be a list")
    seen = set()
    for entry in entries:
        if isinstance(entry, str):
            entry = {"username": entry}
        if not isinstance(entry, dict) or not isinstance(entry.get("username"), str):
            raise ValueError("Each account needs a username")
        name = normalise_username(entry["username"])
        enabled = entry.get("enabled", True)
        webhook_env = entry.get("webhook_env")
        max_pages = entry.get("max_pages_per_run")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be true or false")
        if webhook_env is not None and (not isinstance(webhook_env, str) or
                                       not re.fullmatch(r"[A-Z_][A-Z0-9_]*", webhook_env)):
            raise ValueError("webhook_env must be an environment variable name")
        if max_pages is not None and (type(max_pages) is not int or not 1 <= max_pages <= 10000):
            raise ValueError("max_pages_per_run must be an integer between 1 and 10000")
        if name.lower() not in seen:
            settings.accounts.append(AccountConfig(name, enabled, webhook_env, max_pages))
            seen.add(name.lower())
    return settings


def load_state(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return {"version": 2, "accounts": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not isinstance(state.get("accounts"), dict):
            raise ValueError("Invalid state structure")
        if "version" not in state:
            print("Legacy state detected; creating new complete baselines.")
            return {"version": 2, "accounts": {}}
        if state["version"] != 2:
            raise ValueError("Unsupported state version")
        for account in state["accounts"].values():
            if (not isinstance(account, dict) or
                    not isinstance(account.get("ids"), list) or
                    not all(isinstance(uid, str) for uid in account["ids"]) or
                    not isinstance(account.get("pending"), list)):
                raise ValueError("Invalid account state")
        return state
    except (ValueError, TypeError) as exc:
        raise ValueError("State is invalid; restore it from git history rather than resetting it") from exc


def save_state(path: str | Path, state: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_cookies_from_env() -> dict[str, str]:
    """Only accept the required cookies. Never print or persist their values."""
    raw = os.getenv("X_COOKIES", "").strip()
    if raw:
        try:
            cookies = json.loads(raw)
            if isinstance(cookies, list):
                cookies = {item["name"]: item["value"] for item in cookies}
            if not isinstance(cookies, dict):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise ValueError("X_COOKIES must be a cookie mapping or browser-export JSON list") from None
    else:
        cookies = {"auth_token": os.getenv("X_AUTH_TOKEN", ""),
                   "ct0": os.getenv("X_CT0", "")}
    result = {}
    for name in ("auth_token", "ct0"):
        value = cookies.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Set X_AUTH_TOKEN and X_CT0 secrets (or both in X_COOKIES)")
        result[name] = value.strip()
    return result


def validate_timeline(body: Any) -> None:
    """Do not let the scraper turn a malformed/error response into an empty list."""
    if not isinstance(body, dict) or body.get("errors"):
        raise SourceError("X returned an error/partial response; snapshot unchanged")
    found = False
    stack = [body]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            if "entries" in value:
                if not isinstance(value["entries"], list):
                    raise SourceError("Invalid X timeline entries; snapshot unchanged")
                found = True
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    if not found:
        raise SourceError("X timeline unavailable or changed format; snapshot unchanged")


class XSource:
    """Read-only twifork adapter: browser cookies, no paid API or proxy service."""
    def __init__(self, cookies: dict[str, str], client=None, page_delay: float = 1.0):
        from twikit import Client
        self.client = client or Client("en-US", timeout=30)
        self.client.set_cookies(cookies)
        self.loop = asyncio.new_event_loop()
        self.page_delay = page_delay
        self._page = None
        self._token = None
        self._sequence = 0
        self._signatures = set()
        if client is None:
            for name in ("following", "followers"):
                original = getattr(self.client.gql, name)

                async def checked(*args, _original=original, **kwargs):
                    body, response = await _original(*args, **kwargs)
                    validate_timeline(body)
                    return body, response

                setattr(self.client.gql, name, checked)

    def _run(self, factory):
        import warnings
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings("error", category=UserWarning, module=r"twikit\..*")
                return self.loop.run_until_complete(factory())
        except SourceError:
            raise
        except Exception as exc:
            kind = type(exc).__name__
            if kind in {"InvalidSession", "Unauthorized", "AccountLocked", "AccountSuspended"}:
                raise SessionError("X login needs attention: sign in manually and refresh the cookie secrets") from None
            if kind == "TooManyRequests":
                raise RateLimited("X rate limit reached; stopping until a later scheduled run") from None
            if kind in {"Forbidden", "UserUnavailable", "NotFound", "UserNotFound"}:
                raise SourceError("X refused access or the account is unavailable; check cookies and visibility") from None
            raise SourceError("X request or parser failed; snapshot unchanged. Check session and twifork compatibility") from None

    @staticmethod
    def user_dict(user) -> dict:
        result = {"id": str(getattr(user, "id", "")),
                  "username": getattr(user, "screen_name", ""),
                  "name": getattr(user, "name", ""),
                  "description": getattr(user, "description", ""),
                  "profile_image_url": getattr(user, "profile_image_url", "")}
        validate_user(result)
        return result

    def get_user(self, username: str) -> dict:
        user = self._run(lambda: self.client.get_user_by_screen_name(username))
        if getattr(user, "protected", False):
            raise SourceError("Protected accounts are not supported: state is stored in a public repository")
        result = self.user_dict(user)
        result["following_count"] = getattr(user, "following_count", None)
        result["followers_count"] = getattr(user, "followers_count", None)
        return result

    def get_page(self, user_id: str, mode: str, page_size: int, token=None) -> dict:
        if mode not in {"following", "followers"}:
            raise ValueError("Unsupported watch mode")
        if token is None:
            self._signatures.clear()
            self._sequence = 0
            self._page = None
            self._token = None
            method = (self.client.get_user_following if mode == "following"
                      else self.client.get_user_followers)
            page = self._run(lambda: method(user_id, count=page_size))
        else:
            if token != self._token or self._page is None:
                raise SourceError("Invalid local pagination token")
            if self.page_delay > 0:
                jitter = random.uniform(0.0, 0.35 * self.page_delay)
                time.sleep(self.page_delay + jitter)
            page = self._run(self._page.next)
        users = [self.user_dict(u) for u in page]
        meta = {"result_count": len(users)}
        if users:
            signature = (getattr(page, "next_cursor", None), tuple(u["id"] for u in users))
            if signature in self._signatures:
                raise SourceError("Repeated X pagination page; snapshot unchanged")
            self._signatures.add(signature)
            self._sequence += 1
            self._token = str(self._sequence)
            self._page = page
            meta["next_token"] = self._token
        else:
            self._page = None
            self._token = None
        return {"data": users, "meta": meta}

    def close(self):
        try:
            http = getattr(self.client, "http", None)
            if http is not None:
                self.loop.run_until_complete(http.aclose())
        finally:
            self.loop.close()


def validate_user(user: Any) -> None:
    if (not isinstance(user, dict) or not isinstance(user.get("id"), str) or
            not user["id"].isdigit() or not isinstance(user.get("username"), str) or
            not re.fullmatch(r"[A-Za-z0-9_]{1,15}", user["username"])):
        raise SourceError("X returned an invalid user record")


def fetch_snapshot(
    source,
    user_id: str,
    settings: Settings,
    known_ids: set[str] | None = None,
    max_pages: int | None = None,
) -> dict[str, dict]:
    """Read pages. If incremental_scan is enabled and known_ids exists, stop early once
    early_stop_known_pages consecutive pages contain only known IDs."""
    users = {}
    token = None
    tokens = set()
    budget = max_pages if max_pages is not None else settings.max_pages_per_run
    consecutive_known_pages = 0
    use_incremental = bool(settings.incremental_scan and known_ids)

    for _ in range(budget):
        body = source.get_page(user_id, settings.watch_mode, settings.page_size, token)
        meta = body.get("meta")
        data = body.get("data", [])
        if (body.get("errors") or not isinstance(meta, dict) or
                type(meta.get("result_count")) is not int or
                not isinstance(data, list) or meta["result_count"] != len(data)):
            raise SourceError("Incomplete X response; snapshot unchanged")
        new_on_page = 0
        for user in data:
            validate_user(user)
            uid = user["id"]
            users[uid] = user
            if use_incremental and uid not in known_ids:
                new_on_page += 1
        token = meta.get("next_token")
        if not token:
            return users
        if use_incremental and data:
            if new_on_page == 0:
                consecutive_known_pages += 1
                if consecutive_known_pages >= settings.early_stop_known_pages:
                    return users
            else:
                consecutive_known_pages = 0
        if not isinstance(token, str) or token in tokens:
            raise SourceError("Repeated or invalid pagination token; snapshot unchanged")
        tokens.add(token)
    raise SourceError("Full list exceeds max_pages_per_run; increase it in config.yml. Snapshot unchanged")


class DiscordNotifier:
    def __init__(self, dry_run=False, session=None):
        self.dry_run = dry_run
        self.session = session or requests.Session()

    def send(self, webhook: str, embed: dict) -> None:
        payload = {"username": "X Follow Watch", "allowed_mentions": {"parse": []},
                   "embeds": [embed]}
        if self.dry_run:
            print("[DRY RUN] " + json.dumps(payload, ensure_ascii=True))
            return
        for attempt in range(4):
            try:
                response = self.session.post(webhook, params={"wait": "true"},
                                             json=payload, timeout=30)
            except requests.RequestException:
                raise RuntimeError("Discord network request failed; delivery remains pending") from None
            if response.status_code == 429 and attempt < 3:
                try:
                    delay = float(response.json().get("retry_after", 1))
                except (ValueError, TypeError, AttributeError):
                    delay = 1
                if not 0 <= delay <= 60:
                    break
                time.sleep(delay + 0.25)
                continue
            if response.status_code not in {200, 204}:
                raise RuntimeError(f"Discord returned HTTP {response.status_code}; delivery remains pending")
            return
        raise RuntimeError("Discord rate limit persists; delivery remains pending")


def build_embed(user: dict, watched: dict, mode: str) -> dict:
    name, watched_name = user["username"], watched["username"]
    heading = (f"@{watched_name} followed @{name}" if mode == "following"
               else f"New follower of @{watched_name}")
    embed = {
        "author": {"name": heading, "url": f"https://x.com/{watched_name}"},
        "title": f"{user.get('name') or name} (@{name})"[:256],
        "url": f"https://x.com/{name}", "color": 0x1D9BF0,
        "description": (user.get("description") or "No bio provided.")[:1000],
        "fields": [
            {"name": "Account ID", "value": user["id"], "inline": True},
            {"name": "Watching", "value": f"@{watched_name} (ID {watched['id']})", "inline": True},
        ],
        "footer": {"text": "Detected by polling; timestamp is detection time, not follow time."},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    image = user.get("profile_image_url", "")
    if isinstance(image, str) and image.startswith("https://"):
        embed["thumbnail"] = {"url": image}
    return embed


def resolve_webhook(account: AccountConfig, dry_run: bool) -> str:
    key = account.webhook_env or "DISCORD_WEBHOOK_URL"
    webhook = os.getenv(key, "").strip()
    if not webhook and dry_run:
        return ""
    parsed = urlparse(webhook)
    if (parsed.scheme != "https" or parsed.hostname not in
            {"discord.com", "discordapp.com", "canary.discord.com", "ptb.discord.com"} or
            not re.fullmatch(r"/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9_.-]+", parsed.path)):
        raise ValueError(f"Set {key} to a valid Discord webhook URL")
    return webhook


def process_account(account, settings, state, source, notifier, webhook, checkpoint=lambda: None):
    watched = source.get_user(account.username)
    key = f"{settings.watch_mode}:{watched['id']}"
    previous = state["accounts"].get(key)
    sent = 0

    def drain(record):
        nonlocal sent
        while record["pending"] and sent < settings.max_notifications_per_run:
            notifier.send(webhook, record["pending"][0])
            record["pending"].pop(0)
            sent += 1
            checkpoint()

    if previous:
        drain(previous)
    known_ids = set(previous["ids"]) if previous else None
    current = fetch_snapshot(source, watched["id"], settings, known_ids, account.max_pages_per_run)
    expected = watched.get("following_count" if settings.watch_mode == "following" else "followers_count")
    if not current and isinstance(expected, int) and expected > 0:
        raise SourceError("X returned an empty list despite a nonzero profile count; snapshot unchanged")
    if previous is None:
        if settings.announce_new_watch:
            notifier.send(webhook, {
                "title": f"Now watching @{watched['username']}",
                "description": f"Mode: {settings.watch_mode}. Saved {len(current)} existing accounts as a baseline; only future additions will be reported.",
                "color": 0x1D9BF0,
            })
        state["accounts"][key] = {"ids": sorted(current), "pending": []}
        checkpoint()
        print(f"@{watched['username']}: baseline saved ({len(current)} accounts)")
        return 0
    additions = sorted(set(current) - known_ids)
    previous["pending"].extend(build_embed(current[uid], watched, settings.watch_mode)
                               for uid in additions)
    if settings.incremental_scan:
        previous["ids"] = sorted(known_ids | set(current))
    else:
        previous["ids"] = sorted(current)
    checkpoint()
    drain(previous)
    print(f"@{watched['username']}: {len(additions)} additions; {len(previous['pending'])} pending")
    return len(additions)


def main() -> int:
    try:
        settings = load_settings(os.getenv("CONFIG_PATH", "config.yml"), os.getenv("WATCH_ACCOUNTS"))
        accounts = [a for a in settings.accounts if a.enabled]
        if not accounts:
            raise ValueError("No enabled accounts; edit config.yml or WATCH_ACCOUNTS")
        state_path = os.getenv("STATE_PATH", "state/followers_state.json")
        state = load_state(state_path)
        dry_run = os.getenv("DRY_RUN", "").lower() in {"true", "1", "yes"}
        notifier = DiscordNotifier(dry_run)
        webhooks = [resolve_webhook(a, dry_run) for a in accounts]
        source = XSource(load_cookies_from_env(), page_delay=settings.page_delay)
    except (ValueError, OSError, yaml.YAMLError) as exc:
        print(f"Configuration error: {exc}")
        return 1
    failures = 0
    new_baselines_this_run = 0

    def checkpoint():
        if not dry_run:
            save_state(state_path, state)

    try:
        for index, (account, webhook) in enumerate(zip(accounts, webhooks)):
            if index and settings.account_delay > 0:
                time.sleep(settings.account_delay + random.uniform(0.0, 1.5))
            try:
                # Check if this account already has a baseline; if not, respect max_new_baselines_per_run
                watched = source.get_user(account.username)
                key = f"{settings.watch_mode}:{watched['id']}"
                is_new_baseline = key not in state["accounts"]
                if is_new_baseline and new_baselines_this_run >= settings.max_new_baselines_per_run:
                    print(f"@{account.username}: queued for baseline on next run (max_new_baselines_per_run={settings.max_new_baselines_per_run} reached)")
                    continue
                process_account(account, settings, state, source, notifier, webhook, checkpoint)
                if is_new_baseline:
                    new_baselines_this_run += 1
            except (SessionError, RateLimited) as exc:
                print(f"Check stopped: {exc}")
                failures += 1
                break
            except (SourceError, RuntimeError, ValueError) as exc:
                print(f"Check failed for @{account.username}: {exc}")
                failures += 1
            finally:
                checkpoint()
    finally:
        close = getattr(source, "close", None)
        if close:
            close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
