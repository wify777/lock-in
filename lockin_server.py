#!/usr/bin/env python3
"""Lock In — локальный сервер.

• http://localhost:5173 — приложение (только этот компьютер).
  /api/state   — активное приложение на Mac, адрес вкладки браузера, простой клавиатуры и состояние телефона.
  /api/pair    — ссылка и код для подключения телефона.
  /api/session — приложение сообщает серверу таймер и статус, чтобы их показал телефон.
• https://<IP-в-Wi-Fi>:5174/phone?t=<код> — страница для телефона (та же сеть Wi-Fi).
  HTTPS нужен, чтобы телефон дал датчик движения и не гасил экран. Сертификат самоподписанный,
  поэтому телефон один раз предупредит — это нормально для локальной сети.

Ничего не уходит в интернет. Без сторонних библиотек. Запуск:  python3 lockin_server.py
"""
import json
import os
import re
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("LOCKIN_PORT", os.environ.get("SENSEI_PORT", "5173")))
PHONE_PORT = int(os.environ.get("LOCKIN_PHONE_PORT", os.environ.get("SENSEI_PHONE_PORT", "5174")))
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, ".lockin")
if not os.path.exists(DATA) and os.path.isdir(os.path.join(ROOT, ".sensei")):
    os.rename(os.path.join(ROOT, ".sensei"), DATA)  # данные старой версии (Sensei): сертификат и код телефона
IS_MAC = sys.platform == "darwin"

CHROMIUM = {"Google Chrome", "Google Chrome Canary", "Arc", "Brave Browser", "Microsoft Edge",
            "Vivaldi", "Opera", "Yandex", "Chromium", "Comet", "Dia"}
SAFARI = {"Safari", "Safari Technology Preview"}


# ---------------------------------------------------------------- Mac: активное приложение
def run(cmd, timeout=1.5):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return out.returncode, out.stdout.strip(), out.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


def frontmost():
    """lsappinfo не требует никаких разрешений."""
    code, asn, _ = run(["lsappinfo", "front"])
    if code != 0 or not asn:
        return "", ""
    _, info, _ = run(["lsappinfo", "info", asn])
    m = re.match(r'"(.+?)"', info.splitlines()[0] if info else "")
    b = re.search(r'bundleID="([^"]*)"', info)
    return (m.group(1) if m else ""), (b.group(1) if b else "")


def browser_tab(app):
    """URL и заголовок активной вкладки. macOS один раз спросит разрешение на управление браузером."""
    if app in CHROMIUM:
        s = f'tell application "{app}" to if (count of windows) > 0 then return (URL of active tab of front window) & "\\n" & (title of active tab of front window)'
    elif app in SAFARI:
        s = f'tell application "{app}" to if (count of windows) > 0 then return (URL of current tab of front window) & "\\n" & (name of current tab of front window)'
    else:
        return None, None, None
    code, out, err = run(["osascript", "-e", s], timeout=2.0)
    if code != 0:
        denied = "-1743" in err or "not allowed" in err.lower()
        return None, None, ("automation_denied" if denied else "error")
    url, _, title = out.partition("\n")
    return url, title, None


def running_apps():
    """Обычные приложения с окнами (для выбора «разрешённых» в строгом режиме)."""
    _, out, _ = run(["lsappinfo", "list"], timeout=3)
    apps = []
    for block in re.split(r"\n\s*\d+\) ", "\n" + out):
        m = re.match(r'"(.+?)"', block)
        if m and 'type="Foreground"' in block:
            apps.append(m.group(1))
    return sorted(set(apps), key=str.lower)


APP_PREFIX = f"http://localhost:{PORT}"


def enforce(action, app=None, browser=None):
    """Строгий режим: вернуть человека в разрешённое окно или закрыть запрещённый сайт."""
    if not IS_MAC:
        return False
    if action == "app" and app:
        return run(["open", "-a", app], timeout=3)[0] == 0
    if action in ("lockin", "sensei"):
        for b in [browser] + sorted(CHROMIUM | SAFARI):
            if not b or b not in (CHROMIUM | SAFARI):
                continue
            if b in SAFARI:
                body = f'repeat with w in windows\nrepeat with tb in tabs of w\nif URL of tb starts with "{APP_PREFIX}" then\nset current tab of w to tb\nset index of w to 1\nactivate\nreturn "ok"\nend if\nend repeat\nend repeat'
            else:
                body = f'repeat with w in windows\nset i to 0\nrepeat with tb in tabs of w\nset i to i + 1\nif URL of tb starts with "{APP_PREFIX}" then\nset active tab index of w to i\nset index of w to 1\nactivate\nreturn "ok"\nend if\nend repeat\nend repeat'
            script = f'if application "{b}" is running then\ntell application "{b}"\n{body}\nend tell\nend if\nreturn "none"'
            code, out, _ = run(["osascript", "-e", script], timeout=3)
            if code == 0 and out == "ok":
                return True
        return False
    if action == "block_tab" and browser in (CHROMIUM | SAFARI):
        target = f"{APP_PREFIX}/blocked.html"
        prop = "current tab" if browser in SAFARI else "active tab"
        return run(["osascript", "-e", f'tell application "{browser}" to set URL of {prop} of front window to "{target}"'], timeout=3)[0] == 0
    return False


