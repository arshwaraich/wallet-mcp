"""Shared test plumbing: check(), free_port(), a real server on a throwaway db, and a fake x402 facilitator.
Each test script imports this (tests/ is on sys.path when a script runs directly) and stays runnable on its own:
.venv/bin/python tests/test_x.py. tests/run_all.sh runs them all."""
import atexit, json, os, shutil, socket, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx2 as httpx  # the mcp SDK v2 ships httpx2

ROOT = Path(__file__).resolve().parent.parent
NETWORK = "eip155:8453"
PAY_TO = "0x000000000000000000000000000000000000dEaD"
fails = 0


def check(name, cond, detail=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f"  -- {str(detail)[:2000]}" if detail and not cond else ""))
    fails += 0 if cond else 1


def finish(summary=None):
    """Print the tally and exit 1 if any check failed."""
    print(summary if summary is not None else f"\n{'ALL PASSED' if not fails else f'{fails} FAILED'}")
    sys.exit(1 if fails else 0)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def tempdir() -> Path:
    """A fresh directory removed at exit."""
    d = Path(tempfile.mkdtemp(prefix="wallet-mcp-test-"))
    atexit.register(shutil.rmtree, d, True)
    return d


class Server:
    """server.py on a spare port with a throwaway db, downloads under /var/tmp (/tmp is a small tmpfs that trips
    the low-disk guard), previews and the exempt list in a temp dir, and APNs pointed at a dead port, so nothing
    touches production. env overrides those defaults; clean_env drops inherited WALLET_MCP_* variables first.
    db_path lets a test prepare the database before the server starts; start=False leaves starting it (start())
    to the test, which can use self.env and self.tmp meanwhile."""

    def __init__(self, env=None, *, clean_env=False, db_path=None, start=True):
        self.tmp = tempdir()
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.db_path = Path(db_path) if db_path else self.tmp / "test.db"
        self.downloads = Path("/var/tmp") / f"wallet-mcp-test-dl-{self.tmp.name}"
        atexit.register(shutil.rmtree, self.downloads, True)
        self.log = self.tmp / "server.log"
        base_env = {k: v for k, v in os.environ.items() if not (clean_env and k.startswith("WALLET_MCP_"))}
        self.env = dict(base_env, PORT=str(self.port), WALLET_MCP_DB=str(self.db_path),
                        WALLET_MCP_DOWNLOADS=str(self.downloads), WALLET_MCP_PREVIEWS=str(self.tmp / "previews"),
                        WALLET_MCP_EXEMPT_FILE=str(self.tmp / "exempt.txt"),
                        WALLET_MCP_SITE_DB=str(self.tmp / "site.db"), WALLET_MCP_APNS_URL="http://127.0.0.1:9",
                        WALLET_MCP_PUBLIC_URL=self.base, WALLET_MCP_PUBLIC_HOST=f"127.0.0.1:{self.port}")
        self.env.update(env or {})
        self.proc = None
        if start:
            self.start()

    def start(self):
        self.proc = subprocess.Popen([sys.executable, str(ROOT / "server.py")], env=self.env, cwd=ROOT,
                                     stdout=subprocess.DEVNULL, stderr=open(self.log, "w"))
        atexit.register(self.stop)
        for _ in range(100):
            try:
                httpx.get(f"{self.base}/api/stats", timeout=1)
                break
            except httpx.HTTPError:
                time.sleep(0.1)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=10)


class Facilitator:
    """A fake x402 facilitator in this process. mode decides what /verify and /settle answer; seen records
    every POST as (path, body); settled transactions are named tx."""

    def __init__(self, tx="0xfeed"):
        self.mode = {"verify": True, "settle": True}
        self.seen = []
        fac = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, body):
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._send({"kinds": [{"x402Version": 2, "scheme": "exact", "network": NETWORK}],
                            "extensions": [], "signers": {}})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                fac.seen.append((self.path, body))
                if self.path.endswith("/verify"):
                    self._send({"isValid": True, "payer": "0xpayer"} if fac.mode["verify"]
                               else {"isValid": False, "invalidReason": "insufficient_funds", "payer": "0xpayer"})
                elif fac.mode["settle"]:
                    self._send({"success": True, "transaction": tx, "network": NETWORK, "payer": "0xpayer"})
                else:
                    self._send({"success": False, "errorReason": "nonce_used", "transaction": "", "network": NETWORK})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def shutdown(self):
        self.httpd.shutdown()
