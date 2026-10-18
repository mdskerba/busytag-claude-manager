"""Render 240x280 status screens for the Busy Tag display.

Static, Claude-branded: a top bar with a small spark (top left) and the status pill (top
right), both in the status color; the project (with its session count) sits above the session
name, which fills the middle in a large sans; the footer carries the total session
count and how many are waiting, and under it a bar for the 5-hour session limit (green / blue /
orange by how much is left, red once it is used up) with the time until it resets. With sessions in 2+ projects the middle becomes a list:
the highlighted project as a card with its sessions, the other projects as rows below. Drawn at 3x and downsampled for
smooth edges, then palette-quantized so uploads stay small (~1 s each).
"""
import io
import math

from PIL import Image, ImageDraw, ImageFont

W, H = 240, 280
S = 3  # supersampling factor
BG = (31, 30, 29)
FG = (245, 240, 232)
MUTED = (150, 143, 135)
CLAUDE = (217, 119, 87)
DIM = (72, 68, 64)
STATUS = {
    "working": ("Working", (110, 155, 255)),
    "needs_input": ("Needs input", (255, 140, 40)),
    "done": ("Done", (95, 185, 105)),
    "error": ("Error", (240, 90, 80)),
    "idle": ("Ready", (150, 143, 135)),
}

SANS = {
    "demibold": [("/System/Library/Fonts/Avenir Next.ttc", 2, None), ("/System/Library/Fonts/Helvetica.ttc", 1, None)],
    "medium": [("/System/Library/Fonts/Avenir Next.ttc", 5, None), ("/System/Library/Fonts/Helvetica.ttc", 0, None)],
}

# Ray lengths (fraction of the radius) give the spark its hand-drawn, uneven look.
RAYS = (1.0, 0.82, 0.94, 0.78, 0.98, 0.86, 0.92, 0.8, 1.0, 0.84, 0.9, 0.79)


def _load(candidates, size):
    for path, index, variation in candidates:
        try:
            f = ImageFont.truetype(path, size * S, index=index or 0)
        except OSError:
            continue
        if variation:
            try:
                f.set_variation_by_axes(variation)
            except (OSError, ValueError):
                pass
        return f
    return ImageFont.load_default(size * S)


def _sans(size, weight="demibold"):
    return _load(SANS[weight], size)


def _p(*v):
    """Scale logical coordinates to the supersampled canvas."""
    return tuple(x * S for x in v)


