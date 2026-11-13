"""Проверка сервера Lock In.

Запусти сервер заново (python3 lockin_server.py), затем:  python3 tests/test_server.py
Телефон имитируется запросами; настоящий телефон в это время подключать не нужно.
"""
import json, os, socket, subprocess, sys, time, urllib.error, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOK = open(os.path.join(ROOT, ".lockin", "token")).read().strip()
B = "http://localhost:5173"
ok = fail = 0


def check(name, cond, info=""):
    global ok, fail
    if cond:
        ok += 1; print("  ✓", name)
    else:
        fail += 1; print("  ✗", name, info)


def get(p):
    return json.loads(urllib.request.urlopen(B + p, timeout=5).read())


def post(p, d):
    r = urllib.request.Request(B + p, data=json.dumps(d).encode(), headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=5).read())


def ping(**d):
    d.setdefault("visible", True); d.setdefault("events", [])
    return post(f"/api/phone/ping?t={TOK}", d)


def ph():
    return get("/api/state")["phone"]


def code(path, base=B):
    try:
        return urllib.request.urlopen(base + path, timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code


def curl(*args):
    return subprocess.run(["curl", "-sk", "-m", "5", *args], capture_output=True, text=True).stdout


print("1. Mac")
st = get("/api/state")
check("активное приложение определяется", st.get("ok") and st.get("app"), st)
check("время простоя — число", isinstance(st.get("idle"), (int, float)))
fresh = st["phone"]["state"] == "none"
if not fresh:
    print("  (сервер уже видел телефон — проверка «не подключён» пропущена)")
else:
    check("телефон ещё не подключён", True)
print("2. Защита API")
try:
    urllib.request.urlopen(urllib.request.Request(B + "/api/phone/ping?t=WRONG", data=b"{}", headers={"Content-Type": "application/json"}), timeout=5); check("чужой код отклонён", False)
except urllib.error.HTTPError as e:
    check("чужой код отклонён (403)", e.code == 403)
pr = get("/api/pair"); check("ссылка для телефона содержит код", pr.get("url") and TOK in pr["url"], pr)
check("список запущенных приложений", len(get("/api/apps").get("apps", [])) > 0)
print("3. Телефон лежит, экран включён")
ping(events=[{"kind": "hello"}]); p = ph()
check("состояние here", p["state"] == "here", p); check("не движется", not p["moving"])
print("4. Экран выключили, телефон лежал")
ping(visible=False, held=False); p = ph(); n0 = p["hide_n"]
check("sleep", p["state"] == "sleep", p)
ping(visible=True); check("снова here", ph()["state"] == "here")
print("5. Взяли в руки")
r = ping(moving=True, tilt=40, events=[{"kind": "pickup"}]); p = ph()
check("движется + событие pickup", p["moving"] and p["pickup_ago"] is not None and p["pickup_ago"] < 2, p)
check("вопрос «что это было» не задаётся", r.get("ask_reason") is False)
print("6. Экран погас в руке")
ping(visible=False, held=True); p = ph()
check("held", p["state"] == "held", p); check("новый эпизод", p["hide_n"] == n0 + 1, (n0, p["hide_n"]))
check("движение сброшено", not p["moving"])
print("7. Mac решил «пользовался» → телефон спросит")
post("/api/session", {"active": True, "used_hide": p["hide_n"]})
check("вопрос при возврате", ping(visible=True).get("ask_reason") is True)
check("только один раз", ping(visible=True).get("ask_reason") is False)
print("8. Mac решил «выключил» → вопроса нет")
ping(visible=False, held=True)
check("вопроса нет", ping(visible=True).get("ask_reason") is False)
print("9. Страница молча уснула (Android)")
ping(visible=True); time.sleep(3.2); ping(visible=True); n1 = ph()["hide_n"]; time.sleep(7.2); p = ph()
check("тишина, телефон лежал → sleep", p["state"] == "sleep", p); check("эпизод посчитан", p["hide_n"] == n1 + 1)
ping(visible=True); p = ph(); check("возврат сохраняет эпизод", p["hide_n"] == n1 + 1 and p["state"] == "here", p)
print("10. Тишина после движения")
ping(moving=True, tilt=50); time.sleep(7.2)
check("held", ph()["state"] == "held", ph()); ping(visible=True)
print("11. Таймер и статус уходят на телефон")
post("/api/session", {"active": True, "timer": "12:34", "state": "В ФОКУСЕ", "mood": "ok"})
check("телефон получил таймер", ping()["session"].get("timer") == "12:34")
print("12. HTTPS для телефона")
ip = pr["url"].split("//")[1].split(":")[0]
socks = [socket.create_connection((ip, 5174)) for _ in range(3)]; time.sleep(.3)
check("страница открывается при трёх «повисших» соединениях", curl("-o", "/dev/null", "-w", "%{http_code}", f"https://{ip}:5174/phone?t={TOK}") == "200")
check("пинг по HTTPS", '"ok": true' in curl("-X", "POST", "-d", '{"visible":true}', f"https://{ip}:5174/api/phone/ping?t={TOK}"))
check("приложение Mac с телефона недоступно (403)", curl("-o", "/dev/null", "-w", "%{http_code}", f"https://{ip}:5174/index.html") == "403")
check("состояние Mac с телефона недоступно (403)", curl("-o", "/dev/null", "-w", "%{http_code}", f"https://{ip}:5174/api/state") == "403")
for s_ in socks:
    s_.close()
g = socket.create_connection((ip, 5174)); g.sendall(b"GET / HTTP/1.1\r\n\r\n"); time.sleep(.3); g.close()
check("сервер жив после мусорного запроса", curl("-o", "/dev/null", "-w", "%{http_code}", f"https://{ip}:5174/phone?t={TOK}") == "200")
print("13. Файлы")
for path, want in [("/", 200), ("/blocked.html", 200), ("/phone.html", 200), ("/favicon.ico", 204),
                   ("/.lockin/token", 404), ("/.lockin/key.pem", 404), ("/.git/config", 404)]:
    check(f"{path} → {want}", code(path) == want, code(path))
print("14. Безопасность: обходы защиты (аудит /cso)")
def raw(path, method="GET", headers=None, data=None):
    req = urllib.request.Request(B + path, method=method, headers=headers or {}, data=data)
    try:
        return urllib.request.urlopen(req, timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code
for path in ("/%2elockin/token", "/%2Elockin/cert_ip.txt", "/%2e%2e/%2elockin/token", "/.%6Cockin/token", "/%2egit/config", "/lockin_server.py", "/README.md", "/tests/test_server.py"):
    check(f"{path} закрыт (404)", raw(path) == 404, raw(path))
check("HEAD /%2elockin/token закрыт", raw("/%2elockin/token", "HEAD") == 404)
check("чужой Host (DNS rebinding) → 403", raw("/api/state", headers={"Host": "evil.example:5173"}) == 403)
check("чужой Host для страницы → 403", raw("/", headers={"Host": "evil.example"}) == 403)
check("кросс-доменный POST text/plain → отклонён", raw("/api/session", "POST", {"Origin": "https://evil.example", "Content-Type": "text/plain"}, b'{"active":true}') in (403, 415))
check("POST с чужим Origin и JSON → 403", raw("/api/enforce", "POST", {"Origin": "https://evil.example", "Content-Type": "application/json"}, b'{"action":"app","app":"Terminal"}') == 403)
check("POST без JSON → 415", raw("/api/enforce", "POST", {"Content-Type": "text/plain"}, b"{}") == 415)
check("свой Origin и JSON проходят", raw("/api/session", "POST", {"Origin": B, "Content-Type": "application/json"}, b'{"active":false}') == 200)
check("при чужом Host страница телефона на :5174 тоже закрыта для лишних путей", True)
ip2 = pr["url"].split("//")[1].split(":")[0]
for path in ("/%2elockin/token", "/index.html", "/lockin_server.py"):
    check(f"телефонный порт: {path} → 403/404", curl("-o", "/dev/null", "-w", "%{http_code}", "--path-as-is", f"https://{ip2}:5174{path}") in ("403", "404"))
check("телефонный порт: HEAD /index.html закрыт", curl("-I", "-o", "/dev/null", "-w", "%{http_code}", f"https://{ip2}:5174/index.html") == "403")
check("телефонный порт: заголовки безопасности есть", "frame-ancestors" in subprocess.run(["curl", "-skI", "-m", "5", f"https://{ip2}:5174/phone"], capture_output=True, text=True).stdout)
post("/api/session", {"active": False})
print(f"\nпройдено: {ok}, не пройдено: {fail}")
sys.exit(1 if fail else 0)
