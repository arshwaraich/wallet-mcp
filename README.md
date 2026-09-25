# wallet-mcp

An MCP server that signs [Apple Wallet](https://developer.apple.com/wallet/) (`.pkpass`) passes on request. Point any MCP client (Claude Desktop, Claude.ai custom connector, or anything else speaking Streamable HTTP MCP) at it and ask it to build a boarding pass, event ticket, coupon, store card, or generic pass — it comes back with a signed pass and a temporary download link.

**Hosted instance:** [walletmcppass.com](https://walletmcppass.com) — MCP endpoint at `https://walletmcppass.com/mcp`, free during open beta.

Free to call, rate-limited, with a small usage dashboard. No API key today; a payment layer is meant to slot in later without changing the tool's interface.

## Why this exists

Building a `.pkpass` by hand means writing `pass.json`, generating icon/logo art, hashing every file into a manifest, signing it with an Apple Pass Type ID certificate, and zipping it up flat — a fiddly, easy-to-get-wrong process (see the "gotchas" below). This wraps that whole pipeline behind a single MCP tool call, so an LLM agent (or you, through one) can just ask for a pass.

## How it works

One process, one port, three things on it:

- **The MCP endpoint** (Streamable HTTP) at `/mcp` — the `create_wallet_pass` tool.
- **A usage dashboard** at `/` and `/api/stats` — request counts, success/error rate, recent activity, all read from a local SQLite log.
- **Short-lived download links** at `/download/{token}` — built passes are served with the correct `application/vnd.apple.pkpass` content type (required for Wallet to recognize the file) and expire after 1 hour, swept by file mtime so a crash/restart can't leak files.

```
server.py        MCPServer instance: the tool + custom HTTP routes (dashboard, stats, downloads)
pass_builder.py  Builds pass.json per style, signs the manifest with openssl smime, zips the .pkpass
image_gen.py     Pillow-based icon/logo generation (colored square + initials / wordmark)
db.py            SQLite request log + per-IP and global daily rate limits
dashboard.html   Static dashboard page, polls /api/stats every 15s
wallet-mcp.service   Example systemd unit (Restart=on-failure, boots enabled)
```

## The tool: `create_wallet_pass`

```
style: "boardingPass" | "eventTicket" | "coupon" | "generic" | "storeCard"
organization_name, description        # required
logo_text, transit_type               # transit_type only applies to boardingPass ("Air" default)
barcode_message, barcode_format       # "QR" (default) | "PDF417" | "Aztec" | "Code128"; omit message for no barcode
                                       # iOS 27+: "Code39" | "Codabar" | "EAN13" | "ITF" (auto QR fallback for older iOS)
barcode_alt_text                      # human-readable text under the barcode
background_color, foreground_color, label_color   # hex "#1a1a19" or "rgb(26, 26, 25)"
relevant_date, expiration_date        # ISO 8601
voided                                 # stamps the pass VOID
primary_fields, secondary_fields, auxiliary_fields, header_fields, back_fields
                                       # lists of {key?, label?, value}
serial_number                         # auto-generated UUID if omitted
icon_color, icon_text, logo_color     # control the auto-generated art
icon_png_b64, logo_png_b64            # supply your own PNG instead of auto-generated art

# iOS 27+
poster                                 # Poster Generic layout; style must be generic/storeCard/coupon (kept as fallback)
footer_fields                          # up to 2, poster only
background_png_b64                     # poster background (1035x1515 px); gradient auto-generated if omitted
featured_actions                       # up to 2 of {type, url}, e.g. {"type": "membershipBenefits", "url": "https://..."}
```

Returns `{ download_url, expires_in_seconds, serial_number, pass_type_identifier }`. The download link is valid for one hour.

## Deploying your own instance

You need your own Apple Developer **Pass Type ID certificate** — the one in this repo's design (`pass_builder.py`) is not included, and can't be, since it's a private key tied to a specific Apple Developer account. To stand this up:

1. Issue a Pass Type ID certificate in the Apple Developer portal, export the private key + signed cert as PEM, and download Apple's WWDR G4 intermediate cert.
2. In `pass_builder.py`, point `SIGNING_DIR`/`PASSKEY`/`CERT`/`WWDR` at your files, and set `PASS_TYPE_IDENTIFIER`/`TEAM_IDENTIFIER` to your own.
3. Set up the Python environment (this project has no `pip`/`venv` assumptions baked in beyond standard tooling):
   ```
   uv venv .venv
   uv pip install --python .venv/bin/python "mcp[cli]" pillow uvicorn starlette
   ```
4. Run it:
   ```
   PORT=8400 WALLET_MCP_PUBLIC_URL=https://your-domain/wallet-mcp \
     WALLET_MCP_PUBLIC_HOST=your-domain \
     .venv/bin/python server.py
   ```
   `WALLET_MCP_PUBLIC_HOST` matters: the MCP SDK has DNS-rebinding protection on by default and will reject every request with a 421 unless the request's `Host` header is in the allowed list — set this to whatever hostname the server is actually reached at.
5. Put a reverse proxy (nginx, Caddy, etc.) in front of it with TLS. If you use nginx, the Streamable HTTP transport needs:
   ```
   proxy_http_version 1.1;
   proxy_set_header Connection "";
   proxy_buffering off;
   proxy_read_timeout 3600s;
   ```
   Buffering has to be off or the SSE stream stalls through the proxy.
6. Run it under a process supervisor so it survives crashes and reboots — `wallet-mcp.service` in this repo is a working example (systemd, `Restart=on-failure`, `WantedBy=multi-user.target`).

## Rate limiting

No API key — anyone with the URL can call the tool. Protected only by daily caps in `db.py`: 30 requests/day per caller IP, 500/day globally as a backstop. If you put this behind a hosted MCP client (e.g. a claude.ai connector) rather than direct calls, be aware many end users can share one apparent IP on the server side, so the per-caller cap won't isolate them individually — the global cap is what actually protects you in that case.

## Gotchas worth knowing before you touch pass_builder.py or image_gen.py

- `barcodes` is a top-level key in `pass.json`, a sibling of `boardingPass`/`eventTicket`/etc — not nested inside the style dict. Nesting it there is silently ignored: the pass installs fine, but there's no barcode anywhere on the card.
- Serve `.pkpass` files with `Content-Type: application/vnd.apple.pkpass` explicitly. Both Python's `mimetypes` module and most static file servers don't know this extension and fall back to `application/octet-stream`, which makes Wallet (and Mail/browsers) treat it as a generic download instead of offering "Add to Apple Wallet."
- Field keys must be unique across a pass's sections. Auto-generated keys used to restart at `field0` in every section; iOS rejects many such passes outright (the pass just won't open), though some combinations slip through, which hides the bug. Keys are now section-prefixed (`primary0`, `secondary0`, ...) with collisions suffixed.
- The Interleaved 2 of 5 barcode format string is `PKBarcodeFormatI2of5`, not `PKBarcodeFormatITF` as several WWDC26 write-ups claim. [apple/pass-builder](https://github.com/apple/pass-builder) is the authoritative reference for the iOS 27 keys.
- Some barcodes (e.g. many airline boarding passes) are Aztec codes, not QR — visually similar but with one bullseye finder pattern instead of three corner squares. Match `barcode_format` to what you're actually encoding.

## Status

Live at [walletmcppass.com](https://walletmcppass.com). See `wallet-mcp.service` for the deployment shape it runs under.