def _mix(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def _fit(draw, text, max_w, font):
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text.rstrip() + "…"


def _wrap(draw, text, max_w, font, max_lines=2):
    """Word-wrap into at most `max_lines`; the last line is truncated with an ellipsis."""
    max_w *= S
    words, lines = text.split(), []
    while words and len(lines) < max_lines - 1:
        line = ""
        while words and draw.textlength((line + " " + words[0]).strip(), font=font) <= max_w:
            line = (line + " " + words.pop(0)).strip()
        if not line:  # a single word wider than the line
            break
        lines.append(line)
    if words:
        lines.append(_fit(draw, " ".join(words), max_w, font))
    return lines


def _spark(d, cx, cy, r, color, rays=RAYS):
    """Claude-style spark: tapered rounded rays around a small hub."""
    cx, cy, r = cx * S, cy * S, r * S
    n = len(rays)
    for i, length in enumerate(rays):
        a = -math.pi / 2 + i * 2 * math.pi / n + (0.06 if i % 2 else -0.04)
        ux, uy = math.cos(a), math.sin(a)
        px, py = -uy, ux
        base, tip = r * 0.14, r * 0.085
        lr = r * length - tip
        pts = [
            (cx + px * base, cy + py * base),
            (cx + ux * lr + px * tip, cy + uy * lr + py * tip),
            (cx + ux * lr - px * tip, cy + uy * lr - py * tip),
            (cx - px * base, cy - py * base),
        ]
        d.polygon(pts, fill=color)
        tx, ty = cx + ux * lr, cy + uy * lr
        d.ellipse((tx - tip, ty - tip, tx + tip, ty + tip), fill=color)
    hub = r * 0.2
    d.ellipse((cx - hub, cy - hub, cx + hub, cy + hub), fill=color)


def _counts(d, total, waiting, cy):
    """Footer: "3 sessions · 2 waiting" (waiting in amber); nothing for a lone session."""
    parts = [(f"{total} sessions", MUTED)] if total > 1 else []
    if waiting > 1:
        parts += [("  ·  ", MUTED)] * bool(parts) + [(f"{waiting} waiting", STATUS["needs_input"][1])]
    if not parts:
        return
    f = _sans(15, "medium")
    x = (W * S - sum(d.textlength(t, font=f) for t, _ in parts)) / 2
    for text, color in parts:
        d.text((x, cy * S), text, font=f, fill=color, anchor="lm")
        x += d.textlength(text, font=f)


LIMIT_WARN = _mix(STATUS["error"][1], FG, 0.15)  # reset time under an hour away
LIMIT_X = (W - (W - 2 * 14) * 0.9) / 2  # 10% narrower than the margins, clear of the rounded screen corners


def limit_color(pct):
    """Bar fill by how much of the limit is left: green above 50%, blue 20-50%, orange below 20%,
    red once none is left (until it resets)."""
    left = 100 - pct
    if left > 50:
        return STATUS["done"][1]
    if left <= 0:
        return STATUS["error"][1]
    if left < 20:
        return STATUS["needs_input"][1]
    return STATUS["working"][1]


def _limit(d, limit, cy):
    """Session limit bar (fill colored by limit_color, grey when stale) with "h:mmh" until reset at its right."""
    pct, minutes, stale = limit
    color = MUTED if stale else limit_color(pct)
    f = _sans(13)
    text = f"{minutes // 60}:{minutes % 60:02d}h"
    warn = minutes < 60
    d.text(_p(W - LIMIT_X, cy), text, font=f, fill=LIMIT_WARN if warn else MUTED, anchor="rm")
    x0, x1, h = LIMIT_X, W - LIMIT_X - d.textlength(text, font=f) / S - 10, 6
    d.rounded_rectangle(_p(x0, cy - h / 2, x1, cy + h / 2), radius=h / 2 * S, fill=_mix(BG, FG, 0.12))
    if pct:
        xf = x0 + max(h, (x1 - x0) * pct / 100)
        d.rounded_rectangle(_p(x0, cy - h / 2, xf, cy + h / 2), radius=h / 2 * S, fill=color)


def _pill(d, label, color, right, cy):
    """Status pill, right-aligned to `right`."""
    f = _sans(14)
    text = label.upper()
    tracking = 1.3 * S
    widths = [d.textlength(ch, font=f) for ch in text]
    tw = sum(widths) + tracking * (len(text) - 1)
    pw, ph = tw + 24 * S, 28 * S
    x0, y0 = right * S - pw, cy * S - ph / 2
    d.rounded_rectangle((x0, y0, x0 + pw, y0 + ph), radius=ph / 2, fill=_mix(BG, color, 0.18))
    x = x0 + 12 * S
    ink = _mix(color, FG, 0.3)  # lifted toward FG so every status clears 4.5:1 on its tint
    for ch, cw in zip(text, widths):
        d.text((x, cy * S), ch, font=f, fill=ink, anchor="lm")
        x += cw + tracking


def _canvas():
    im = Image.new("RGB", (W * S, H * S), BG)
    return im, ImageDraw.Draw(im)


def _png(im):
    im = im.resize((W, H), Image.LANCZOS).quantize(colors=128, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


TOP = 26  # top-bar center line
MARGIN = 14


def _folder(d, x, cy, ink):
    """Folder glyph (tab + outlined body), 13 px wide, left edge at x."""
    d.rounded_rectangle(_p(x, cy - 5, x + 13, cy + 5), radius=2 * S, outline=ink, width=round(1.6 * S))
    d.rounded_rectangle(_p(x, cy - 7, x + 6, cy - 4), radius=1 * S, fill=ink)


def _tag(d, text, x, cy, align="l"):
    """Small rounded count tag; returns its width. align="r" puts its right edge at x."""
    f = _sans(13)
    w = d.textlength(text, font=f) / S + 12
    if align == "r":
        x -= w
    d.rounded_rectangle(_p(x, cy - 9, x + w, cy + 9), radius=9 * S, fill=_mix(BG, MUTED, 0.3))
    d.text(_p(x + w / 2, cy), text, font=f, fill=FG, anchor="mm")
    return w


def _project(d, project, group, cy):
    """Project line: folder glyph + name, plus a small count tag when the project has several sessions."""
    f = _sans(15)
    count = str(group) if group > 1 else ""
    cw = (d.textlength(count, font=_sans(13)) / S + 12) if count else 0
    gap = 6 if count else 0
    name = _fit(d, project, (W - 2 * MARGIN - 18 - cw - gap) * S, f)
    tw = d.textlength(name, font=f) / S
    x = (W - (18 + tw + gap + cw)) / 2
    ink = _mix(MUTED, FG, 0.45)
    _folder(d, x, cy, ink)
    d.text(_p(x + 18, cy), name, font=f, fill=ink, anchor="lm")
    if count:
        _tag(d, count, x + 18 + tw + gap, cy)


# Overview (2+ projects) row heights and the band they fill between top bar and footer.
LIST_TOP, LIST_BOTTOM = 52, 238
HEAD_H, SESSION_H, OTHER_H, MORE_H, CARD_PAD, CARD_GAP = 30, 26, 30, 22, 5, 6
MAX_SESSIONS = 4


def _overview_layout(n_sessions, n_others, bottom=LIST_BOTTOM):
    """How many of the highlighted project's sessions and other projects fit (at least one each)."""
    avail = bottom - LIST_TOP

    def height(k, m):
        card = 2 * CARD_PAD + HEAD_H + SESSION_H * k + (MORE_H if k < n_sessions else 0)
        rest = CARD_GAP + OTHER_H * m + (MORE_H if m < n_others else 0)
        return card + rest

    for k in range(min(n_sessions, MAX_SESSIONS), 0, -1):
        for m in range(n_others, 0, -1):
            if height(k, m) <= avail:
                return k, m
    return 1, 1


def _overview(d, overview, bottom=LIST_BOTTOM):
    """Highlighted project as a card with its sessions; other projects as rows below."""
    (project, _, sessions), others = overview[0], overview[1:]
    k, m = _overview_layout(len(sessions), len(others), bottom)
    cur = next(i for i, row in enumerate(sessions) if row[2])
    shown = list(sessions[:k]) if cur < k else [*sessions[:k - 1], sessions[cur]]  # current stays visible
    x0, x1 = MARGIN - 4, W - MARGIN + 4
    soft = _mix(MUTED, FG, 0.45)

    y = LIST_TOP
    card_h = 2 * CARD_PAD + HEAD_H + SESSION_H * len(shown) + (MORE_H if len(shown) < len(sessions) else 0)
    d.rounded_rectangle(_p(x0, y, x1, y + card_h), radius=12 * S, fill=_mix(BG, FG, 0.07))
    y += CARD_PAD
    cy = y + HEAD_H / 2
    _folder(d, MARGIN + 2, cy, FG)
    d.text(_p(MARGIN + 21, cy), _fit(d, project, (x1 - MARGIN - 29) * S, _sans(16)), font=_sans(16), fill=FG, anchor="lm")
    y += HEAD_H
    for name, status, current in shown:
        cy = y + SESSION_H / 2
        r = 4.5
        d.ellipse(_p(MARGIN + 8.5 - r, cy - r, MARGIN + 8.5 + r, cy + r), fill=STATUS[status][1])
        f = _sans(15) if current else _sans(15, "medium")
        d.text(_p(MARGIN + 21, cy), _fit(d, name, (x1 - MARGIN - 29) * S, f), font=f, fill=FG if current else soft, anchor="lm")
        y += SESSION_H
    if len(shown) < len(sessions):
        d.text(_p(MARGIN + 21, y + MORE_H / 2), f"+{len(sessions) - len(shown)} more", font=_sans(13, "medium"), fill=MUTED, anchor="lm")
        y += MORE_H
    y += CARD_PAD + CARD_GAP

    for name, top, rows in others[:m]:
        cy = y + OTHER_H / 2
        _folder(d, MARGIN + 2, cy, MUTED)
        tag_w = _tag(d, str(len(rows)), x1 - 6, cy, align="r")
        r = 4.5
        dot_x = x1 - 6 - tag_w - 6 - r
        d.ellipse(_p(dot_x - r, cy - r, dot_x + r, cy + r), fill=STATUS[top][1])
        f = _sans(15, "medium")
        d.text(_p(MARGIN + 21, cy), _fit(d, name, (dot_x - r - 8 - MARGIN - 21) * S, f), font=f, fill=soft, anchor="lm")
        y += OTHER_H
    if m < len(others):
        d.text(_p(MARGIN + 21, y + MORE_H / 2), f"+{len(others) - m} more projects", font=_sans(13, "medium"), fill=MUTED, anchor="lm")


# With the limit bar the session count moves up to make room for it.
COUNTS_Y, COUNTS_Y_WITH_LIMIT, LIMIT_Y = 254, 244, 266


def render_session(name, status, others=0, waiting=0, project=None, group=1, overview=None, limit=None):
    label, color = STATUS[status]
    im, d = _canvas()
    _spark(d, MARGIN + 12, TOP, 12, color)
    _pill(d, label, color, W - MARGIN, TOP)
    counts_y = COUNTS_Y_WITH_LIMIT if limit else COUNTS_Y
    if limit:
        _limit(d, limit, LIMIT_Y)
    if overview:
        _overview(d, overview, LIST_BOTTOM - (COUNTS_Y - counts_y))
        _counts(d, others + 1, waiting, counts_y)
        return _png(im)
    # An untitled session is already named after its project; skip the repeat.
    if project and name.split(" #")[0] == project:
        project = None if group <= 1 else project

    # Largest size that fits untruncated: 34 on one line, 28 on two, else 24 on up to three.
    for size, max_lines in ((34, 1), (28, 2), (24, 3)):
        font = _sans(size)
        lines = _wrap(d, name, W - 2 * MARGIN, font, max_lines)
        if not lines[-1].endswith("…"):
            break
    lh = size * 1.25
    block = lh * len(lines) + (34 if project else 0)
    y0 = 146 - block / 2 + lh / 2 + (34 if project else 0)
    if project:
        _project(d, project, group, y0 - lh / 2 - 22)
    for i, line in enumerate(lines):
        d.text(_p(W / 2, y0 + i * lh), line, font=font, fill=FG, anchor="mm")

    _counts(d, others + 1, waiting, counts_y)
    return _png(im)


def render_idle():
    im, d = _canvas()
    _spark(d, MARGIN + 12, TOP, 12, DIM)
    d.text(_p(W / 2, 128), "All quiet", font=_sans(34), fill=FG, anchor="mm")
    d.text(_p(W / 2, 164), "No active sessions", font=_sans(15, "medium"), fill=MUTED, anchor="mm")
    return _png(im)
