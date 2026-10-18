"""Claude 5-hour session limit.

Live readings are pushed by the status line command (statusline.py), which Claude Code feeds the
rate-limit headers of every API response. The usage endpoint is only polled when no push arrived
recently (e.g. only desktop-app sessions are running): it rate-limits hard, often for an hour.

The OAuth token is read from the "Claude Code-credentials" keychain item on every poll and is only
ever sent to api.anthropic.com. Only the claude CLI refreshes that token, and the desktop app never
runs it, so once the token has expired the poller runs the CLI once (a one-word Haiku reply) to get
it refreshed; until then the endpoint just answers 429.
"""
import json
import logging
import math
import os
import pwd
import shutil
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

log = logging.getLogger(__name__)

URL = "https://api.anthropic.com/api/oauth/usage"
KEYCHAIN_SERVICE = "Claude Code-credentials"
# python.org builds ship without CA certs; macOS keeps a bundle here.
SYSTEM_CA = "/etc/ssl/cert.pem"


def _ssl_context():
    return ssl.create_default_context(cafile=SYSTEM_CA if os.path.exists(SYSTEM_CA) else None)


def _credentials():
    out = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True, text=True, timeout=5, check=True,
    ).stdout
    return json.loads(out)["claudeAiOauth"]


def _token():
    return _credentials()["accessToken"]


def token_expires_at():
    """Epoch seconds when the keychain token expires, or None when unknown."""
    try:
        return _credentials()["expiresAt"] / 1000
    except Exception:
        return None


def _claude_cli():
    # launchd's PATH leaves out ~/.local/bin, where the native installer puts claude.
    return shutil.which("claude") or next(
        (p for p in (os.path.expanduser("~/.local/bin/claude"), "/opt/homebrew/bin/claude", "/usr/local/bin/claude")
         if os.access(p, os.X_OK)), None)


def refresh_token():
    """Run the claude CLI once so it refreshes the keychain token. Returns True when it ran OK.

    No settings sources means no hooks (so this never shows up as a session on the tag), and
    nothing is saved to the session history.
    """
    cli = _claude_cli()
    if not cli:
        log.warning("keychain token expired and the claude CLI was not found to refresh it")
        return False
    try:
        out = subprocess.run(
            [cli, "-p", "Reply with just: ok", "--model", "haiku", "--max-turns", "1",
             "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence"],
            capture_output=True, text=True, timeout=120, check=True, cwd=os.path.expanduser("~"),
            stdin=subprocess.DEVNULL,
            # Without USER the CLI looks up the wrong keychain account and says "Not logged in".
            env={**os.environ, "USER": os.environ.get("USER") or pwd.getpwuid(os.getuid()).pw_name},
        ).stdout
    except subprocess.CalledProcessError as e:
        log.warning("could not refresh the keychain token with the claude CLI: %s",
                    (e.stdout or e.stderr or str(e)).strip()[:200])
        return False
    except Exception as e:
        log.warning("could not refresh the keychain token with the claude CLI: %s", e)
        return False
    if "not logged in" in out.lower():  # exits 0 all the same
        log.warning("could not refresh the keychain token: the claude CLI is not logged in")
        return False
    log.info("refreshed the keychain token with the claude CLI")
    return True


def fetch():
    """Return (utilization 0-100, resets_at epoch seconds) for the 5-hour window, or None."""
    req = urllib.request.Request(URL, headers={
        "Authorization": f"Bearer {_token()}",
        "anthropic-beta": "oauth-2025-04-20",
    })
    with urllib.request.urlopen(req, timeout=10, context=_ssl_context()) as r:
        window = json.load(r).get("five_hour")
    if not window or not window.get("resets_at"):
        return None
    return float(window.get("utilization") or 0), datetime.fromisoformat(window["resets_at"]).timestamp()


MAX_BACKOFF_S = 3600
REFRESH_RETRY_S = 1800  # a refresh that failed (offline, logged out) is tried again this often
STALE_S = 600  # older readings are drawn grey: the bar would otherwise look live while it isn't


