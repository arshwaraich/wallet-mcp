"""Building a signed .pkpass from create_wallet_pass's parameters, shared by server.py (single passes) and
pilot.py (organizer pilots and paid batches, including the pilot CLI, which must not import server).

PUBLIC_BASE_URL is baked into every updatable pass as its webServiceURL, so it must be the service's
real value (WALLET_MCP_PUBLIC_URL): a pass built with the wrong one can never receive updates.
"""
import base64
import inspect
import os

import image_fetch
import image_gen
import pass_builder

PUBLIC_BASE_URL = os.environ.get("WALLET_MCP_PUBLIC_URL", "https://vps.arshw.com/wallet-mcp")
# A pass is ~16 KB without images; each image URL can be 25 megapixels, so cap the signed result.
MAX_PKPASS_BYTES = int(os.environ.get("WALLET_MCP_MAX_PASS_KB", 10 * 1024)) * 1024
DEFAULT_ICON_COLOR = "#c98500"  # site accent color

# create_wallet_pass's image parameters (server.py asserts this matches the tool's signature).
IMAGE_PARAMS = ["icon_png_b64", "logo_png_b64", "background_png_b64", "primary_logo_png_b64",
                "secondary_logo_png_b64", "strip_png_b64", "thumbnail_png_b64", "artwork_png_b64"]
# create_wallet_pass's parameters that go into pass.json.
PASS_JSON_PARAMS = [
    name for name in inspect.signature(pass_builder.build_pass_json).parameters
    if name not in ("serial_number", "web_service_url", "authentication_token")
]


def resolve_image_urls(params: dict) -> dict:
    """Replace any https:// image URL with the fetched image as base64 PNG, so a stored
    updatable pass never needs to re-fetch a URL that may since have changed or gone."""
    out = dict(params)
    for name in IMAGE_PARAMS:
        value = out.get(name)
        if isinstance(value, str) and value.strip().lower().startswith(("https://", "http://")):
            out[name] = base64.b64encode(image_fetch.fetch_png(value.strip(), name)).decode()
    return out


def validate_pass_json(params: dict, serial: str = "check", auth_token: str | None = None) -> None:
    """Raises PassBuildError if params' pass.json doesn't build. Cheap: no images are fetched or decoded
    and nothing is signed, so it can run before a call is paid for."""
    pass_builder.build_pass_json(**{k: params.get(k) for k in PASS_JSON_PARAMS if k in params},
                                 serial_number=serial, web_service_url="https://x" if auth_token else None,
                                 authentication_token=auth_token)


def build(params: dict, serial: str, auth_token: str | None) -> bytes:
    """Build and sign a pass from create_wallet_pass parameters. auth_token makes it updatable."""
    p = params
    icon_color = p.get("icon_color") or DEFAULT_ICON_COLOR
    style, organization_name = p["style"], p["organization_name"]
    logo_text, background_color = p.get("logo_text"), p.get("background_color")
    poster, semantic_layout = p.get("poster", False), p.get("semantic_layout", False)
    generate_logo = p.get("generate_logo", True)
    pass_dict = pass_builder.build_pass_json(
        **{k: p.get(k) for k in PASS_JSON_PARAMS if k in p},
        serial_number=serial,
        web_service_url=f"{PUBLIC_BASE_URL}/passkit" if auth_token else None,
        authentication_token=auth_token,
    )

    files: dict[str, bytes] = {}
    if p.get("icon_png_b64"):
        files["icon.png"] = image_gen.decode_b64_png(p["icon_png_b64"], "icon_png_b64")
    else:
        files.update(image_gen.icon_set(icon_color, p.get("icon_text") or organization_name))
    if p.get("logo_png_b64"):
        files["logo.png"] = image_gen.decode_b64_png(p["logo_png_b64"], "logo_png_b64")
    elif generate_logo:
        files.update(image_gen.logo_set(p.get("logo_color") or icon_color, logo_text or organization_name))
    background_png_b64 = p.get("background_png_b64")
    if poster:
        if background_png_b64:
            files["background.png"] = image_gen.decode_b64_png(background_png_b64, "background_png_b64")
        else:
            files.update(image_gen.background_set(background_color or icon_color))
    elif background_png_b64 and style == "eventTicket":
        files["background.png"] = image_gen.decode_b64_png(background_png_b64, "background_png_b64")
    if semantic_layout and style == "eventTicket":
        if p.get("artwork_png_b64"):
            files["artwork.png"] = image_gen.decode_b64_png(p["artwork_png_b64"], "artwork_png_b64")
        elif not background_png_b64:  # Wallet needs one of the two
            files.update(image_gen.artwork_set(background_color or icon_color))
    if p.get("primary_logo_png_b64"):
        files["primaryLogo.png"] = image_gen.decode_b64_png(p["primary_logo_png_b64"], "primary_logo_png_b64")
    elif (poster or semantic_layout) and generate_logo:
        files.update(image_gen.primary_logo_set(p.get("foreground_color") or "#ffffff", logo_text or organization_name))
    for name, param in (("secondaryLogo", "secondary_logo_png_b64"), ("strip", "strip_png_b64"),
                        ("thumbnail", "thumbnail_png_b64")):
        if p.get(param):
            files[f"{name}.png"] = image_gen.decode_b64_png(p[param], param)

    pkpass = pass_builder.build_pkpass(pass_dict, files)
    if len(pkpass) > MAX_PKPASS_BYTES:
        raise pass_builder.PassBuildError(
            f"the pass is {len(pkpass) / 1024 ** 2:.2f} MB; the limit is {MAX_PKPASS_BYTES / 1024 ** 2:g} MB. "
            "Use smaller images: Wallet shows them at most a few hundred pixels wide")
    return pkpass
