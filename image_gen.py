"""Auto-generated icon/logo PNGs for Wallet passes that don't supply their own art."""
import base64
import io

from PIL import Image, ImageDraw, ImageFont

from pass_builder import parse_color

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    return parse_color(color)


def _initials(text: str) -> str:
    words = [w for w in text.strip().split() if w]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def _icon_png(color_hex: str, text: str, px: int) -> bytes:
    rgb = _hex_to_rgb(color_hex)
    img = Image.new("RGB", (px, px), rgb)
    draw = ImageDraw.Draw(img)
    label = _initials(text)
    font = _font(int(px * 0.42))
    bbox = draw.textbbox((0, 0), label, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((px - w) / 2 - bbox[0], (px - h) / 2 - bbox[1]), label, fill="white", font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _logo_png(color_hex: str, text: str, px_w: int, px_h: int) -> bytes:
    rgb = _hex_to_rgb(color_hex)
    img = Image.new("RGBA", (px_w, px_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _font(int(px_h * 0.5))
    label = text.strip() or "?"
    bbox = draw.textbbox((0, 0), label, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if w > px_w - 8:
        # shrink to fit rather than truncate
        scale = (px_w - 8) / w
        font = _font(max(8, int(px_h * 0.5 * scale)))
        bbox = draw.textbbox((0, 0), label, font=font)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((px_w - w) / 2 - bbox[0], (px_h - h) / 2 - bbox[1]), label, fill=rgb, font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def icon_set(color_hex: str, text: str) -> dict[str, bytes]:
    return {
        "icon.png": _icon_png(color_hex, text, 29),
        "icon@2x.png": _icon_png(color_hex, text, 58),
        "icon@3x.png": _icon_png(color_hex, text, 87),
    }


def logo_set(color_hex: str, text: str) -> dict[str, bytes]:
    return {
        "logo.png": _logo_png(color_hex, text, 160, 50),
        "logo@2x.png": _logo_png(color_hex, text, 320, 100),
        "logo@3x.png": _logo_png(color_hex, text, 480, 150),
    }


def _background_png(color_hex: str, px_w: int, px_h: int) -> bytes:
    # Vertical gradient from the pass color down to a darker shade of it, so the
    # poster has some depth without needing caller-supplied artwork.
    r, g, b = _hex_to_rgb(color_hex)
    img = Image.new("RGB", (px_w, px_h))
    draw = ImageDraw.Draw(img)
    for y in range(px_h):
        t = 0.55 * y / max(1, px_h - 1)
        draw.line([(0, y), (px_w, y)], fill=(int(r * (1 - t)), int(g * (1 - t)), int(b * (1 - t))))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def background_set(color_hex: str) -> dict[str, bytes]:
    """Poster background, 345x505pt (Apple's required size)."""
    return {
        "background.png": _background_png(color_hex, 345, 505),
        "background@2x.png": _background_png(color_hex, 690, 1010),
        "background@3x.png": _background_png(color_hex, 1035, 1515),
    }


def primary_logo_set(color_hex: str, text: str) -> dict[str, bytes]:
    """Poster primary logo, max 126x30pt."""
    return {
        "primaryLogo.png": _logo_png(color_hex, text, 126, 30),
        "primaryLogo@2x.png": _logo_png(color_hex, text, 252, 60),
        "primaryLogo@3x.png": _logo_png(color_hex, text, 378, 90),
    }


def decode_b64_png(b64_data: str) -> bytes:
    return base64.b64decode(b64_data)
