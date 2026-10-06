# wallet-mcp: free Apple Wallet pass generator for Claude, ChatGPT and any MCP client

Turn a PDF boarding pass, ticket, coupon or loyalty card into a signed Apple Wallet pass (`.pkpass`) from an AI assistant, with no Apple Developer account. wallet-mcp is the open-source server behind [walletmcppass.com](https://walletmcppass.com): an MCP server with two tools, `create_wallet_pass` and `update_wallet_pass`, and the same pass builder as a REST API.

| | |
|---|---|
| MCP endpoint | `https://walletmcppass.com/mcp` (Streamable HTTP) |
| REST API | `POST https://walletmcppass.com/api/passes`, `PATCH .../api/passes/{serial}` |
| Sign-in | None. No account or API key |
| Price | Free |
| Limits | 30 passes a day per IP address, 500 a day in total |
| Pass types | Boarding pass, event ticket, coupon, store card, generic (+ iOS 27 poster layout) |
| Works with | Claude, ChatGPT, Codex, the OpenAI Responses API, Claude Code, any Streamable HTTP MCP client |
| Output | A download link, valid for 1 hour, that opens "Add to Apple Wallet" on an iPhone |
| Updates | Optional: `updatable=True` passes can be changed for 1 hour, or up to 30 days with `updatable_hours`, and installed copies refresh |

## Quick start

**Claude** (web, desktop and mobile; any plan, Free allows one custom connector): open **Customize → Connectors**, click **+ Add → Add custom connector**, name it `walletmcppass`, paste `https://walletmcppass.com/mcp`, choose **No sign in** and click **Add**. In a chat, click **+ → Connectors** and switch it on. [Full steps](https://walletmcppass.com/use-with-claude).

**ChatGPT**: go to [chatgpt.com/plugins](https://chatgpt.com/plugins), select **+ → Add custom MCP server**, name it `walletmcppass`, enter `https://walletmcppass.com/mcp` under **Connection**, set authentication to none and select **Create as a plugin**. In a chat, type `@` and pick walletmcppass. Availability depends on your plan and workspace settings. [Full steps](https://walletmcppass.com/use-with-chatgpt).

**Codex**

```sh
codex mcp add walletmcppass --url https://walletmcppass.com/mcp
```

**Claude Code**

```sh
claude mcp add --transport http walletmcppass https://walletmcppass.com/mcp
```

**OpenAI Responses API**

```python
from openai import OpenAI

client = OpenAI()
response = client.responses.create(
    model="gpt-6-astra",
    tools=[{
        "type": "mcp",
        "server_label": "walletmcppass",
        "server_description": "Creates signed Apple Wallet passes.",
        "server_url": "https://walletmcppass.com/mcp",
        "require_approval": "never",
    }],
    input="Make an Apple Wallet coupon for 20% off at Harbor Café, valid until 31 December, with the QR code CAFE20.",
)
print(response.output_text)
```

**Any other MCP client**: add a remote Streamable HTTP server at `https://walletmcppass.com/mcp`, with no headers. **No MCP client?** Use the [REST API](#the-rest-api-post-apipasses).

Then ask for a pass:

- "Make an Apple Wallet boarding pass from this PDF." (with the PDF attached)
- "Turn my climbing gym card into a Wallet pass. Member number 004217, Code 128 barcode."
- "Create an event ticket for Blue Note on Friday at 9pm, seat B12, with this QR code text: TICKET-0042."

## PDF boarding pass to Apple Wallet

Give your assistant the airline's PDF or a screenshot and it builds a Wallet boarding pass. **Boarding passes are built from your real airline-issued barcode**: the pass carries the same barcode data as your PDF (usually a PDF417 or Aztec code holding an IATA BCBP string), which is what security and gate scanners read. The service doesn't issue tickets, check you in or contact airlines, and the pass won't receive gate changes. Assistants read a PDF's printed text reliably but can't always decode the barcode image; if yours can't, scan it with a barcode scanner app and paste the text.

## Compared with Create a Pass in iOS 27

Wallet in iOS 27 can scan a card or fill in a Standard, Membership or Event template on the iPhone, which covers a single simple card. wallet-mcp adds:

- boarding pass, coupon and store card layouts, Apple's iOS 26 semantic boarding pass and the iOS 27 poster layout;
- passes built by an assistant or app from a PDF, an email or a sentence, instead of a form on the phone;
- control over the barcode format (QR, PDF417, Aztec, Code 128 everywhere; Code 39, Codabar, EAN-13, ITF on iOS 27), colors, logo, images and back-of-pass text;
- a signed `.pkpass` file you can share, or add on an iPhone that isn't running iOS 27.

## Privacy, terms and support

walletmcppass.com is an independent project, not affiliated with Apple. Pass contents aren't written to the request log, and pass files are deleted when their link expires after 1 hour; the request log keeps only the time, IP address, pass style and organization name. Updatable passes are stored for their update window (1 hour by default, at most 30 days) and then deleted. Questions and bug reports: [GitHub issues](https://github.com/arshwaraich/wallet-mcp/issues).

## Why this exists

Building a `.pkpass` by hand means writing `pass.json`, generating icon/logo art, hashing every file into a manifest, signing it with an Apple Pass Type ID certificate, and zipping it up flat — a fiddly, easy-to-get-wrong process (see the "gotchas" below). This wraps that whole pipeline behind a single MCP tool call, so an LLM agent (or you, through one) can just ask for a pass.

## How it works

One process, one port, four things on it:

- **The MCP endpoint** (Streamable HTTP) at `/mcp` — the `create_wallet_pass` tool.
- **The REST API** at `POST /api/passes` — the same pass builder for non-MCP callers. It shares the rate limits and request log with the tool; each logged request records which one it came through (`source` = `mcp` or `api`).
- **A usage dashboard** at `/` and `/api/stats` — request counts, success/error rate, recent activity, all read from a local SQLite log. The hosted instance doesn't expose these publicly.
- **Short-lived download links** at `/download/{token}` — built passes are served with the correct `application/vnd.apple.pkpass` content type (required for Wallet to recognize the file) and expire after 1 hour, swept by file mtime so a crash/restart can't leak files.

```
server.py        MCPServer instance: the tool + custom HTTP routes (REST API, dashboard, stats, downloads)
pass_builder.py  Builds pass.json per style, signs the manifest with openssl smime, zips the .pkpass
image_gen.py     Pillow-based icon/logo generation (colored square + initials / wordmark)
db.py            SQLite request log + per-IP and global daily rate limits
dashboard.html   Static dashboard page, polls /api/stats every 15s
wallet-mcp.service   Example systemd unit (Restart=on-failure, boots enabled)
```

## The tool: `create_wallet_pass`

Annotated with `title: "Create Apple Wallet pass"`, `readOnlyHint: false` and `destructiveHint: false`: it only ever creates a new pass file.

```
style: "boardingPass" | "eventTicket" | "coupon" | "generic" | "storeCard"
organization_name, description        # required
logo_text, transit_type               # transit_type only applies to boardingPass ("Air" default)
barcode_message, barcode_format       # format name or ordered list, e.g. ["EAN13", "Code128"]; omit message for no barcode
                                       # "QR" (default) | "PDF417" | "Aztec" | "Code128"; iOS 27+: "Code39" | "Codabar" | "EAN13" | "ITF"
                                       # Wallet shows the first listed format the device supports; no fallback is added
barcode_alt_text                      # human-readable text under the barcode
background_color, foreground_color, label_color   # hex "#1a1a19" or "rgb(26, 26, 25)"
relevant_date, expiration_date        # ISO 8601
locations                              # up to 10 of {latitude, longitude, altitude?, relevantText?}: show on the lock screen nearby
max_distance                           # meters; can only shrink Wallet's default radius around each location
voided                                 # stamps the pass VOID
primary_fields, secondary_fields, auxiliary_fields, header_fields, back_fields
                                       # lists of {key?, label?, value}
serial_number                         # auto-generated UUID if omitted
icon_color, icon_text, logo_color     # control the auto-generated art
icon_png_b64, logo_png_b64            # supply your own PNG instead of auto-generated art
generate_logo                          # False = no logo image (e.g. when logo_text alone is enough)

# iOS 27+
poster                                 # Poster Generic layout; style must be generic/storeCard/coupon (kept as fallback)
footer_fields                          # up to 2, poster only
background_png_b64                     # poster background (1035x1515 px); gradient auto-generated if omitted
featured_actions                       # up to 2 of {type, url}, e.g. {"type": "membershipBenefits", "url": "https://..."}

# Semantic tags and layouts
semantics                              # Apple SemanticTags dict, emitted as given; unknown keys and bad types rejected
semantic_layout                        # boardingPass (airline, iOS 26+) -> semanticBoardingPass; eventTicket -> posterEventTicket
                                       # required tags are checked; the classic style stays as the fallback, so keep its fields
info_links                             # top-level event-guide / airline-page links: bagPolicyURL, changeSeatURL, ...

# Extra images (base64 PNG); which one shows depends on style and iOS version, see the tool docstring
primary_logo_png_b64, secondary_logo_png_b64, strip_png_b64, thumbnail_png_b64, artwork_png_b64
```

Returns `{ download_url, expires_in_seconds, serial_number, pass_type_identifier }`. The download link is valid for one hour. Open it in Safari on the iPhone; Wallet won't add a `.pkpass` handed over by another app. Google Wallet on Android imports the same file.

## The REST API: `POST /api/passes`

The JSON body takes exactly the tool's parameters above. The request model is generated from the same function signature, so the two can't drift apart. Unknown keys are rejected.

```sh
curl https://walletmcppass.com/api/passes \
  -H 'Content-Type: application/json' \
  -d '{"style": "generic", "organization_name": "Harbor Climbing Gym", "description": "Day pass",
       "primary_fields": [{"label": "Guest", "value": "Ada L."}], "barcode_message": "DAY-0042"}'
```

| Status | Body |
|---|---|
| 200 | Same as the tool: `{ download_url, expires_in_seconds, serial_number, pass_type_identifier }` |
| 400 | `{"error": "invalid request", "details": [...]}` for a malformed body or wrong types; `{"error": "..."}` when the pass itself is invalid |
| 429 | `{"error": "rate limit exceeded: ..."}` — the same limit as MCP, counted across both |
| 500 | `{"error": "internal error building the pass"}` |

As with the tool, bodies that fail schema validation are rejected before the rate limiter and aren't logged; pass-builder errors are logged and do count.

## Updating a pass: `update_wallet_pass` / `PATCH /api/passes/{serial}`

Create the pass with `updatable=True`, and optionally `updatable_hours` (1 to 720, default 1) for how long it can be updated. The response adds `updatable_until` and an `edit_token`, a secret that is the only way to change that pass (no accounts; whoever holds the token owns the pass). The pass carries a `webServiceURL` (`{PUBLIC_BASE_URL}/passkit`) and its own `authenticationToken`, so every iPhone that adds it registers with the server.

An update takes `serial_number`, `edit_token` and any of the create parameters. Given parameters replace the stored ones whole (a fields list replaces the list); omitted ones are kept. The server re-signs the pass, stores it, and sends an empty APNs push to each registered device. Wallet then fetches the new version. `voided=True` is how you cancel a pass. A field's `changeMessage` (for example `"You now have %@ points"`) shows on the lock screen when that field's value changes. Wallet matches fields by `key`, so give changing fields an explicit key.

```sh
curl -X PATCH https://walletmcppass.com/api/passes/$SERIAL \
  -H "Authorization: Bearer $EDIT_TOKEN" -H 'Content-Type: application/json' \
  -d '{"primary_fields": [{"key": "points", "label": "Points", "value": "150", "changeMessage": "You now have %@ points"}]}'
```

It returns the create response plus `notified_devices` and `updatable_until` (pushes APNs accepted, not devices that have refreshed yet). The status codes are 200, 400, 401 (no bearer token), 404 (unknown serial, wrong token, or update window over, deliberately the same) and 429. Updates count towards the same daily limits as creates.

Notes:
- Only updatable passes store their contents (in the `passes` table, with the edit token as a SHA-256 hash). The window is counted from creation and updates don't extend it. When it ends, the pass is treated as gone at once, and the download sweeper deletes the row and its device registrations within 30 seconds. Copies in Wallet keep their last version. Image URLs are fetched once and stored as PNG, so an update never refetches them.
- The PassKit web service (`/passkit/v1/...`) implements Apple's protocol: register and unregister a device, list changed serials (`passesUpdatedSince` is the pass's integer `updated` time), fetch the latest pass (`If-Modified-Since` gives 304), and log. Each pass accepts up to 100 devices.
- APNs uses the pass-signing certificate over HTTP/2 via `curl`, because the venv has no HTTP/2 client. `WALLET_MCP_APNS_URL` overrides the endpoint, and the tests use that to point it at a fake. A push that gets 400 or 410 removes that device's token.
- nginx must forward `/passkit/` and `/api/passes/` to the backend, alongside `/mcp`, `/download/` and `= /api/passes`.
- Tests: `tests/test_updates.py`.

## Image URLs

Every `*_png_b64` parameter also accepts an `https://` URL. `image_fetch.py` fetches it with SSRF guards: https only; every resolved address must be globally routable (so no loopback, private, link-local or CGNAT/tailnet addresses); the connection is pinned to the checked IP; redirects are re-checked (at most 3); and the limits are 5 MB, 25 megapixels and 10 s per read. JPEG, WebP, GIF and ICO are converted to PNG. Base64 input keeps its old rule, which is PNG only.

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
5. Put a reverse proxy (nginx, Caddy, etc.) in front of it with TLS, exposing `/mcp`, `/download/` and `/api/passes` (but not `/` or `/api/stats`, which are the private dashboard). If you use nginx, the Streamable HTTP transport needs:
   ```
   proxy_http_version 1.1;
   proxy_set_header Connection "";
   proxy_buffering off;
   proxy_read_timeout 3600s;
   ```
   Buffering has to be off or the SSE stream stalls through the proxy.
6. Run it under a process supervisor so it survives crashes and reboots — `wallet-mcp.service` in this repo is a working example (systemd, `Restart=on-failure`, `WantedBy=multi-user.target`).

## Rate limiting

No API key — anyone with the URL can call the tool or the REST API. Protected only by daily caps in `db.py`: 30 requests/day per caller IP, 500/day globally as a backstop, counted across MCP and REST calls combined (override with `WALLET_MCP_IP_LIMIT` / `WALLET_MCP_GLOBAL_LIMIT`; `WALLET_MCP_DB` moves the sqlite file, which the tests use). If you put this behind a hosted MCP client (e.g. a claude.ai connector) rather than direct calls, be aware many end users can share one apparent IP on the server side, so the per-caller cap won't isolate them individually — the global cap is what actually protects you in that case.

## Gotchas worth knowing before you touch pass_builder.py or image_gen.py

- `barcodes` is a top-level key in `pass.json`, a sibling of `boardingPass`/`eventTicket`/etc — not nested inside the style dict. Nesting it there is silently ignored: the pass installs fine, but there's no barcode anywhere on the card.
- Serve `.pkpass` files with `Content-Type: application/vnd.apple.pkpass` explicitly. Both Python's `mimetypes` module and most static file servers don't know this extension and fall back to `application/octet-stream`, which makes Wallet (and Mail/browsers) treat it as a generic download instead of offering "Add to Apple Wallet."
- Field keys must be unique across a pass's sections. Auto-generated keys used to restart at `field0` in every section; iOS rejects many such passes outright (the pass just won't open), though some combinations slip through, which hides the bug. Keys are now section-prefixed (`primary0`, `secondary0`, ...) with collisions suffixed.
- The Interleaved 2 of 5 barcode format string is `PKBarcodeFormatI2of5`, not `PKBarcodeFormatITF` as several WWDC26 write-ups claim. [apple/pass-builder](https://github.com/apple/pass-builder) is the authoritative reference for the iOS 27 keys.
- Apple's docs and Apple's own pass-builder code disagree on the semantic boarding pass time-zone keys: `departureLocationTimeZone` (docs) vs `departureAirportTimeZone` (code), same for destination. Both are accepted and emitted as given; send both.
- Apple's Pass Builder validator flags a poster event ticket without NFC as an error, and the docs say the design isn't meant for barcode entry. NFC needs an Apple entitlement this deployment doesn't have, so those passes may show as classic event tickets.
- Some barcodes (e.g. many airline boarding passes) are Aztec codes, not QR — visually similar but with one bullseye finder pattern instead of three corner squares. Match `barcode_format` to what you're actually encoding.

## Status

Live at [walletmcppass.com](https://walletmcppass.com). See `wallet-mcp.service` for the deployment shape it runs under.