def idle_seconds():
    _, out, _ = run(["ioreg", "-c", "IOHIDSystem"], timeout=1.5)
    m = re.search(r'"HIDIdleTime" = (\d+)', out)
    return int(m.group(1)) / 1e9 if m else None


# ---------------------------------------------------------------- общее состояние
LOCK = threading.Lock()
def load_token():
    """Код подключения телефона хранится в .lockin/token — после перезапуска сервера телефон не надо подключать заново."""
    os.makedirs(DATA, exist_ok=True)
    f = os.path.join(DATA, "token")
    if os.path.exists(f):
        tok = open(f).read().strip()
        if len(tok) >= 8:
            return tok
    tok = secrets.token_urlsafe(8)
    with open(f, "w") as fh:
        fh.write(tok)
    os.chmod(f, 0o600)
    return tok


TOKEN = load_token()
PHONE = {"seen": 0.0, "visible": False, "hidden_since": None, "pickup_at": 0.0, "moving": False, "moving_at": 0.0,
         "held": False, "hide_n": 0, "tilt": 0, "events": [], "next_id": 1, "ua": ""}
SESSION = {"ts": 0.0, "data": {}}
_mac_cache = {"t": 0.0, "data": None}


def phone_event(kind, extra=None):
    with LOCK:
        ev = {"id": PHONE["next_id"], "kind": kind, "ts": time.time(), **(extra or {})}
        PHONE["next_id"] += 1
        PHONE["events"] = (PHONE["events"] + [ev])[-50:]


def phone_view():
    """state: here — страница Lock In открыта; sleep — экран погас, когда телефон лежал (так и надо);
    held — экран погас или ушли в другое приложение, когда телефон был в руках: это может быть и «выключил»,
    и «ушёл в Telegram». Что именно — решает Mac по камере; none — не подключали."""
    now = time.time()
    with LOCK:
        paired = PHONE["seen"] > 0
        silent = paired and now - PHONE["seen"] > 6
        hidden_since = PHONE["hidden_since"]
        held = PHONE["held"]
        hide_n = PHONE["hide_n"]
        inferred = hidden_since is None and silent
        if inferred:
            # Android усыпляет страницу раньше, чем она успевает сказать «ухожу». Тишина = ушли со страницы.
            hidden_since = PHONE["seen"]
            held = PHONE["moving"] or bool(PHONE["moving_at"] and PHONE["seen"] - PHONE["moving_at"] < 3)
            hide_n += 1
        hidden = hidden_since is not None
        state = "none" if not paired else ("held" if hidden and held else "sleep" if hidden else "here")
        return {"paired": paired, "connected": paired, "visible": not hidden, "state": state, "hide_n": hide_n, "silent": inferred,
                "hidden_for": (now - hidden_since) if hidden else 0,
                "pickup_ago": (now - PHONE["pickup_at"]) if PHONE["pickup_at"] else None,
                "moving": PHONE["moving"] and not silent,
                "tilt": PHONE["tilt"], "seen_ago": now - PHONE["seen"] if paired else None,
                "events": PHONE["events"][-20:]}


def mac_state():
    with LOCK:
        if _mac_cache["data"] and time.time() - _mac_cache["t"] < 0.6:
            data = dict(_mac_cache["data"])
        else:
            data = None
    if data is None:
        if IS_MAC:
            app, bundle = frontmost()
            url, title, err = browser_tab(app)
            data = {"ok": True, "app": app, "bundle": bundle, "url": url, "title": title,
                    "browser": app in CHROMIUM or app in SAFARI, "url_error": err, "idle": idle_seconds()}
        else:
            data = {"ok": False, "reason": "not_mac"}
        with LOCK:
            _mac_cache.update(t=time.time(), data=data)
        data = dict(data)
    data["phone"] = phone_view()
    data["ts"] = time.time()
    return data


