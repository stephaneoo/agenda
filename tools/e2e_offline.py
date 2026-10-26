"""End-to-end check of offline reading in headless Chromium (nix run .#e2e, Linux).

    AGENDA_E2E_ROOT=<repo or flake source> postgis-fixture python tools/e2e_offline.py <venv python>

The root is AGENDA_E2E_ROOT, never the working directory or git: on the host (a deploy from
a Mac) it is the flake source in the Nix store. Without it, or without the fixtures under it,
the run refuses.

Seeds a database (tools/e2e_seed.py), starts the app on 127.0.0.1, then in Chromium:
loads /, waits until the service worker controls the page and the IndexedDB copy is saved,
visits one occurrence page, goes offline, reloads, and checks the list, the banner, the
commune and radius filters (against tests/fixtures/filter-cases.json), a visited and an
unvisited occurrence page, and that no Content-Security-Policy violation happened. Then a
visited page is hidden on the server; once the copy is refreshed online, it is no longer
served offline.
The browser's clock is fixed at 2026-11-01 09:00 Paris, the fixture's "today".
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

FIXTURE_PATH = "tests/fixtures/filter-cases.json"
FIXTURE: dict = {}


def resolve_root(env) -> Path:
    """The repository the e2e runs from: AGENDA_E2E_ROOT, which must hold the fixtures."""
    value = env.get("AGENDA_E2E_ROOT") or ""
    if not value or not Path(value).is_dir():
        raise SystemExit(f"e2e: AGENDA_E2E_ROOT must name the repository or flake source (got {value!r})")
    root = Path(value)
    if not (root / FIXTURE_PATH).is_file():
        raise SystemExit(f"e2e: {root / FIXTURE_PATH} not found: AGENDA_E2E_ROOT is not the repository")
    return root
PORT = int(os.environ.get("E2E_PORT", "8765"))
BASE = f"http://127.0.0.1:{PORT}"
NOW = datetime(2026, 11, 1, 9, 0, tzinfo=ZoneInfo("Europe/Paris"))
SPANNING = {"expo", "until4"}  # over 24 h: in the collapsed "En cours" group
CASES = {  # (commune, rayon) -> the shared case with the same answer for 1-30 November
    ("26220", "0"): "radius 0 keeps the origin only",
    ("26220", "10"): "radius 10 from Nyons",
    ("26220", "30"): "radius 30 from Nyons",
    ("84138", "0"): "a commune alone is that commune only",
}
CSP_PROBE = """
window.__csp = [];
document.addEventListener("securitypolicyviolation",
  (e) => window.__csp.push(`${e.violatedDirective} ${e.blockedURI}`));
"""


def check(condition: bool, what: str) -> None:
    if not condition:
        raise SystemExit(f"e2e: FAILED: {what}")
    print(f"e2e: ok: {what}")


def _listening_inodes(port: int, proc: Path = Path("/proc")) -> set[str]:
    """The socket inodes listening on 127.0.0.1:`port` (or any address) in this network
    namespace, from /proc/net/tcp (Linux only, as is the e2e)."""
    found = set()
    for table in ("tcp", "tcp6"):
        try:
            lines = (proc / "net" / table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            f = line.split()
            if len(f) > 9 and f[3] == "0A" and int(f[1].rsplit(":", 1)[1], 16) == port:
                found.add(f[9])
    return found


def _socket_inodes(pid: int, proc: Path = Path("/proc")) -> set[str]:
    found = set()
    try:
        fds = list((proc / str(pid) / "fd").iterdir())
    except OSError:
        return found
    for fd in fds:
        try:
            target = os.readlink(fd)
        except OSError:
            continue
        if target.startswith("socket:["):
            found.add(target[8:-1])
    return found


def owns_port(pid: int, port: int, proc: Path = Path("/proc")) -> bool:
    """Whether every listener on `port` is a socket of process `pid`: the server answering is
    the e2e's own child, not something else that took the port first."""
    listening = _listening_inodes(port, proc)
    return bool(listening) and listening <= _socket_inodes(pid, proc)


def wait_for_server(server: subprocess.Popen) -> None:
    for _ in range(100):
        if server.poll() is not None:
            raise SystemExit("e2e: the app exited")
        try:
            with urllib.request.urlopen(f"{BASE}/healthz", timeout=2) as r:
                if r.status == 200:
                    if server.poll() is not None or not owns_port(server.pid, PORT):
                        raise SystemExit(f"e2e: something else answers on port {PORT}, not the e2e's app")
                    return
        except OSError:
            time.sleep(0.3)
    raise SystemExit("e2e: the app did not answer /healthz")


