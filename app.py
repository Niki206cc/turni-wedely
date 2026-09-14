import json
import logging
import os
import re
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, flash, redirect, render_template, request, url_for
from playwright.sync_api import sync_playwright

APP_VERSION = "1.0.4"
TZ = ZoneInfo("Europe/Rome")
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = DATA_DIR / "config.json"
STATE_FILE = DATA_DIR / "state.json"
DEBUG_FILE = DATA_DIR / "debug-last.png"
LOGIN_URL = "https://www.wedely.com/wemanager_login"
CENTERS = [
    "WeManagers - Luxembourg Center",
    "WeManagers - Luxembourg South",
]
DEFAULT_CONFIG = {
    "username": "",
    "password": "",
    "employee_name": "Nicola Trussardi",
    "waha_url": "",
    "waha_api_key": "",
    "waha_session": "default",
    "waha_number": "",
}
DEFAULT_STATE = {
    "status": "Mai eseguito",
    "last_check": None,
    "last_whatsapp": None,
    "last_error": None,
    "preview": "",
    "login_ok": None,
    "waha_ok": None,
}

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", os.urandom(24))
lock = threading.Lock()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("wedely")


def load_json(path, default):
    try:
        return {**default, **json.loads(path.read_text(encoding="utf-8"))}
    except (FileNotFoundError, json.JSONDecodeError):
        return default.copy()