def _retry_after(e):
    """Seconds the server asked us to wait (HTTP 429 Retry-After), or None."""
    if isinstance(e, urllib.error.HTTPError) and e.code == 429:
        try:
            return max(0, int(e.headers.get("Retry-After", "")))
        except ValueError:
            return None
    return None


class UsagePoller:
    def __init__(self, interval=120, fetcher=fetch, clock=time.time, cache_path=None,
                 expires_at=None, refresher=None):
        self.interval = interval
        self.fetcher = fetcher
        self.clock = clock
        self.expires_at = expires_at  # () -> token expiry (epoch seconds) or None; None skips refreshing
        self.refresher = refresher  # () -> bool, refreshes an expired token
        self.refreshed_at = None
        self.cache_path = cache_path  # last reading survives restarts (and rate-limited polls)
        self.value, self.updated_at = self._load()
        self.failures = 0
        self.pushed_at = None  # clock() of the last push from the status line
        self.lock = threading.Lock()

    def _load(self):
        if not self.cache_path:
            return None, None
        try:
            with open(self.cache_path) as f:
                d = json.load(f)
            return (float(d["utilization"]), float(d["resets_at"])), float(d.get("updated_at", 0))
        except (OSError, ValueError, KeyError, TypeError):
            return None, None

    def _save(self, value):
        if not self.cache_path or value is None:
            return
        try:
            with open(self.cache_path, "w") as f:
                json.dump({"utilization": value[0], "resets_at": value[1], "updated_at": self.updated_at}, f)
        except OSError as e:
            log.debug("could not save usage cache: %s", e)

    def push(self, value):
        """A reading from the status line; fresher than any poll, so polling pauses while they come."""
        with self.lock:
            self.value = value
            self.pushed_at = self.updated_at = self.clock()
        self._save(value)

    def poll(self):
        """Poll once; return seconds to wait before the next poll.

        A failed poll keeps the last reading (limit() drops it once its reset time passes) and
        backs off: the endpoint rate-limits hard, and polling through a 429 keeps it blocked.
        """
        if self.pushed_at is not None and self.clock() - self.pushed_at < self.interval:
            return self.interval
        refreshed = self._refresh_if_expired()
        try:
            value = self.fetcher()
        except Exception as e:  # offline, logged out, rate limited, endpoint changed
            self.failures += 1
            wait = _retry_after(e)
            if wait is None:
                wait = min(MAX_BACKOFF_S, self.interval * 2 ** (self.failures - 1))
            wait = max(self.interval, wait)
            if refreshed is False:
                wait = self.interval  # the 429 is from the expired token, not from polling too often
            if isinstance(e, urllib.error.HTTPError) and e.code == 401:
                e = "keychain token expired; it refreshes when you use the claude CLI"
            (log.warning if self.failures == 1 else log.debug)(
                "usage poll failed (%s); keeping last reading, retrying in %ds", e, wait)
            return wait
        if self.failures:
            log.info("usage poll recovered after %d failure(s)", self.failures)
        self.failures = 0
        with self.lock:
            self.value = value
            self.updated_at = self.clock()
        self._save(value)
        return self.interval

    def _refresh_if_expired(self):
        """Refresh an expired token: True if refreshed, False if it is expired and still is, else None."""
        if not (self.expires_at and self.refresher):
            return None
        expires = self.expires_at()
        now = self.clock()
        if expires is None or expires > now + 60:
            return None
        if self.refreshed_at is not None and now - self.refreshed_at < REFRESH_RETRY_S:
            return False
        self.refreshed_at = now
        return bool(self.refresher()) or False

    def start(self):
        def loop():
            while True:
                time.sleep(self.poll())

        threading.Thread(target=loop, daemon=True).start()

    def limit(self):
        """(percent used, whole minutes until reset, stale), or None when unknown or already reset."""
        with self.lock:
            value, updated_at = self.value, self.updated_at
        if value is None:
            return None
        pct, resets_at = value
        minutes = math.ceil((resets_at - self.clock()) / 60)
        if minutes <= 0:
            return None
        stale = updated_at is None or self.clock() - updated_at > STALE_S
        return min(100, max(0, round(pct))), minutes, stale