def wait_until(page, expression: str, timeout: float = 30) -> None:
    """Poll `expression` with page.evaluate. Playwright's wait_for_function would compile it
    in the page, which the site's CSP (no eval) rightly refuses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if page.evaluate(expression):
            return
        time.sleep(0.2)
    raise SystemExit(f"e2e: FAILED: timed out waiting for {expression}")


def titles(page, selector: str) -> list[str]:
    return page.eval_on_selector_all(selector, "els => els.map((e) => e.textContent.trim())")


def main(venv_python: str) -> None:
    from playwright.sync_api import sync_playwright

    global FIXTURE
    root = resolve_root(os.environ)
    os.chdir(root)
    FIXTURE = json.loads((root / FIXTURE_PATH).read_text(encoding="utf-8"))
    subprocess.run([venv_python, "-m", "tools.e2e_seed"], check=True)
    if _listening_inodes(PORT):
        raise SystemExit(f"e2e: port {PORT} is already taken")
    server = subprocess.Popen([venv_python, "manage.py", "runserver", "--noreload", f"127.0.0.1:{PORT}"])
    violations: list[str] = []
    try:
        wait_for_server(server)
        with sync_playwright() as p:
            # Chromium's own sandbox whenever it can have one: not as root (the dev VM's
            # check), where Chromium refuses it.
            browser = p.chromium.launch(chromium_sandbox=os.geteuid() != 0)
            context = browser.new_context(locale="fr-FR", timezone_id="Europe/Paris")
            context.clock.set_fixed_time(NOW)
            context.add_init_script(CSP_PROBE)
            page = context.new_page()
            page.on("console", lambda m: violations.append(m.text)
                    if m.type == "error" and "Content Security Policy" in m.text else None)

            def collect() -> None:
                violations.extend(page.evaluate("window.__csp || []"))

            page.goto(f"{BASE}/")
            wait_until(page, "navigator.serviceWorker.controller !== null")
            page.wait_for_selector("html[data-offline-copy=saved]", state="attached", timeout=30_000)
            check(True, "service worker in control, IndexedDB copy saved")
            items = page.evaluate("fetch('/api/v1/occurrences?from=2026-11-01&to=2026-11-30&limit=200')"
                                  ".then((r) => r.json()).then((j) => j.items)")
            url = {o["name"]: o["url"] for o in items}
            path = lambda name: "/" + url[name].split("/", 3)[3]  # noqa: E731
            collect()
            for name in ("dateonly", "next"):
                page.goto(f"{BASE}{path(name)}")  # visited online: cached by the worker
                wait_until(page, f"caches.match({json.dumps(path(name))}).then(Boolean)", 10)
            collect()

            context.set_offline(True)
            page.goto(f"{BASE}/")
            page.wait_for_selector("#offline-banner:not([hidden])", timeout=15_000)
            banner = page.text_content("#offline-banner")
            check("Hors ligne" in banner and "Données du 31 octobre 2026" in banner and "copie du 1 novembre 2026" in banner,
                  f"offline banner: {banner!r}")
            check(len(titles(page, "#results a.title")) > 0, "the list renders offline")
            page.select_option("select[name=periode]", "30j")
            for (commune, rayon), name in CASES.items():
                page.select_option("select[name=commune]", commune)
                page.select_option("select[name=rayon]", rayon)
                expected = next(c["expected"] for c in FIXTURE["cases"] if c["name"] == name)
                days = titles(page, "#results section.day a.title")
                ongoing = titles(page, "#results details.ongoing a.title")
                check(days == [n for n in expected if n not in SPANNING] and set(ongoing) == SPANNING & set(expected),
                      f"offline filter {commune} {rayon} min: {name}")
            check("rayon=0" in page.evaluate("location.search"), "the URL follows the filters")
            collect()

            page.goto(f"{BASE}{path('dateonly')}")
            check(page.text_content("main h1") == "dateonly", "a visited occurrence page, offline, from the cache")
            collect()
            page.goto(f"{BASE}{path('late')}")
            page.wait_for_selector("article.occurrence h1", timeout=15_000)
            check(page.text_content("article.occurrence h1") == "late", "an unvisited occurrence page, offline, from IndexedDB")
            collect()

            # A visited page hidden on the server: the next copy saved online drops it from the
            # cache (the copy is fresh for 10 minutes, so the clock moves on first).
            subprocess.run([venv_python, "-m", "tools.e2e_seed", "--hide", "next"], check=True)
            context.set_offline(False)
            context.clock.set_fixed_time(NOW + timedelta(minutes=11))
            page.goto(f"{BASE}/")
            page.wait_for_selector("html[data-offline-copy=saved]", state="attached", timeout=30_000)
            hidden = json.dumps(path("next"))
            wait_until(page, f"caches.open('agenda-pages').then((c) => c.match({hidden})).then((r) => !r)", 10)
            check(True, "a page hidden on the server is dropped from the cache when the copy is refreshed")
            context.set_offline(True)
            page.goto(f"{BASE}{path('next')}")
            page.wait_for_selector("main p.empty", timeout=15_000)
            check("next" not in titles(page, "main h1") and "pas été enregistrée" in page.text_content("main"),
                  "the hidden page is not served offline")
            collect()
            browser.close()
        check(not violations, f"no Content-Security-Policy violation {violations}")
    finally:
        server.terminate()
        server.wait(timeout=10)


if __name__ == "__main__":
    main(sys.argv[1])
