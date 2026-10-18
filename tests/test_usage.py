import urllib.error

from busytag_claude.usage import URL, UsagePoller


def poller(value, now=1000.0):
    p = UsagePoller(fetcher=lambda: value, clock=lambda: now)
    p.poll()
    return p


def test_limit_rounds_percent_and_ceils_minutes():
    assert poller((90.6, 1000 + 92 * 60 - 30)).limit() == (91, 92, False)


def test_limit_hidden_when_unknown_or_reset():
    assert poller(None).limit() is None
    assert poller((50.0, 999.0)).limit() is None


def test_failed_poll_with_no_reading_hides_bar():
    def boom():
        raise OSError("offline")
    p = UsagePoller(fetcher=boom)
    p.poll()
    assert p.limit() is None


def test_failed_poll_keeps_last_reading():
    results = [(40.0, 1000 + 60 * 60), OSError("offline")]

    def fetch():
        r = results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    p = UsagePoller(fetcher=fetch, clock=lambda: 1000.0)
    p.poll()
    p.poll()
    assert p.limit() == (40, 60, False)


def test_reading_survives_restart(tmp_path):
    cache = tmp_path / "usage-cache.json"
    p = UsagePoller(fetcher=lambda: (25.0, 1000 + 30 * 60), clock=lambda: 1000.0, cache_path=str(cache))
    p.poll()

    def limited():
        raise OSError("rate limited")
    restarted = UsagePoller(fetcher=limited, clock=lambda: 1000.0, cache_path=str(cache))
    restarted.poll()
    assert restarted.limit() == (25, 30, False)


def test_rate_limit_waits_for_retry_after():
    def limited():
        raise urllib.error.HTTPError(URL, 429, "Too Many Requests", {"Retry-After": "3056"}, None)
    p = UsagePoller(interval=120, fetcher=limited)
    assert p.poll() == 3056


def test_other_failures_back_off_exponentially():
    def boom():
        raise OSError("offline")
    p = UsagePoller(interval=120, fetcher=boom)
    assert [p.poll() for _ in range(3)] == [120, 240, 480]
    p.fetcher = lambda: (10.0, 99999.0)
    assert p.poll() == 120


def test_limit_bar_color_by_amount_left():
    from busytag_claude.render import STATUS, limit_color
    green, blue, red = STATUS["done"][1], STATUS["working"][1], STATUS["error"][1]
    orange = STATUS["needs_input"][1]
    assert [limit_color(p) for p in (0, 49, 50, 70, 80, 81, 99, 100)] == [
        green, green, blue, blue, blue, orange, orange, red]


def test_push_replaces_reading_and_pauses_polling():
    calls = []
    p = UsagePoller(interval=120, fetcher=lambda: calls.append(1) or (5.0, 99999.0), clock=lambda: 1000.0)
    p.push((63.4, 1000 + 45 * 60))
    assert p.limit() == (63, 45, False)
    assert p.poll() == 120 and not calls


def test_polls_again_once_pushes_stop():
    now = [1000.0]
    p = UsagePoller(interval=120, fetcher=lambda: (70.0, 99999.0), clock=lambda: now[0])
    p.push((60.0, 99999.0))
    now[0] += 121
    p.poll()
    assert p.limit()[0] == 70


def test_statusline_forwards_five_hour_window(monkeypatch, capsys):
    import io
    import json
    from busytag_claude import statusline
    sent = []

    class Resp:
        def close(self):
            pass
    monkeypatch.setattr(statusline.urllib.request, "urlopen",
                        lambda req, timeout: sent.append(json.loads(req.data)) or Resp())
    window = {"used_percentage": 42.0, "resets_at": 1792248600}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"rate_limits": {"five_hour": window}})))
    statusline.main()
    assert sent == [{"five_hour": window}]
    assert capsys.readouterr().out.startswith("5h 42% · resets ")


def test_old_reading_is_stale():
    now = [1000.0]
    p = UsagePoller(fetcher=lambda: (30.0, 99999.0), clock=lambda: now[0])
    p.poll()
    now[0] += 601
    assert p.limit()[2] is True


def test_expired_token_is_refreshed_before_polling():
    calls = []
    p = UsagePoller(fetcher=lambda: calls.append("fetch") or (56.0, 99999.0), clock=lambda: 1000.0,
                    expires_at=lambda: 900.0, refresher=lambda: calls.append("refresh") or True)
    p.poll()
    assert calls == ["refresh", "fetch"]
    assert p.limit()[0] == 56


def test_valid_token_is_not_refreshed():
    calls = []
    p = UsagePoller(fetcher=lambda: (10.0, 99999.0), clock=lambda: 1000.0,
                    expires_at=lambda: 5000.0, refresher=lambda: calls.append(1) or True)
    p.poll()
    assert not calls


def test_failed_refresh_is_retried_later_and_ignores_long_retry_after():
    now = [1000.0]
    refreshes = []

    def limited():
        raise urllib.error.HTTPError(URL, 429, "Too Many Requests", {"Retry-After": "3000"}, None)
    p = UsagePoller(interval=120, fetcher=limited, clock=lambda: now[0],
                    expires_at=lambda: 900.0, refresher=lambda: refreshes.append(now[0]) and False)
    assert p.poll() == 120
    now[0] += 120
    p.poll()
    now[0] += 1800
    p.poll()
    assert refreshes == [1000.0, 2920.0]