def save_json(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def config():
    return load_json(CONFIG_FILE, DEFAULT_CONFIG)


def state():
    return load_json(STATE_FILE, DEFAULT_STATE)


def update_state(**values):
    current = state()
    current.update(values)
    save_json(STATE_FILE, current)


def masked_number(number):
    digits = re.sub(r"\D", "", number or "")
    return ("*" * max(0, len(digits) - 4) + digits[-4:]) if digits else "Non configurato"


def click_text(page, text, exact=False):
    candidates = [
        page.get_by_text(text, exact=exact),
        page.get_by_role("link", name=re.compile(re.escape(text), re.I)),
        page.get_by_role("button", name=re.compile(re.escape(text), re.I)),
    ]
    for locator in candidates:
        try:
            if locator.count():
                locator.first.click(timeout=5000)
                return True
        except Exception:
            pass
    return False


def login(page, cfg):
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    if "login" not in page.url.lower() and "wemanager" in page.url.lower():
        return
    user = page.locator('input[type="email"], input[name*="user" i], input[name*="email" i], input[type="text"]').first
    password = page.locator('input[type="password"]').first
    user.fill(cfg["username"])
    password.fill(cfg["password"])
    submit = page.locator('button[type="submit"], input[type="submit"]').first
    if submit.count():
        submit.click()
    else:
        password.press("Enter")
    page.wait_for_load_state("domcontentloaded", timeout=60000)
    page.wait_for_timeout(2000)
    if "login" in page.url.lower() or page.locator('input[type="password"]').count():
        raise RuntimeError("Login Wedely non riuscito: controlla username e password")


def has_shift_controls(scope):
    try:
        text = scope.locator("body").inner_text(timeout=3000)
        if any(center.lower() in text.lower() for center in CENTERS):
            return True
        for select in scope.locator("select").all():
            options = " ".join(select.locator("option").all_text_contents()).lower()
            if "luxembourg center" in options or "luxembourg south" in options:
                return True
    except Exception:
        pass
    return False


def open_shifts(page):
    # La pagina può essere già aperta dopo il login.
    scopes = [page] + list(page.frames)
    for scope in scopes:
        if has_shift_controls(scope):
            return scope

    # Percorso principale: menu laterale WeDrivers -> WeDrivers Shifts.
    for scope in scopes:
        parent_candidates = [
            scope.get_by_text(re.compile(r"^\\s*WeDrivers\\s*$", re.I)),
            scope.get_by_role("button", name=re.compile(r"^\\s*WeDrivers\\s*$", re.I)),
            scope.get_by_role("link", name=re.compile(r"^\\s*WeDrivers\\s*$", re.I)),
            scope.locator('aside a, aside button, nav a, nav button').filter(
                has_text=re.compile(r"^\\s*WeDrivers\\s*$", re.I)
            ),
        ]
        opened = False
        for locator in parent_candidates:
            try:
                for i in range(locator.count()):
                    item = locator.nth(i)
                    if item.is_visible():
                        item.click(timeout=5000)
                        page.wait_for_timeout(800)
                        opened = True
                        break
                if opened:
                    break
            except Exception:
                continue
        if not opened:
            continue

        submenu_rx = re.compile(r"^\\s*WeDrivers?\\s+Shifts?\\s*$", re.I)
        submenu_candidates = [
            scope.get_by_text(submenu_rx),
            scope.get_by_role("link", name=submenu_rx),
            scope.locator('aside a, nav a, .sidebar a, .submenu a').filter(has_text=submenu_rx),
        ]
        for locator in submenu_candidates:
            try:
                for i in range(locator.count()):
                    item = locator.nth(i)
                    if item.is_visible():
                        item.click(timeout=5000)
                        page.wait_for_timeout(1300)
                        for candidate_scope in [page] + list(page.frames):
                            if has_shift_controls(candidate_scope):
                                return candidate_scope
            except Exception:
                continue

    # Cerca sia per testo sia nell'href; gestisce singolare/plurale e spazi.
    shift_rx = re.compile(r"We\s*Drivers?\s*Shifts?|Drivers?\s*Shifts?|Shifts?", re.I)
    for scope in [page] + list(page.frames):
        candidates = [
            scope.get_by_text(shift_rx),
            scope.get_by_role("link", name=shift_rx),
            scope.locator('a[href*="shift" i], a[href*="driver" i]'),
            scope.locator('[data-url*="shift" i], [onclick*="shift" i]'),
        ]
        for locator in candidates:
            try:
                for i in range(min(locator.count(), 20)):
                    element = locator.nth(i)
                    if not element.is_visible():
                        continue
                    element.click(timeout=5000)
                    page.wait_for_timeout(1300)
                    for candidate_scope in [page] + list(page.frames):
                        if has_shift_controls(candidate_scope):
                            return candidate_scope
            except Exception:
                continue

    # Ultimo tentativo: apre direttamente un link "shift" trovato nel DOM.
    for scope in [page] + list(page.frames):
        try:
            hrefs = scope.locator('a[href*="shift" i]').evaluate_all(
                "els => els.map(e => e.href).filter(Boolean)"
            )
            for href in hrefs:
                page.goto(href, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1000)
                if has_shift_controls(page):
                    return page
        except Exception:
            pass

    try:
        visible = page.locator("a:visible, button:visible").all_text_contents()
        diagnostic = " | ".join(x.strip() for x in visible if x.strip())[:600]
    except Exception:
        diagnostic = "nessuna voce leggibile"
    raise RuntimeError(
        "Pagina 'WeDrivers Shifts' non trovata. "
        f"Voci visibili: {diagnostic}. Screenshot: /app/data/debug-last.png"
    )


def normalize_label(value):
    value = (value or "").casefold()
    value = re.sub(r"[–—−-]+", " ", value)
    return re.sub(r"\\s+", " ", value).strip()


def wanted_aliases(wanted):
    normalized = normalize_label(wanted)
    aliases = [normalized]
    if "luxembourg center" in normalized:
        aliases += ["luxembourg center", "wemanagers luxembourg center"]
    elif "luxembourg south" in normalized:
        aliases += ["luxembourg south", "wemanagers luxembourg south"]
    return aliases


def label_matches(label, wanted):
    normalized = normalize_label(label)
    return any(alias in normalized or normalized in alias for alias in wanted_aliases(wanted))


def choose_value(page, wanted):
    # Menu HTML nativo, compresi quelli nascosti da Bootstrap/Select2.
    for select in page.locator("select").all():
        try:
            options = select.locator("option")
            for i in range(options.count()):
                option = options.nth(i)
                label = option.inner_text().strip()
                if label_matches(label, wanted):
                    value = option.get_attribute("value")
                    if value is not None:
                        select.select_option(value=value, force=True)
                    else:
                        select.select_option(index=i, force=True)
                    select.evaluate("""el => {
                        el.dispatchEvent(new Event('input', {bubbles:true}));
                        el.dispatchEvent(new Event('change', {bubbles:true}));
                    }""")
                    page.wait_for_timeout(700)
                    return True
        except Exception:
            pass

    # Menu grafici usati da Bootstrap Select, Select2, Chosen e componenti simili.
    controls = page.locator(
        '.bootstrap-select button, button.dropdown-toggle, '
        '.select2-selection, .chosen-single, [role="combobox"], '
        '[aria-haspopup="listbox"], [aria-haspopup="true"]'
    )
    for i in range(controls.count()):
        try:
            control = controls.nth(i)
            if not control.is_visible():
                continue
            control.click(timeout=4000)
            page.wait_for_timeout(400)
            visible_options = page.locator(
                '[role="option"]:visible, .dropdown-menu li:visible, '
                '.select2-results__option:visible, .chosen-results li:visible, '
                'a.dropdown-item:visible'
            )
            for j in range(visible_options.count()):
                option = visible_options.nth(j)
                if label_matches(option.inner_text(), wanted):
                    option.click(timeout=5000)
                    page.wait_for_timeout(700)
                    return True
            # Alcuni plugin non assegnano una classe alle righe del menu.
            text_option = page.get_by_text(
                re.compile(re.escape("Luxembourg Center" if "Center" in wanted else
                                     "Luxembourg South" if "South" in wanted else wanted), re.I)
            )
            for j in range(text_option.count()):
                option = text_option.nth(j)
                if option.is_visible() and label_matches(option.inner_text(), wanted):
                    option.click(timeout=5000)
                    page.wait_for_timeout(700)
                    return True
            # Richiude il controllo se non era quello corretto.
            try:
                control.press("Escape")
            except Exception:
                pass
        except Exception:
            continue

    # Ultimo tentativo su qualunque testo visibile corrispondente.
    short = ("Luxembourg Center" if "Center" in wanted else
             "Luxembourg South" if "South" in wanted else wanted)
    candidates = page.get_by_text(re.compile(re.escape(short), re.I))
    for i in range(candidates.count()):
        try:
            option = candidates.nth(i)
            if option.is_visible() and label_matches(option.inner_text(), wanted):
                option.click(timeout=5000)
                page.wait_for_timeout(500)
                return True
        except Exception:
            pass
    return False


def navigate_week(page, target):
    # Cerca l'intervallo settimana visualizzato e usa le frecce finché contiene target.
    for _ in range(12):
        body = page.locator("body").inner_text()
        dates = re.findall(r"\b(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?\b", body)
        visible = []
        for d, m, y in dates:
            try:
                year = int(y) if y else target.year
                if year < 100:
                    year += 2000
                visible.append(date(year, int(m), int(d)))
            except ValueError:
                pass
        if any(abs((x - target).days) <= 6 for x in visible) or not visible:
            return
        direction = "next" if max(visible) < target else "prev"
        selectors = (
            'button[aria-label*="next" i], .fc-next-button, [title*="next" i]'
            if direction == "next"
            else 'button[aria-label*="prev" i], .fc-prev-button, [title*="prev" i]'
        )
        button = page.locator(selectors).first
        if not button.count():
            return
        button.click()
        page.wait_for_timeout(700)


def extract_employee_shifts(page, employee, start_day, end_day):
    raw = page.locator("body").inner_text()
    found = []
    # Estrazione DOM: per ogni occorrenza del nome risale al riquadro del turno.
    nodes = page.get_by_text(re.compile(re.escape(employee), re.I))
    for i in range(nodes.count()):
        node = nodes.nth(i)
        try:
            data = node.evaluate("""el => {
              let cur = el;
              for (let i=0; i<7 && cur; i++, cur=cur.parentElement) {
                const t = (cur.innerText || '').trim();
                const times = t.match(/\\b(?:[01]?\\d|2[0-3]):[0-5]\\d\\b/g) || [];
                if (times.length >= 2 && t.length < 1200) {
                  let p = cur;
                  let context = t;
                  for (let j=0; j<4 && p; j++, p=p.parentElement)
                    context += '\\n' + (p.innerText || '').slice(0, 500);
                  return {text:t, context};
                }
              }
              return null;
            }""")
            if not data:
                continue
            times = re.findall(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b", data["text"])
            context = data["context"]
            shift_day = None
            for day in (start_day + timedelta(days=n) for n in range((end_day-start_day).days+1)):
                patterns = [
                    rf"\b{day.day}[./-]0?{day.month}\b",
                    rf"\b0?{day.day}\s+{day.strftime('%B')}\b",
                ]
                if any(re.search(p, context, re.I) for p in patterns):
                    shift_day = day
                    break
            if shift_day and len(times) >= 2:
                found.append((shift_day, times[0], times[1]))
        except Exception:
            continue

    # Fallback testuale per layout a colonne: data più recente prima del nome.
    if not found:
        lines = [x.strip() for x in raw.splitlines() if x.strip()]
        current_day = None
        for idx, line in enumerate(lines):
            for day in (start_day + timedelta(days=n) for n in range((end_day-start_day).days+1)):
                if re.search(rf"\b0?{day.day}[./-]0?{day.month}\b", line):
                    current_day = day
            if employee.lower() in line.lower() and current_day:
                nearby = " ".join(lines[max(0, idx-5):idx+3])
                times = re.findall(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b", nearby)
                if len(times) >= 2:
                    found.append((current_day, times[-2], times[-1]))

    return sorted(set(found))


def safe_screenshot(page):
    try:
        page.screenshot(
            path=str(DEBUG_FILE),
            full_page=False,
            timeout=5000,
            animations="disabled",
        )
    except Exception as exc:
        log.warning("Screenshot diagnostico ignorato: %s", exc)


def scrape(start_day, end_day, login_only=False):
    cfg = config()
    if not cfg["username"] or not cfg["password"]:
        raise RuntimeError("Inserisci username e password Wedely")
    result = {center: [] for center in CENTERS}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=os.getenv("HEADLESS", "true").lower() == "true")
        context = browser.new_context(locale="it-IT", timezone_id="Europe/Rome", viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        try:
            login(page, cfg)
            if login_only:
                return result
            shifts_scope = open_shifts(page)
            for center in CENTERS:
                if not choose_value(shifts_scope, center):
                    raise RuntimeError(f"Sede non trovata: {center}")
                page.wait_for_timeout(600)
                if not choose_value(shifts_scope, cfg["employee_name"]):
                    # Alcuni campi richiedono digitazione.
                    search = shifts_scope.locator('input[role="combobox"], input[type="search"]').last
                    if search.count():
                        search.fill(cfg["employee_name"])
                        page.wait_for_timeout(600)
                        click_text(shifts_scope, cfg["employee_name"], exact=True)
                click_text(shifts_scope, "Open", exact=True)
                page.wait_for_timeout(1200)
                navigate_week(page, start_day)
                result[center] = extract_employee_shifts(shifts_scope, cfg["employee_name"], start_day, end_day)
                # Ritorna alla scelta senza dipendere da una URL specifica.
                if not click_text(shifts_scope, "Back"):
                    shifts_scope = open_shifts(page)
                else:
                    page.wait_for_timeout(700)
                    shifts_scope = open_shifts(page)
            safe_screenshot(page)
            return result
        except Exception:
            safe_screenshot(page)
            raise
        finally:
            browser.close()


def format_message(start_day, end_day, shifts):
    weekdays = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]
    if start_day == end_day:
        lines = [f"📅 Turni di domani - {weekdays[start_day.weekday()]} {start_day.strftime('%d/%m')}", ""]
    else:
        lines = [f"📅 Turni della settimana - {start_day.strftime('%d/%m')} → {end_day.strftime('%d/%m')}", ""]
    for n in range((end_day-start_day).days+1):
        day = start_day + timedelta(days=n)
        if start_day != end_day:
            lines += [f"*{weekdays[day.weekday()]} {day.strftime('%d/%m')}*"]
        any_shift = False
        for center in CENTERS:
            items = [(a,b) for d,a,b in shifts.get(center, []) if d == day]
            if items:
                any_shift = True
                short = "Luxembourg Center" if "Center" in center else "Luxembourg South"
                lines.append(f"📍 {short}")
                lines.extend(f"{a} - {b}" for a,b in items)
        if not any_shift:
            lines.append("Nessun turno")
        lines.append("")
    return "\n".join(lines).strip()


def send_waha(message):
    cfg = config()
    if not cfg["waha_url"] or not cfg["waha_number"]:
        raise RuntimeError("Configurazione WAHA incompleta")
    number = re.sub(r"\D", "", cfg["waha_number"]) + "@c.us"
    headers = {"Content-Type": "application/json"}
    if cfg["waha_api_key"]:
        headers["X-Api-Key"] = cfg["waha_api_key"]
    response = requests.post(
        cfg["waha_url"].rstrip("/") + "/api/sendText",
        headers=headers,
        json={"session": cfg["waha_session"] or "default", "chatId": number, "text": message},
        timeout=30,
    )
    response.raise_for_status()
    update_state(last_whatsapp=datetime.now(TZ).isoformat(), waha_ok=True)


def target_range(now=None):
    today = (now or datetime.now(TZ)).date()
    if today.weekday() == 6:
        start = today + timedelta(days=1)
        return start, start + timedelta(days=6)
    tomorrow = today + timedelta(days=1)
    return tomorrow, tomorrow


def run_check(send=True):
    if not lock.acquire(blocking=False):
        raise RuntimeError("Un controllo è già in corso")
    try:
        update_state(status="Controllo in corso", last_error=None)
        start_day, end_day = target_range()
        shifts = scrape(start_day, end_day)
        message = format_message(start_day, end_day, shifts)
        update_state(status="Completato", last_check=datetime.now(TZ).isoformat(), preview=message, login_ok=True)
        if send:
            send_waha(message)
        return message
    except Exception as exc:
        log.exception("Controllo fallito")
        update_state(status="Errore", last_check=datetime.now(TZ).isoformat(), last_error=str(exc))
        raise
    finally:
        lock.release()


def scheduled_check():
    try:
        run_check(send=True)
    except Exception as exc:
        # Prova ad avvisare anche quando Wedely fallisce.
        try:
            send_waha(f"⚠️ Wedely: impossibile recuperare i turni.\n{exc}")
        except Exception:
            pass


@app.route("/")
def index():
    current = state()
    cfg = config()
    return render_template("index.html", cfg=cfg, state=current, version=APP_VERSION, masked=masked_number(cfg["waha_number"]))


@app.post("/save")
def save():
    old = config()
    values = {key: request.form.get(key, "").strip() for key in DEFAULT_CONFIG}
    # I campi segreti vuoti mantengono il valore già salvato.
    for secret in ("password", "waha_api_key"):
        if not values[secret]:
            values[secret] = old[secret]
    save_json(CONFIG_FILE, values)
    flash("Configurazione salvata.", "success")
    return redirect(url_for("index"))


@app.post("/test-login")
def test_login():
    try:
        today = datetime.now(TZ).date()
        scrape(today, today, login_only=True)
        update_state(login_ok=True, last_error=None)
        flash("Login Wedely riuscito.", "success")
    except Exception as exc:
        update_state(login_ok=False, last_error=str(exc))
        flash(str(exc), "error")
    return redirect(url_for("index"))


@app.post("/test-whatsapp")
def test_whatsapp():
    try:
        send_waha("✅ Test WAHA riuscito: Turni Wedely è collegato.")
        flash("Messaggio WhatsApp inviato.", "success")
    except Exception as exc:
        update_state(waha_ok=False, last_error=str(exc))
        flash(f"Errore WhatsApp: {exc}", "error")
    return redirect(url_for("index"))


@app.post("/check")
def check_now():
    try:
        message = run_check(send=False)
        flash("Turni recuperati. Controlla l'anteprima.", "success")
    except Exception as exc:
        flash(str(exc), "error")
    return redirect(url_for("index"))


@app.post("/send-preview")
def send_preview():
    try:
        preview = state().get("preview")
        if not preview:
            raise RuntimeError("Prima esegui 'Controlla ora'")
        send_waha(preview)
        flash("Anteprima inviata su WhatsApp.", "success")
    except Exception as exc:
        flash(str(exc), "error")
    return redirect(url_for("index"))


scheduler = BackgroundScheduler(timezone=TZ)
scheduler.add_job(scheduled_check, "cron", hour=21, minute=0, id="daily-check", replace_existing=True)
scheduler.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8091")), debug=False)
