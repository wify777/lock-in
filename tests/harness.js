// Автотесты Lock In. Открой http://localhost:5173/?test и в консоли: await import('/tests/harness.js')
// В режиме ?test камера и нейросети не нужны: «позу» и «телефон» задаёт тест.
const D = window.__dbg;
const T = window.T = { enforced: [], fakeApp: null, tok: null, res: [], asked: 0, bg: true };
const of = window.fetch.bind(window);
window.fetch = async (u, o) => {
  const url = String(u);
  if (url.includes('/api/enforce')) { T.enforced.push(JSON.parse(o.body)); return new Response('{"ok":true}'); }
  const r = await of(u, o);
  if (url.includes('/api/state') && T.fakeApp) { const j = await r.json(); Object.assign(j, T.fakeApp); return new Response(JSON.stringify(j)); }
  return r;
};
T.sleep = ms => new Promise(r => setTimeout(r, ms));
T.pose = (o = {}) => { D.SIM.M = { yaw: 0, pitch: 0, eyeDown: .1, blink: .1, jaw: 0, w: .3, ny: .5, roll: 0, fb: { x0: .4, y0: .3, x1: .6, y1: .6 }, ...o }; };
T.gone = () => { D.SIM.M = null; };
T.said = () => D.SIM.said.splice(0);
T.ping = async d => {
  if (!T.tok) T.tok = (await (await of('/api/pair')).json()).token;
  return (await of('/api/phone/ping?t=' + T.tok, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ visible: true, events: [], ...d }) })).json();
};
T.phone = null;   // null — телефон не подключён; {visible, moving, tilt} — шлёт пинг каждые 0,5 с
(async () => { while (T.bg) { if (T.phone && T.phone.visible) { try { const r = await T.ping({ ...T.phone }); if (r.ask_reason) T.asked++; } catch {} } await T.sleep(500); } })();
T.card = () => document.querySelector('#phSt span').textContent;
T.st = () => document.querySelector('#schipT').textContent;
T.q = s => document.querySelector(s);
T.check = (name, cond, info) => { T.res.push((cond ? '✓ ' : '✗ ') + name + (cond ? '' : '  → ' + JSON.stringify(info))); };
T.out = () => T.res.splice(0);
T.boot = async (extra = {}) => {
  Object.assign(D.settings, { task: 'Тест: решить 5 задач', firstStep: '', learnSkipped: true, lock: false, strict: false, screen: false, ai: false, apiKey: '',
    dur: 50, breakEvery: 0, sens: 1, care: true, paper: false, voice: true, coach: 'sensei', lang: 'ru', ...extra });
  T.pose(); D.SIM.noise = 1.5; await D.startSession(); await T.sleep(3500); T.said();
};
T.cleanup = () => {
  T.bg = false; if (D.S) D.finish('manual'); T.q('#summary').classList.remove('on');
  for (const k of Object.keys(localStorage)) if (/^(lockin|sensei)\./.test(k)) localStorage.removeItem(k);
};
export default T;