def lan_ip():
    """Адрес Mac в текущей сети. Определяем каждый раз: сеть может смениться (дом, хотспот, кафе)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # пакеты не отправляются, только выбирается сетевой интерфейс
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def all_ips():
    """Все IPv4-адреса Mac (Wi-Fi, кабель, хотспот, Tailscale) — запасные варианты для телефона."""
    _, out, _ = run(["ifconfig"], timeout=2)
    ips = [ip for ip in re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out) if not ip.startswith("127.")]
    main = lan_ip()
    return [main] + [ip for ip in ips if ip != main] if main != "127.0.0.1" else ips


CERT_LOCK = threading.Lock()
CERT_STATE = {"ips": None, "ctx": None}


def ensure_cert(ips):
    """Самоподписанный сертификат на все текущие адреса. Перевыпускается, если сеть сменилась."""
    os.makedirs(DATA, exist_ok=True)
    cert, key, ipfile = (os.path.join(DATA, n) for n in ("cert.pem", "key.pem", "cert_ip.txt"))
    want = ",".join(sorted(ips))
    same = os.path.exists(ipfile) and open(ipfile).read().strip() == want
    if os.path.exists(cert) and os.path.exists(key) and same:
        return cert, key, False
    san = ",".join([f"IP:{ip}" for ip in ips] + ["IP:127.0.0.1", "DNS:localhost"])
    code, _, err = run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert,
                        "-days", "825", "-subj", "/CN=Lock In local", "-addext", f"subjectAltName={san}"], timeout=30)
    if code != 0:
        print("Не удалось создать сертификат для телефона:", err[-300:])
        return None, None, False
    with open(ipfile, "w") as f:
        f.write(want)
    return cert, key, True


def refresh_cert():
    """Вызывается при каждом открытии QR-кода: если адрес Mac сменился — новый сертификат без перезапуска."""
    ips = all_ips()
    with CERT_LOCK:
        if CERT_STATE["ips"] == ips and CERT_STATE["ctx"]:
            return ips
        cert, key, _ = ensure_cert(ips)
        if cert and CERT_STATE["ctx"]:
            CERT_STATE["ctx"].load_cert_chain(cert, key)  # новые подключения получат новый сертификат
        CERT_STATE["ips"] = ips
    return ips


PHONE_URL = {"url": None}


# ---------------------------------------------------------------- HTTP
def send_json(h, obj, code=200):
    body = json.dumps(obj, ensure_ascii=False).encode()
    h.send_response(code)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


def read_json(h):
    n = int(h.headers.get("Content-Length") or 0)
    if n <= 0 or n > 20000:
        return {}
    try:
        return json.loads(h.rfile.read(n) or b"{}")
    except Exception:
        return {}


class Base(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        msg = fmt % args if args else str(fmt)
        if "/api/" not in msg:
            super().log_message("%s", msg)

    # общие обработчики телефона
    def phone_api(self, method):
        u = urlparse(self.path)
        if parse_qs(u.query).get("t", [""])[0] != TOKEN:
            return send_json(self, {"error": "bad_token"}, 403)
        if u.path == "/api/phone/ping" and method == "POST":
            d = read_json(self)
            now = time.time()
            with LOCK:
                prev = PHONE["seen"]
                vis = bool(d.get("visible", True))
                if prev and now - prev > 6 and PHONE["hidden_since"] is None:
                    # страница молча «спала» (Android гасит её раньше, чем она скажет «ухожу») — это тоже уход
                    PHONE["hidden_since"] = prev
                    PHONE["held"] = PHONE["moving"] or bool(PHONE["moving_at"] and prev - PHONE["moving_at"] < 3)
                    PHONE["hide_n"] += 1
                was_hidden = PHONE["hidden_since"] is not None
                PHONE["seen"] = now
                PHONE["ua"] = self.headers.get("User-Agent", "")[:120]
                PHONE["visible"] = vis
                PHONE["moving"] = bool(d.get("moving", False)) and vis
                PHONE["tilt"] = int(d.get("tilt", 0) or 0)
                if PHONE["moving"]:
                    PHONE["moving_at"] = now
                if not vis and PHONE["hidden_since"] is None:
                    PHONE["hidden_since"] = now
                    PHONE["held"] = bool(d.get("held", False))
                    PHONE["hide_n"] += 1
                ask = False
                if vis and was_hidden:
                    # Mac по камере решил, что это был не «выключил», а «пользовался» — спросим на телефоне, что это было
                    ask = SESSION["data"].get("used_hide") == PHONE["hide_n"] and time.time() - SESSION["ts"] < 5
                    PHONE["hidden_since"] = None
                    PHONE["held"] = False
            if not vis and not was_hidden:
                phone_event("hidden")
            if vis and was_hidden:
                phone_event("visible")
            for ev in d.get("events", [])[:10]:
                kind = str(ev.get("kind", ""))[:20]
                if kind in ("pickup", "call", "reason", "hello", "ask"):
                    if kind == "pickup":
                        with LOCK:
                            PHONE["pickup_at"] = now
                    phone_event(kind, {"value": str(ev.get("value", ""))[:300]})
            with LOCK:
                sess = dict(SESSION["data"]) if now - SESSION["ts"] < 5 else {"active": False}
            return send_json(self, {"ok": True, "session": sess, "ask_reason": ask})
        return send_json(self, {"error": "not_found"}, 404)


class LocalHandler(Base):
    """Только 127.0.0.1: приложение и закрытые данные о Mac."""

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/api/state":
            return send_json(self, mac_state())
        if p == "/api/pair":
            if not PHONE_URL["url"]:
                return send_json(self, {"url": None, "error": "phone_server_off"})
            ips = refresh_cert()
            urls = [f"https://{ip}:{PHONE_PORT}/phone?t={TOKEN}" for ip in ips]
            return send_json(self, {"url": urls[0] if urls else None, "alt": urls[1:], "token": TOKEN})
        if p == "/api/apps":
            return send_json(self, {"apps": running_apps() if IS_MAC else []})
        if p == "/favicon.ico":
            self.send_response(204); self.end_headers(); return
        if p == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/api/session":
            d = read_json(self)
            with LOCK:
                SESSION.update(ts=time.time(), data=d)
            return send_json(self, {"ok": True})
        if p == "/api/enforce":
            d = read_json(self)
            ok = enforce(str(d.get("action", "")), d.get("app"), d.get("browser"))
            with LOCK:
                _mac_cache["t"] = 0  # сразу перечитать активное приложение
            return send_json(self, {"ok": ok})
        if p.startswith("/api/phone/"):
            return self.phone_api("POST")
        return send_json(self, {"error": "not_found"}, 404)


class PhoneHandler(Base):
    """Доступно из Wi-Fi, но отдаёт только страницу телефона и API телефона с кодом."""
    timeout = 15  # «молчащие» соединения закрываются сами

    def setup(self):
        self.request.settimeout(self.timeout)
        try:
            self.request.do_handshake()
        except Exception:
            pass  # телефон отклонил сертификат или соединение пустое — дальше просто закроется
        super().setup()

    def handle(self):
        try:
            super().handle()
        except (ssl.SSLError, OSError):
            pass

    def do_GET(self):
        p = urlparse(self.path).path
        if p in ("/phone", "/phone.html"):
            self.path = "/phone.html"
            return super().do_GET()
        return send_json(self, {"error": "forbidden"}, 403)

    def do_POST(self):
        if urlparse(self.path).path.startswith("/api/phone/"):
            return self.phone_api("POST")
        return send_json(self, {"error": "forbidden"}, 403)


def main():
    local = ThreadingHTTPServer(("127.0.0.1", PORT), LocalHandler)
    print(f"Lock In: http://localhost:{PORT}")
    ips = all_ips()
    cert, key, _ = ensure_cert(ips)
    if cert:
        try:
            phone = ThreadingHTTPServer(("0.0.0.0", PHONE_PORT), PhoneHandler)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
            CERT_STATE.update(ips=ips, ctx=ctx)
            # рукопожатие TLS — в потоке запроса, а не в общем цикле: иначе одно «повисшее» соединение
            # (браузер телефона часто открывает лишнее заранее) блокирует связь с телефоном целиком
            phone.socket = ctx.wrap_socket(phone.socket, server_side=True, do_handshake_on_connect=False)
            PHONE_URL["url"] = f"https://{ips[0]}:{PHONE_PORT}/phone?t={TOKEN}"
            threading.Thread(target=phone.serve_forever, daemon=True).start()
            print(f"Телефон (та же сеть Wi-Fi): {PHONE_URL['url']}")
        except OSError as e:
            print("Не удалось открыть порт для телефона:", e)
    print("Ctrl+C — остановить")
    try:
        local.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
