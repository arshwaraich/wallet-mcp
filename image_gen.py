"""Auto-generated icon/logo PNGs for Wallet passes that don't supply their own art."""
import base64
import io

from PIL import Image, ImageDraw, ImageFont

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


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


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


def decode_b64_png(b64_data: str) -> bytes:
    return base64.b64decode(b64_data)
