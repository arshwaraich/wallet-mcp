"""Makes an event's brand colours look polished on a pass: tames neon, guarantees readable text.

repair() takes the organizer's background (and optionally foreground / label) colour, hex or
"rgb(r,g,b)", and returns the three pass.json colours as "rgb(r, g, b)" strings."""
import colorsys

import pass_builder

MAX_SATURATION = 0.80   # HSL saturation above this reads as neon on a card
MIN_LIGHTNESS, MAX_LIGHTNESS = 0.07, 0.93  # pure black / pure white backgrounds look flat and harsh
TEXT_CONTRAST, LABEL_CONTRAST = 4.5, 3.0
NEAR_WHITE, NEAR_BLACK = (250, 250, 252), (20, 20, 24)


def _luminance(rgb) -> float:
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _tame(rgb):
    h, l, s = colorsys.rgb_to_hls(*(c / 255 for c in rgb))
    s = min(s, MAX_SATURATION)
    if s > 0.5 and 0.35 < l < 0.65:
        l *= 0.88  # a saturated mid-tone also glares; deepen it slightly
    l = min(max(l, MIN_LIGHTNESS), MAX_LIGHTNESS)
    return tuple(round(c * 255) for c in colorsys.hls_to_rgb(h, l, s))


def _best_text(bg):
    for candidate in (NEAR_WHITE, NEAR_BLACK, (255, 255, 255), (0, 0, 0)):
        if contrast(candidate, bg) >= TEXT_CONTRAST:
            # of the two near candidates prefer the one with more contrast
            alt = NEAR_BLACK if candidate == NEAR_WHITE else NEAR_WHITE
            if candidate in (NEAR_WHITE, NEAR_BLACK) and contrast(alt, bg) >= TEXT_CONTRAST \
                    and contrast(alt, bg) > contrast(candidate, bg):
                return alt
            return candidate
    return max(((255, 255, 255), (0, 0, 0)), key=lambda c: contrast(c, bg))


def _soften(fg, bg):
    """fg blended toward bg as far as the label contrast (3:1) allows, at most 40%."""
    for pct in range(40, -1, -2):
        t = pct / 100
        mixed = tuple(round(f + (b - f) * t) for f, b in zip(fg, bg))
        if contrast(mixed, bg) >= LABEL_CONTRAST:
            return mixed
    return fg


def _fmt(rgb) -> str:
    return "rgb({}, {}, {})".format(*rgb)


def repair(background: str, foreground: str | None = None, label: str | None = None) -> dict:
    """Raises pass_builder.PassBuildError if background is not a colour. An unparseable foreground or
    label is ignored (replaced by a computed one)."""
    bg = _tame(pass_builder.parse_color(background))
    fg = None
    if foreground:
        try:
            fg = pass_builder.parse_color(foreground)
        except pass_builder.PassBuildError:
            fg = None
    if fg is None or contrast(fg, bg) < TEXT_CONTRAST:
        fg = _best_text(bg)
    lb = None
    if label:
        try:
            lb = pass_builder.parse_color(label)
        except pass_builder.PassBuildError:
            lb = None
    # A brand label colour is kept only if it is readable and clearly softer than the foreground.
    if lb is None or contrast(lb, bg) < LABEL_CONTRAST or contrast(lb, bg) > contrast(fg, bg):
        lb = _soften(fg, bg)
    return {"background_color": _fmt(bg), "foreground_color": _fmt(fg), "label_color": _fmt(lb)}
