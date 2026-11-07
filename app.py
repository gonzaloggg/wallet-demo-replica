from flask import Flask, request, jsonify, send_file, render_template_string, redirect, url_for, Response
from werkzeug.middleware.proxy_fix import ProxyFix
from functools import wraps
from datetime import datetime, timezone, date, timedelta
from email.utils import formatdate, parsedate_to_datetime
from PIL import Image, ImageDraw, ImageFont
import os, io, csv, re, json, base64, hashlib, zipfile, subprocess, uuid, httpx, ssl, sqlite3, tempfile, shutil, math, time, threading
import qrcode
from py_vapid import Vapid01, Vapid02
from pywebpush import webpush, WebPushException
from cryptography.hazmat.primitives import serialization

app = Flask(__name__)
# Respetar los encabezados del túnel / proxy HTTPS (X-Forwarded-Proto / Host)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)


BARISTA_COOKIE_NAME = "maishas_barista_auth"
_login_attempts = {}  # ip -> [timestamps]
LOGIN_RATE_LIMIT_MAX = 8
LOGIN_RATE_LIMIT_WINDOW = 60  # segundos


def _client_ip():
    xff = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return xff or request.remote_addr or "unknown"


def _is_login_rate_limited(ip):
    now = datetime.now(timezone.utc).timestamp()
    recent = [t for t in _login_attempts.get(ip, []) if now - t < LOGIN_RATE_LIMIT_WINDOW]
    _login_attempts[ip] = recent
    return len(recent) >= LOGIN_RATE_LIMIT_MAX


def _record_login_failure(ip):
    now = datetime.now(timezone.utc).timestamp()
    recent = [t for t in _login_attempts.get(ip, []) if now - t < LOGIN_RATE_LIMIT_WINDOW]
    recent.append(now)
    _login_attempts[ip] = recent


def _clear_login_failures(ip):
    _login_attempts.pop(ip, None)


def _normalize_phone_digits(phone_str):
    """Extrae los últimos 10 dígitos del número de celular para evitar duplicados (ej. +52 642 123 4567 -> 6421234567)."""
    digits = "".join(ch for ch in (phone_str or "") if ch.isdigit())
    if len(digits) >= 10:
        return digits[-10:]
    return digits


def _barista_expected_token():
    pw = os.environ.get("BARISTA_PASSWORD", "cafe2026")
    return hashlib.sha256(f"maishas-barista-session:{pw}".encode("utf-8")).hexdigest()


def check_auth(username, password):
    u = (username or "").strip().lower()
    p = (password or "").strip()
    return u == "maishas" and p == os.environ.get("BARISTA_PASSWORD", "cafe2026")


def is_barista_authenticated():
    cookie_val = request.cookies.get(BARISTA_COOKIE_NAME, "")
    if cookie_val and cookie_val == _barista_expected_token():
        return True
    auth = request.authorization
    if auth and check_auth(auth.username, auth.password):
        return True
    return False


BARISTA_LOGIN_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <title>Demo Café — Barista POS</title>
    <meta name="theme-color" content="#F4ECE6">
    <meta name="mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="default">
    <meta name="apple-mobile-web-app-title" content="Barista POS">
    <link rel="manifest" href="/barista/manifest.webmanifest" crossorigin="use-credentials">
    <link rel="apple-touch-icon" href="/barista/icon-512.png">
    <link rel="icon" type="image/png" href="/card/icon-192.png">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Calligraffitti&family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {
            color-scheme: light;
            --bg-canvas: #F4ECE6;
            --gold: #4A5D23;
            --gold-light: #4E301B;
            --text-primary: #121212;
            --text-secondary: #6E655F;
            --border-subtle: rgba(18, 18, 18, 0.08);
        }
        * { box-sizing: border-box; -webkit-font-smoothing: antialiased; }
        html, body {
            overflow-x: hidden;
            max-width: 100vw;
            touch-action: manipulation;
            -webkit-text-size-adjust: 100%;
        }
        body {
            margin: 0;
            min-height: 100vh;
            font-family: "Plus Jakarta Sans", -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", sans-serif;
            background-color: var(--bg-canvas);
            background-image:
                radial-gradient(circle at 50% 0%, rgba(234, 223, 215, 0.85) 0%, transparent 55%),
                radial-gradient(circle at 85% 90%, rgba(232, 108, 120, 0.06) 0%, transparent 45%);
            color: var(--text-primary);
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 24px 16px;
        }
        .login-card {
            width: 100%;
            max-width: 380px;
            background: #EADFD7;
            border: 1px solid var(--border-subtle);
            border-radius: 24px;
            padding: 30px 24px 26px;
            box-shadow: 0 12px 34px rgba(78, 48, 27, 0.08);
            text-align: center;
        }
        .login-emblem {
            width: 58px;
            height: 58px;
            border-radius: 50%;
            background: #121212;
            border: 2.5px solid #4A5D23;
            color: #FAF6F3;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 26px;
            margin: 0 auto 14px;
            box-shadow: 0 6px 16px rgba(18, 18, 18, 0.14);
        }
        .login-title {
            margin: 0 0 6px;
            font-size: 22px;
            font-weight: 800;
            letter-spacing: -0.02em;
            color: var(--text-primary);
        }
        .login-sub {
            margin: 0 0 22px;
            font-size: 13.5px;
            color: var(--text-secondary);
        }
        .login-field {
            width: 100%;
            background: #FAF6F3;
            border: 1.5px solid rgba(18, 18, 18, 0.10);
            border-radius: 13px;
            padding: 13px 14px;
            color: var(--text-primary);
            font-size: 16px;
            font-family: inherit;
            margin-bottom: 12px;
            outline: none;
        }
        .login-field:focus {
            border-color: #121212;
            background: #FFFFFF;
            box-shadow: 0 0 0 3px rgba(18, 18, 18, 0.08);
        }
        .login-btn {
            width: 100%;
            border: none;
            border-radius: 14px;
            padding: 14px 16px;
            margin-top: 6px;
            background: #121212;
            color: #FAF6F3;
            font-size: 15px;
            font-weight: 700;
            font-family: inherit;
            cursor: pointer;
            box-shadow: 0 8px 20px rgba(18, 18, 18, 0.16);
        }
        .login-error {
            background: rgba(217, 56, 72, 0.14);
            border: 1px solid rgba(217, 56, 72, 0.38);
            color: #C81D40;
            border-radius: 12px;
            padding: 10px 12px;
            font-size: 13px;
            font-weight: 600;
            margin-bottom: 14px;
        }
    </style>
    <title>[DEMO] Demo Café — Barista POS</title>
</head>
<body>
    <div class="login-card">
        <div style="display:inline-flex; align-items:center; gap:6px; padding:5px 12px; border-radius:99px; background:rgba(74,93,35,0.12); border:1px solid rgba(74,93,35,0.34); color:#4A5D23; font-size:11px; font-weight:800; letter-spacing:0.6px; text-transform:uppercase; margin-bottom:12px;">
            🧪 ENTORNO DEMO · RÉPLICA DE PRUEBAS
        </div>
        <div class="login-emblem">☕️</div>
        <h1 class="login-title">Iniciar sesión <span style="color:#4A5D23; font-size:15px;">(DEMO)</span></h1>
        <p class="login-sub">Demo Café · Consola Barista DEMO</p>
        {% if error %}
        <div class="login-error">{{ error }}</div>
        {% endif %}
        <form action="/barista/login" method="POST">
            <input type="text" name="username" class="login-field" placeholder="Nombre de usuario" autocomplete="username" autocapitalize="none" required autofocus>
            <input type="password" name="password" class="login-field" placeholder="Contraseña" autocomplete="current-password" required>
            <button type="submit" class="login-btn">Iniciar sesión</button>
        </form>
    </div>
    <script>
        if ('serviceWorker' in navigator) {
            navigator.serviceWorker.register('/sw.js').catch(() => {});
        }
    </script>
</body>
</html>
"""


def authenticate(error=None, status_code=200):
    return Response(render_template_string(BARISTA_LOGIN_HTML, error=error), status=status_code, mimetype="text/html")


def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_barista_authenticated():
            return authenticate()
        return f(*args, **kwargs)
    return decorated


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PASS_TEMPLATE_DIR = os.path.join(BASE_DIR, "maishas-cafe-pass")

# DATA_DIR será el volumen persistente en Railway. En local usa la carpeta "data"
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
os.makedirs(DATA_DIR, exist_ok=True)

KEY_PEM = os.path.join(DATA_DIR, "key.pem")
CERT_PEM = os.path.join(DATA_DIR, "cert.pem")
WWDR_PEM = os.path.join(DATA_DIR, "wwdr.pem")
DB_PATH = os.path.join(DATA_DIR, "loyalty.db")
VAPID_FILE = os.path.join(DATA_DIR, "vapid_keys.json")
CUSTOM_LOGO_PATH = os.path.join(DATA_DIR, "campaign_logo.png")
CUSTOM_STRIP_BG_PATH = os.path.join(DATA_DIR, "campaign_strip_bg.png")


AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "MaishasSecretAuthToken2026")

# Cargar bóveda cifrada local (vault.enc) si no se definieron variables manuales en Railway
_VAULT_PATH = os.path.join(BASE_DIR, "vault.enc")
if os.path.isfile(_VAULT_PATH):
    try:
        from cryptography.fernet import Fernet
        _vkey = base64.urlsafe_b64encode(hashlib.sha256(f"{AUTH_TOKEN}:replica-vault-v1".encode("utf-8")).digest())
        with open(_VAULT_PATH, "rb") as _vf:
            _vdata = json.loads(Fernet(_vkey).decrypt(_vf.read().strip()).decode("utf-8"))
        if not os.environ.get("ENV_CERT_PEM") and _vdata.get("cert_pem"):
            os.environ["ENV_CERT_PEM"] = _vdata["cert_pem"]
        if not os.environ.get("ENV_KEY_PEM") and _vdata.get("key_pem"):
            os.environ["ENV_KEY_PEM"] = _vdata["key_pem"]
        if not os.environ.get("ENV_WWDR_PEM") and _vdata.get("wwdr_pem"):
            os.environ["ENV_WWDR_PEM"] = _vdata["wwdr_pem"]
        if not os.environ.get("GOOGLE_WALLET_ISSUER_ID") and _vdata.get("gw_issuer_id"):
            os.environ["GOOGLE_WALLET_ISSUER_ID"] = _vdata["gw_issuer_id"]
        if not os.environ.get("GOOGLE_WALLET_SERVICE_ACCOUNT_JSON") and _vdata.get("gw_sa_json"):
            os.environ["GOOGLE_WALLET_SERVICE_ACCOUNT_JSON"] = _vdata["gw_sa_json"]
    except Exception as _ve:
        print(f"⚠️ No se pudo abrir vault.enc: {_ve}")

# Auto-inyectar certificados Apple desde Variables de Entorno en Railway
for env_var, file_path in [("ENV_KEY_PEM", KEY_PEM), ("ENV_CERT_PEM", CERT_PEM), ("ENV_WWDR_PEM", WWDR_PEM)]:
    val = os.environ.get(env_var)
    if val:
        val = val.replace('\\n', '\n').strip() + '\n'
        with open(file_path, 'w') as f:
            f.write(val)

PASS_TYPE_ID = "pass.com.maishascafe.loyalty"
DEFAULT_MESSAGE = "¡Bienvenido a Demo Café! ☕️"
DEFAULT_LAT = 27.0706
DEFAULT_LNG = -109.4437
DEFAULT_GEO_TEXT = "☕️ ¡Estás cerca de Demo Café! Pasa por tu café favorito y suma tu sello de hoy ✨"

# Zona horaria de Navojoa, Sonora (America/Hermosillo: UTC-7 fijo todo el año)
SONORA_TZ = timezone(timedelta(hours=-7))
MESES_ES = ["", "Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]


def init_vapid_keys():
    """Inicializa o carga las llaves VAPID (ECDSA P-256) para Notificaciones Web Push en Android."""
    if os.path.isfile(VAPID_FILE):
        try:
            with open(VAPID_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("private_pem") and data.get("public_key"):
                    return data["private_pem"], data["public_key"]
        except Exception:
            pass

    v = Vapid01()
    v.generate_keys()
    priv_pem = v.private_pem().decode("utf-8")
    raw_pub = v.public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint
    )
    pub_b64 = base64.urlsafe_b64encode(raw_pub).rstrip(b"=").decode("utf-8")
    data = {"private_pem": priv_pem, "public_key": pub_b64}
    with open(VAPID_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return priv_pem, pub_b64


VAPID_PRIVATE_PEM, VAPID_PUBLIC_KEY = init_vapid_keys()


def get_utc_timestamp():
    """Retorna timestamp UTC con precisión de microsegundos compatible con SQLite e iOS PassKit."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def get_short_time():
    """Retorna la hora local de Navojoa, Sonora (UTC-7) en formato sencillo de 12 horas para el barista."""
    return datetime.now(SONORA_TZ).strftime("%I:%M %p").lstrip("0")


def get_sonora_datetime_str():
    """Retorna fecha y hora local de Navojoa, Sonora (UTC-7) para registro de alta y reportes."""
    now_s = datetime.now(SONORA_TZ)
    return f"{now_s.day:02d}/{MESES_ES[now_s.month]}/{now_s.year} {now_s.strftime('%I:%M %p').lstrip('0')}"


def format_sonora_time(raw_val):
    """Convierte cualquier hora UTC antigua o timestamp a hora local amigable de Navojoa, Sonora."""
    if not raw_val:
        return ""
    s = str(raw_val).strip()
    # Si es un timestamp completo UTC (YYYY-MM-DD HH:MM:SS...)
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            dt_utc = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            dt_son = dt_utc.astimezone(SONORA_TZ)
            return f"{dt_son.day:02d}/{MESES_ES[dt_son.month]}/{dt_son.year} {dt_son.strftime('%I:%M %p').lstrip('0')}"
        except Exception:
            pass
    # Si contiene una hora corta en UTC del tipo '04:28:11 UTC'
    m = re.search(r"(\d{2}):(\d{2})(?::\d{2})?\s*UTC", s)
    if m:
        hh_utc, mm_utc = int(m.group(1)), int(m.group(2))
        hh_son = (hh_utc - 7) % 24
        ampm = "AM" if hh_son < 12 else "PM"
        hh_12 = hh_son % 12 or 12
        return f"{hh_12}:{mm_utc:02d} {ampm}"
    return s


def humanize_push_status(raw_status):
    """Traduce estados técnicos (como '200 OK' o 'UTC') a lenguaje claro para el barista en hora de Sonora."""
    if not raw_status:
        return ""
    s = str(raw_status).strip()
    t_son = format_sonora_time(s)
    # Extraer hora amigable si existe entre paréntesis
    m_par = re.search(r"\(([^)]+)\)", s)
    time_part = format_sonora_time(m_par.group(1)) if m_par else t_son
    if "200 OK" in s or "Notificación enviada" in s or "Live Sync" in s or "actualizada" in s:
        if time_part and time_part != s:
            return f"✅ Notificación enviada ({time_part})"
        return "✅ Notificación enviada"
    if "Error" in s:
        return f"⚠️ No se pudo notificar ({time_part})" if time_part else "⚠️ No se pudo notificar"
    return s


def get_vip_tier(total_visits):
    """Calcula el nivel VIP del cliente según su histórico acumulado de visitas."""
    visits = total_visits or 0
    if visits >= 25:
        return {
            "key": "gold",
            "name": "Socio Oro VIP ♛",
            "short": "Oro VIP ♛",
            "badge": "♛ Oro VIP",
            "bg": "linear-gradient(135deg, #D4AF37 0%, #F3E5AB 50%, #AA7C11 100%)",
            "fg": "#12100E",
            "border": "rgba(212, 175, 55, 0.5)",
            "next_goal": "¡Máximo nivel VIP alcanzado!"
        }
    elif visits >= 10:
        return {
            "key": "silver",
            "name": "Socio Plata ✦",
            "short": "Plata ✦",
            "badge": "✦ Plata",
            "bg": "linear-gradient(135deg, #E0E5EC 0%, #B8C2CC 50%, #8A95A5 100%)",
            "fg": "#12100E",
            "border": "rgba(224, 229, 236, 0.4)",
            "next_goal": f"A {25 - visits} visitas de Oro VIP ♛"
        }
    else:
        return {
            "key": "bronze",
            "name": "Amante del Café ☕️",
            "short": "Bronce ☕️",
            "badge": "🌱 Bronce",
            "bg": "rgba(78, 48, 27, 0.10)",
            "fg": "#4E301B",
            "border": "rgba(78, 48, 27, 0.24)",
            "next_goal": f"A {10 - visits} visitas de Nivel Plata ✦"
        }


def calculate_birthday_info(birthdate_str, ref_date=None):
    """
    Analiza la fecha de nacimiento (YYYY-MM-DD) e identifica si el cliente cumple años
    HOY, en los próximos 7 días o en el mes actual (usando fecha local de Navojoa, Sonora).
    """
    empty_res = {
        "has_birthdate": False,
        "is_today": False,
        "is_upcoming_week": False,
        "is_this_month": False,
        "days_until": 999,
        "short_date": "",
        "badge_text": "",
    }
    if not birthdate_str:
        return empty_res

    raw = birthdate_str.strip()
    try:
        parts = raw.replace("/", "-").split("-")
        if len(parts) == 3:
            if len(parts[0]) == 4:
                month, day = int(parts[1]), int(parts[2])
            else:
                day, month = int(parts[0]), int(parts[1])
        elif len(parts) == 2:
            month, day = int(parts[0]), int(parts[1])
        else:
            return empty_res

        if not (1 <= month <= 12 and 1 <= day <= 31):
            return empty_res
    except Exception:
        return empty_res

    today = ref_date or datetime.now(SONORA_TZ).date()

    def _safe_date(yr, m, d):
        try:
            return date(yr, m, d)
        except ValueError:
            return date(yr, 2, 28)

    bday_this_year = _safe_date(today.year, month, day)
    if bday_this_year < today:
        next_bday = _safe_date(today.year + 1, month, day)
    else:
        next_bday = bday_this_year

    days_until = (next_bday - today).days
    short_date = f"{day:02d} {MESES_ES[month]}"
    is_today = (days_until == 0)
    is_upcoming_week = (0 <= days_until <= 7)
    is_this_month = (month == today.month)

    if is_today:
        badge_text = "🎉 ¡HOY!"
    elif days_until == 1:
        badge_text = f"Mañana · {short_date}"
    elif is_upcoming_week:
        badge_text = f"En {days_until} días · {short_date}"
    else:
        badge_text = f"🎂 {short_date}"

    return {
        "has_birthdate": True,
        "is_today": is_today,
        "is_upcoming_week": is_upcoming_week,
        "is_this_month": is_this_month,
        "days_until": days_until,
        "short_date": short_date,
        "badge_text": badge_text,
    }


def _load_strip_font(size=26):
    """Carga la fuente geométrica ExtraBold del diseño (Plus Jakarta Sans) o respaldo TrueType con soporte UTF-8."""
    candidates = [
        os.path.join(BASE_DIR, "static", "PlusJakartaSans-ExtraBold.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for path in candidates:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size=size), True
            except Exception:
                pass
    try:
        return ImageFont.load_default(size=size), False
    except TypeError:
        return ImageFont.load_default(), False


def _clean_text_for_pil(text, has_utf8_font=False):
    """Limpia emojis fuera de rango y adapta acentos según soporte de la fuente para renderizado nítido sin cuadros vacíos."""
    if not text:
        return ""
    if has_utf8_font:
        cleaned = "".join(ch for ch in text if 32 <= ord(ch) <= 255)
    else:
        import unicodedata
        norm = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
        cleaned = "".join(ch for ch in norm if 32 <= ord(ch) <= 126)
    return " ".join(cleaned.split()).strip()


def _hex_to_rgb_tuple(hex_str, default=(18, 16, 14)):
    s = (hex_str or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) == 6:
        try:
            return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
        except ValueError:
            pass
    return default


def _hex_to_passkit_rgb(hex_str, default="rgb(18, 16, 14)"):
    r, g, b = _hex_to_rgb_tuple(hex_str, default=None) or (None, None, None)
    if r is None:
        return default
    return f"rgb({r}, {g}, {b})"


def _cubic_bezier_pts(p0, p1, p2, p3, steps=22):
    pts = []
    for i in range(steps + 1):
        t = i / float(steps)
        mt = 1.0 - t
        x = (mt ** 3) * p0[0] + 3 * (mt ** 2) * t * p1[0] + 3 * mt * (t ** 2) * p2[0] + (t ** 3) * p3[0]
        y = (mt ** 3) * p0[1] + 3 * (mt ** 2) * t * p1[1] + 3 * mt * (t ** 2) * p2[1] + (t ** 3) * p3[1]
        pts.append((x, y))
    return pts


def _draw_vertical_coffee_bean_pil(draw, cx, cy, radius, fill_color):
    """Dibuja un grano de café vertical y simétrico de dos mitades con hendidura en S (idéntico al SVG del Simulador)."""
    scale = radius * 0.052

    def _tr(pt):
        return (cx + (pt[0] - 12.0) * scale, cy + (pt[1] - 12.0) * scale)

    left_pts = (
        _cubic_bezier_pts((10.95, 3.12), (7.35, 3.85), (5.35, 7.45), (5.35, 12.0))[:-1]
        + _cubic_bezier_pts((5.35, 12.0), (5.35, 16.45), (7.25, 19.95), (10.75, 20.82))[:-1]
        + _cubic_bezier_pts((10.75, 20.82), (12.95, 15.65), (8.55, 8.25), (10.95, 3.12))
    )
    right_pts = (
        _cubic_bezier_pts((13.05, 20.88), (16.65, 20.15), (18.65, 16.55), (18.65, 12.0))[:-1]
        + _cubic_bezier_pts((18.65, 12.0), (18.65, 7.55), (16.75, 4.05), (13.25, 3.18))[:-1]
        + _cubic_bezier_pts((13.25, 3.18), (11.05, 8.35), (15.45, 15.75), (13.05, 20.88))
    )
    draw.polygon([_tr(p) for p in left_pts], fill=fill_color)
    draw.polygon([_tr(p) for p in right_pts], fill=fill_color)


def _draw_gift_box_pil(draw, cx, cy, radius):
    """Dibuja el ícono de regalo 🎁 en el último círculo pendiente (idéntico al Simulador Apple Wallet)."""
    s = radius * 0.44
    box_w = int(s * 1.15)
    box_h = int(s * 0.85)
    lid_w = int(s * 1.28)
    lid_h = int(s * 0.34)
    by_top = int(cy - s * 0.12)
    by_bot = int(cy + box_h)
    ly_top = int(by_top - lid_h)

    # Moño superior dorado
    ribbon_gold = (248, 196, 46)
    ribbon_dark = (214, 154, 18)
    draw.ellipse([cx - int(s * 0.62), ly_top - int(s * 0.42), cx - int(s * 0.04), ly_top + int(s * 0.12)], fill=ribbon_gold, outline=ribbon_dark, width=2)
    draw.ellipse([cx + int(s * 0.04), ly_top - int(s * 0.42), cx + int(s * 0.62), ly_top + int(s * 0.12)], fill=ribbon_gold, outline=ribbon_dark, width=2)

    # Caja base roja/coral y tapa
    draw.rounded_rectangle([cx - box_w, by_top, cx + box_w, by_bot], radius=6, fill=(224, 67, 67))
    draw.rounded_rectangle([cx - lid_w, ly_top, cx + lid_w, by_top + 2], radius=5, fill=(238, 82, 83))

    # Listón vertical y horizontal dorado
    rw = max(4, int(s * 0.24))
    draw.rectangle([cx - rw, ly_top, cx + rw, by_bot], fill=ribbon_gold)
    draw.rectangle([cx - box_w, int((by_top + by_bot) / 2) - rw // 2, cx + box_w, int((by_top + by_bot) / 2) + rw // 2], fill=ribbon_gold)


def _render_circular_logo_rgba(src_path, size):
    """Recorta cualquier logotipo en formato circular perfecto con antialiasing 4x (igual que border-radius:50% en el Simulador)."""
    ss = size * 4
    src = Image.open(src_path).convert("RGBA")
    w, h = src.size
    if w != h:
        m = min(w, h)
        left = (w - m) // 2
        top = (h - m) // 2
        src = src.crop((left, top, left + m, top + m))
    src = src.resize((ss, ss), Image.Resampling.LANCZOS)

    mask = Image.new("L", (ss, ss), 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.ellipse([2, 2, ss - 3, ss - 3], fill=255)

    out = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    out.paste(src, (0, 0), mask)

    bdraw = ImageDraw.Draw(out)
    bdraw.ellipse([2, 2, ss - 3, ss - 3], outline=(255, 255, 255, 68), width=max(2, ss // 32))

    return out.resize((size, size), Image.Resampling.LANCZOS)


def _create_dynamic_strip_pil(stamps_count, message=None, campaign=None):
    """
    Crea el objeto PIL Image (1500x500 super-sampled) replicando con exactitud milimétrica
    el diseño del Simulador Apple Wallet del Dashboard de Campaña.
    """
    cfg = campaign if isinstance(campaign, dict) else get_campaign_config()
    goal = max(4, min(12, int(cfg.get("stampGoal") or 10)))
    stamps = max(0, min(goal, int(stamps_count or 0)))
    W, H = 1500, 500

    bg_rgb = _hex_to_rgb_tuple(cfg.get("backgroundColor"), (28, 19, 14))
    fg_rgb = _hex_to_rgb_tuple(cfg.get("foregroundColor"), (250, 246, 243))
    accent_rgb = _hex_to_rgb_tuple(cfg.get("labelColor"), (197, 168, 105))

    # Color base de la tira: rgba(0, 0, 0, 0.28) sobre backgroundColor (idéntico a #ca-prev-strip-box en el Simulador)
    strip_bg_rgb = (
        int(bg_rgb[0] * 0.72),
        int(bg_rgb[1] * 0.72),
        int(bg_rgb[2] * 0.72),
    )
    img = Image.new("RGB", (W, H), strip_bg_rgb)

    if (cfg.get("useCustomStrip") or cfg.get("useCustomStripBg")) and os.path.isfile(CUSTOM_STRIP_BG_PATH):
        try:
            bg_img = Image.open(CUSTOM_STRIP_BG_PATH).convert("RGB").resize((W, H), Image.Resampling.LANCZOS)
            overlay = Image.new("RGB", (W, H), (12, 10, 9))
            img = Image.blend(bg_img, overlay, 0.62)
            strip_bg_rgb = (36, 32, 28)
        except Exception:
            pass

    draw = ImageDraw.Draw(img)

    # Líneas sutiles superior e inferior: border-top / border-bottom 1px solid rgba(255,255,255,0.12)
    line_col = tuple(min(255, int(strip_bg_rgb[c] * 0.88 + 255 * 0.12)) for c in range(3))
    draw.line([(0, 1), (W, 1)], fill=line_col, width=3)
    draw.line([(0, H - 2), (W, H - 2)], fill=line_col, width=3)

    # Encabezado superior dentro del Strip: "TARJETA DE FIDELIDAD" (izq) y "X/Y SELLOS" (der)
    font_hdr, has_utf8 = _load_strip_font(size=31)
    title_left = "TARJETA DE FIDELIDAD"
    status_right = ("¡META COMPLETADA!" if has_utf8 else "META COMPLETADA!") if stamps >= goal else f"{stamps}/{goal} SELLOS"

    pad_x = 76
    hdr_y = 28
    draw.text((pad_x, hdr_y), title_left, fill=accent_rgb, font=font_hdr)
    rb = draw.textbbox((0, 0), status_right, font=font_hdr)
    rw = rb[2] - rb[0]
    draw.text((W - pad_x - rw, hdr_y), status_right, fill=fg_rgb, font=font_hdr)

    # Distribución de columnas idéntica al Simulador (goal <= 4 -> 4, <= 6 -> 3, <= 8 -> 4, <= 10 -> 5, else 6)
    cols_per_row = 4 if goal <= 4 else (3 if goal <= 6 else (4 if goal <= 8 else (5 if goal <= 10 else 6)))
    num_rows = int(math.ceil(goal / float(cols_per_row)))
    rows_y = [174, 318] if num_rows >= 2 else [246]
    radius = 58 if cols_per_row <= 5 else 50

    # Espaciado horizontal equivalente a CSS grid-template-columns: repeat(cols, 1fr); justify-items: center
    grid_left = 76
    grid_width = W - 2 * grid_left
    cell_w = grid_width / float(cols_per_row)

    font_num, _ = _load_strip_font(size=40)

    # Color del grano de café dentro del círculo ganado: bg_rgb (igual que txtCol = filled ? bg : ... en el Simulador)
    bean_luma = 0.299 * bg_rgb[0] + 0.587 * bg_rgb[1] + 0.114 * bg_rgb[2]
    acc_luma = 0.299 * accent_rgb[0] + 0.587 * accent_rgb[1] + 0.114 * accent_rgb[2]
    bean_col = bg_rgb if abs(bean_luma - acc_luma) >= 35 else ((250, 246, 243) if acc_luma < 150 else (18, 16, 14))

    pending_fill = tuple(min(255, int(strip_bg_rgb[c] * 0.95 + 255 * 0.05)) for c in range(3))
    pending_border = tuple(min(255, int(strip_bg_rgb[c] * 0.76 + 255 * 0.24)) for c in range(3))
    pending_txt = tuple(min(255, int(strip_bg_rgb[c] * 0.55 + 255 * 0.45)) for c in range(3))

    for idx in range(goal):
        row = idx // cols_per_row
        col = idx % cols_per_row
        cx = int(grid_left + (col + 0.5) * cell_w)
        cy = rows_y[min(row, len(rows_y) - 1)]
        is_earned = (idx < stamps)
        is_last = (idx == goal - 1)

        if is_earned:
            # Resplandor suave exterior (box-shadow: 0 0 10px rgba(212,175,55,0.35))
            for glow_extra, alpha_g in ((10, 0.12), (6, 0.22), (3, 0.34)):
                gr = radius + glow_extra
                g_col = tuple(min(255, int(strip_bg_rgb[c] * (1.0 - alpha_g) + accent_rgb[c] * alpha_g)) for c in range(3))
                draw.ellipse([cx - gr, cy - gr, cx + gr, cy + gr], fill=g_col)

            # Círculo sólido limpio del color acento (sin aros oscuros concéntricos)
            draw.ellipse(
                [cx - radius, cy - radius, cx + radius, cy + radius],
                fill=accent_rgb,
                outline=accent_rgb,
                width=4
            )
            _draw_vertical_coffee_bean_pil(draw, cx, cy, radius, bean_col)
        else:
            # Círculo pendiente limpio con borde translúcido suave
            draw.ellipse(
                [cx - radius, cy - radius, cx + radius, cy + radius],
                fill=pending_fill,
                outline=pending_border,
                width=5
            )
            if is_last:
                _draw_gift_box_pil(draw, cx, cy, radius)
            else:
                num_str = str(idx + 1)
                nb = draw.textbbox((0, 0), num_str, font=font_num)
                nw = nb[2] - nb[0]
                nh = nb[3] - nb[1]
                draw.text((cx - nw // 2 - nb[0], cy - nh // 2 - nb[1]), num_str, fill=pending_txt, font=font_num)

    # Pie inferior dentro del Strip: "Premio: <rewardText>" centrado en color acento (sin recuadro)
    font_footer, has_utf8_f = _load_strip_font(size=32)
    reward_cfg_clean = _clean_text_for_pil(cfg.get("rewardText") or "1 Café Gratis", has_utf8_font=has_utf8_f)
    footer_text = f"Premio: {reward_cfg_clean}"
    if len(footer_text) > 58:
        footer_text = footer_text[:55] + "..."
    fb = draw.textbbox((0, 0), footer_text, font=font_footer)
    fw = fb[2] - fb[0]
    fh = fb[3] - fb[1]
    draw.text(((W - fw) // 2 - fb[0], 438 - fh // 2 - fb[1]), footer_text, fill=accent_rgb, font=font_footer)

    return img


def render_dynamic_strip(stamps_count, output_dir, total_visits=0, message=None, campaign=None):
    """
    Guarda strip.png (375x125), strip@2x.png (750x250) y strip@3x.png (1125x375) para Apple Wallet .pkpass.
    """
    img = _create_dynamic_strip_pil(stamps_count, message=message, campaign=campaign)
    img_3x = img.resize((1125, 375), Image.Resampling.LANCZOS)
    img_2x = img.resize((750, 250), Image.Resampling.LANCZOS)
    img_1x = img.resize((375, 125), Image.Resampling.LANCZOS)
    img_3x.save(os.path.join(output_dir, "strip@3x.png"), format="PNG", optimize=True)
    img_2x.save(os.path.join(output_dir, "strip@2x.png"), format="PNG", optimize=True)
    img_1x.save(os.path.join(output_dir, "strip.png"), format="PNG", optimize=True)


DEFAULT_GIFT_COMBOS_LIST = [
    {
        "key": "cafe_individual",
        "icon": "☕",
        "title": "Vale «Te invito un Café»",
        "short_title": "Te invito un Café",
        "desc": "1 bebida caliente o fría de cualquier tamaño",
        "price": 85.0,
        "uses": 1,
    },
    {
        "key": "manana_perfecta",
        "icon": "🥐",
        "title": "Pack «Mañana Perfecta»",
        "short_title": "Pack Mañana Perfecta",
        "desc": "1 bebida + 1 postre o panini recién hecho",
        "price": 160.0,
        "uses": 1,
    },
    {
        "key": "cita_para_dos",
        "icon": "👯",
        "title": "Pack «Cita para Dos»",
        "short_title": "Pack Cita para Dos",
        "desc": "2 bebidas + 2 postres o paninis para compartir",
        "price": 280.0,
        "uses": 1,
    },
]

GIFT_COMBO_CATALOG = {item["key"]: item for item in DEFAULT_GIFT_COMBOS_LIST}
GIFT_COMBO_CATALOG["cafe_amigo"] = GIFT_COMBO_CATALOG["cafe_individual"]
GIFT_COMBO_CATALOG["cita_dos"] = GIFT_COMBO_CATALOG["cita_para_dos"]


def get_gift_combos_list():
    """Devuelve la lista de vales/combos configurados por el barista para las Gift Cards."""
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT value FROM settings WHERE key = 'gift_combos_json'").fetchone()
        conn.close()
        if row and row[0]:
            parsed = json.loads(row[0])
            if isinstance(parsed, list) and len(parsed) > 0:
                out = []
                for idx, item in enumerate(parsed):
                    if not isinstance(item, dict):
                        continue
                    title = str(item.get("title") or "").strip()
                    if not title:
                        continue
                    key = str(item.get("key") or f"combo_{idx+1}").strip().lower()
                    icon = str(item.get("icon") or "🥐").strip() or "🥐"
                    desc = str(item.get("desc") or "Experiencia especial en sucursal").strip()
                    try:
                        price = max(10.0, round(float(item.get("price") or 100.0), 2))
                    except Exception:
                        price = 100.0
                    out.append({
                        "key": key,
                        "icon": icon[:4],
                        "title": title[:80],
                        "short_title": str(item.get("short_title") or title)[:40],
                        "desc": desc[:140],
                        "price": price,
                        "uses": 1,
                    })
                if out:
                    return out
    except Exception:
        pass
    return [dict(x) for x in DEFAULT_GIFT_COMBOS_LIST]


def get_gift_combos_map():
    """Devuelve un diccionario por key con todos los combos activos + alias de compatibilidad."""
    combos_list = get_gift_combos_list()
    cmap = {c["key"]: c for c in combos_list}
    if "cafe_individual" in cmap and "cafe_amigo" not in cmap:
        cmap["cafe_amigo"] = cmap["cafe_individual"]
    elif combos_list and "cafe_amigo" not in cmap:
        cmap["cafe_amigo"] = combos_list[0]
    if "cita_para_dos" in cmap and "cita_dos" not in cmap:
        cmap["cita_dos"] = cmap["cita_para_dos"]
    return cmap


def _create_gift_strip_pil(gift_data, campaign=None):
    """
    Crea el banner (1500x576 super-sampled, proporción exacta 375x144 de Apple Wallet storeCard)
    con la paleta Latte / Crema / Arena / Oliva / Espresso de todo el sistema, sin recortes laterales
    y sin duplicar PARA / DE PARTE DE dentro del cuadro de saldo.
    """
    cfg = campaign if isinstance(campaign, dict) else get_campaign_config()
    W, H = 1500, 576

    # Paleta oficial de todo el sistema (Hub, Gift Card page, Barista POS)
    strip_bg_rgb = (234, 223, 215)   # #EADFD7 Arena cálido
    card_fill = (250, 246, 243)      # #FAF6F3 Crema editorial
    border_col = (206, 192, 180)     # Borde sutil arena oscuro
    line_col = (214, 200, 189)       # Divisor superior/inferior
    text_dark = (18, 18, 18)         # #121212 Espresso / Negro principal
    text_muted = (110, 101, 95)      # #6E655F Taupe secundario
    olive_rgb = (74, 93, 35)         # #4A5D23 Verde Oliva acento del sistema
    bar_track_rgb = (228, 217, 208)  # Pista de barra de saldo

    img = Image.new("RGB", (W, H), strip_bg_rgb)
    draw = ImageDraw.Draw(img)

    # Líneas divisorias superior e inferior y cuadro interno perfectamente alineado al margen de Apple Wallet
    draw.line([(0, 1), (W, 1)], fill=line_col, width=3)
    draw.line([(0, H - 2), (W, H - 2)], fill=line_col, width=3)
    card_margin_x = 80
    card_margin_y = 32
    draw.rounded_rectangle(
        [card_margin_x, card_margin_y, W - card_margin_x, H - card_margin_y],
        radius=36,
        fill=card_fill,
        outline=border_col,
        width=4
    )

    font_hdr, has_utf8 = _load_strip_font(size=28)
    font_big, _ = _load_strip_font(size=68)
    font_sub, _ = _load_strip_font(size=31)

    gift_type = (gift_data.get("gift_type") or "balance").strip().lower()
    status_val = (gift_data.get("status") or "active").strip().lower()
    shop_name = _clean_text_for_pil(cfg.get("shopName") or "Demo Café", has_utf8_font=has_utf8).upper()

    if status_val == "pending":
        status_badge = "PENDIENTE DE ACTIVAR"
        badge_col = (180, 83, 9)
    elif status_val == "redeemed":
        status_badge = "CANJEADA AL 100%"
        badge_col = olive_rgb
    elif status_val == "cancelled":
        status_badge = "CANCELADA"
        badge_col = (185, 28, 28)
    else:
        status_badge = "ACTIVA EN MOSTRADOR"
        badge_col = olive_rgb

    pad_x = 122
    hdr_y = 68
    left_hdr = f"GIFT CARD - {shop_name}"
    draw.text((pad_x, hdr_y), left_hdr, fill=olive_rgb, font=font_hdr)

    rb = draw.textbbox((0, 0), status_badge, font=font_hdr)
    rw = rb[2] - rb[0]
    draw.text((W - pad_x - rw, hdr_y), status_badge, fill=badge_col, font=font_hdr)

    # Línea separadora suave debajo del encabezado del cuadro
    draw.line([(pad_x, 122), (W - pad_x, 122)], fill=(232, 222, 214), width=2)

    # Emblema circular de Regalo a la izquierda, centrado en el área útil del cuadro
    emblem_cx, emblem_cy, emblem_r = 222, 332, 86
    for g_off, g_col in ((12, (238, 229, 222)), (6, (226, 214, 205))):
        draw.ellipse(
            [emblem_cx - emblem_r - g_off, emblem_cy - emblem_r - g_off, emblem_cx + emblem_r + g_off, emblem_cy + emblem_r + g_off],
            fill=g_col
        )
    draw.ellipse(
        [emblem_cx - emblem_r, emblem_cy - emblem_r, emblem_cx + emblem_r, emblem_cy + emblem_r],
        fill=text_dark,
        outline=olive_rgb,
        width=5
    )
    _draw_gift_box_pil(draw, emblem_cx, emblem_cy, int(emblem_r * 1.15))

    content_x = 348
    if gift_type == "combo":
        pkg_title = _clean_text_for_pil(gift_data.get("package_title") or "Experiencia de Regalo", has_utf8_font=has_utf8)
        pkg_desc = _clean_text_for_pil(gift_data.get("package_desc") or "1 bebida + 1 postre en sucursal", has_utf8_font=has_utf8)
        rem_uses = int(gift_data.get("remaining_uses") or 0)
        main_line = pkg_title.upper()
        if len(main_line) > 26:
            font_big, _ = _load_strip_font(size=50)
        draw.text((content_x, 198), main_line, fill=text_dark, font=font_big)
        sub_line = f"Incluye: {pkg_desc}" if rem_uses > 0 and status_val != "redeemed" else "¡Experiencia disfrutada en sucursal!"
        if len(sub_line) > 52:
            sub_line = sub_line[:49] + "..."
        draw.text((content_x, 298), sub_line, fill=text_muted, font=font_sub)

        bar_x1, bar_y1, bar_x2, bar_y2 = content_x, 382, W - pad_x, 410
        draw.rounded_rectangle([bar_x1, bar_y1, bar_x2, bar_y2], radius=14, fill=bar_track_rgb)
        if status_val != "redeemed" and rem_uses > 0:
            draw.rounded_rectangle([bar_x1, bar_y1, bar_x2, bar_y2], radius=14, fill=olive_rgb)
    else:
        init_amt = max(1.0, float(gift_data.get("initial_amount") or 200.0))
        curr_bal = max(0.0, float(gift_data.get("current_balance") or 0.0))
        main_line = f"${curr_bal:,.2f} MXN"
        draw.text((content_x, 192), main_line, fill=text_dark, font=font_big)
        sub_line = f"Saldo disponible (Monto original: ${init_amt:,.0f} MXN)" if curr_bal > 0 else "¡Saldo disfrutado al 100% en sucursal!"
        draw.text((content_x, 296), sub_line, fill=text_muted, font=font_sub)

        bar_x1, bar_y1, bar_x2, bar_y2 = content_x, 380, W - pad_x, 408
        draw.rounded_rectangle([bar_x1, bar_y1, bar_x2, bar_y2], radius=14, fill=bar_track_rgb)
        ratio = max(0.0, min(1.0, curr_bal / init_amt))
        if ratio > 0:
            fill_w = max(28, int((bar_x2 - bar_x1) * ratio))
            draw.rounded_rectangle([bar_x1, bar_y1, bar_x1 + fill_w, bar_y2], radius=14, fill=olive_rgb)

    return img


def render_gift_strip(gift_data, output_dir, campaign=None):
    """Guarda strip.png (375x144), strip@2x.png (750x288) y strip@3x.png (1125x432) con proporción exacta de storeCard."""
    img = _create_gift_strip_pil(gift_data, campaign=campaign)
    img_3x = img.resize((1125, 432), Image.Resampling.LANCZOS)
    img_2x = img.resize((750, 288), Image.Resampling.LANCZOS)
    img_1x = img.resize((375, 144), Image.Resampling.LANCZOS)
    img_3x.save(os.path.join(output_dir, "strip@3x.png"), format="PNG", optimize=True)
    img_2x.save(os.path.join(output_dir, "strip@2x.png"), format="PNG", optimize=True)
    img_1x.save(os.path.join(output_dir, "strip.png"), format="PNG", optimize=True)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS clients (
            serial TEXT PRIMARY KEY,
            name TEXT,
            email TEXT,
            phone TEXT,
            birthdate TEXT,
            stamps INTEGER DEFAULT 0,
            total_visits INTEGER DEFAULT 0,
            redeemed_count INTEGER DEFAULT 0,
            push_token TEXT,
            push_token_auth TEXT,
            message TEXT,
            updated_at TEXT,
            last_push_status TEXT,
            last_sync_at TEXT,
            platform TEXT DEFAULT 'ios',
            android_active INTEGER DEFAULT 0,
            created_at TEXT,
            last_action TEXT DEFAULT 'add',
            status TEXT DEFAULT 'active',
            deactivated_at TEXT DEFAULT '',
            recovery_pin TEXT DEFAULT '',
            recovery_pin_expires INTEGER DEFAULT 0
        )
    ''')
    cols = [r[1] for r in c.execute("PRAGMA table_info(clients)")]
    migrations = [
        ("message", "ALTER TABLE clients ADD COLUMN message TEXT"),
        ("phone", "ALTER TABLE clients ADD COLUMN phone TEXT"),
        ("birthdate", "ALTER TABLE clients ADD COLUMN birthdate TEXT"),
        ("last_push_status", "ALTER TABLE clients ADD COLUMN last_push_status TEXT"),
        ("last_sync_at", "ALTER TABLE clients ADD COLUMN last_sync_at TEXT"),
        ("total_visits", "ALTER TABLE clients ADD COLUMN total_visits INTEGER DEFAULT 0"),
        ("redeemed_count", "ALTER TABLE clients ADD COLUMN redeemed_count INTEGER DEFAULT 0"),
        ("platform", "ALTER TABLE clients ADD COLUMN platform TEXT DEFAULT 'ios'"),
        ("android_active", "ALTER TABLE clients ADD COLUMN android_active INTEGER DEFAULT 0"),
        ("created_at", "ALTER TABLE clients ADD COLUMN created_at TEXT"),
        ("last_action", "ALTER TABLE clients ADD COLUMN last_action TEXT DEFAULT 'add'"),
        ("status", "ALTER TABLE clients ADD COLUMN status TEXT DEFAULT 'active'"),
        ("deactivated_at", "ALTER TABLE clients ADD COLUMN deactivated_at TEXT DEFAULT ''"),
        ("recovery_pin", "ALTER TABLE clients ADD COLUMN recovery_pin TEXT DEFAULT ''"),
        ("recovery_pin_expires", "ALTER TABLE clients ADD COLUMN recovery_pin_expires INTEGER DEFAULT 0"),
    ]
    for col_name, sql in migrations:
        if col_name not in cols:
            c.execute(sql)

    c.execute("UPDATE clients SET total_visits = COALESCE(stamps, 0) WHERE total_visits IS NULL OR (total_visits = 0 AND stamps > 0)")
    c.execute("UPDATE clients SET redeemed_count = 0 WHERE redeemed_count IS NULL")
    c.execute("UPDATE clients SET android_active = 0 WHERE android_active IS NULL")
    c.execute("UPDATE clients SET last_action = 'add' WHERE last_action IS NULL OR last_action = ''")
    c.execute("UPDATE clients SET status = 'active' WHERE status IS NULL OR status = ''")
    c.execute("UPDATE clients SET deactivated_at = '' WHERE deactivated_at IS NULL")
    c.execute("UPDATE clients SET updated_at = ? WHERE updated_at IS NULL OR updated_at = ''", (get_utc_timestamp(),))
    c.execute("UPDATE clients SET created_at = ? WHERE created_at IS NULL OR created_at = ''", (get_sonora_datetime_str(),))
    c.execute(
        "UPDATE clients SET message = ? WHERE message = '¡Bienvenido a Maishas Café! Aquí verás nuestras promociones.'",
        (DEFAULT_MESSAGE,)
    )

    c.execute('''
        CREATE TABLE IF NOT EXISTS registrations (
            device_id TEXT,
            push_token TEXT,
            serial TEXT,
            push_service_url TEXT,
            PRIMARY KEY (device_id, serial)
        )
    ''')
    reg_cols = [r[1] for r in c.execute("PRAGMA table_info(registrations)")]
    if "push_service_url" not in reg_cols:
        c.execute("ALTER TABLE registrations ADD COLUMN push_service_url TEXT")

    c.execute('''
        CREATE TABLE IF NOT EXISTS android_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            serial TEXT,
            endpoint TEXT UNIQUE,
            sub_json TEXT,
            created_at TEXT
        )
    ''')

    # Limpiar falsos positivos donde un cliente de iPhone tenía android_active=1 por sincronización web del Hub
    c.execute("""
        UPDATE clients
        SET platform = 'ios', android_active = 0
        WHERE (COALESCE(platform, 'ios') = 'ios' OR UPPER(serial) IN (SELECT UPPER(serial) FROM registrations) OR push_token IS NOT NULL)
          AND UPPER(serial) NOT IN (SELECT UPPER(serial) FROM android_subscriptions)
    """)

    c.execute('''
        CREATE TABLE IF NOT EXISTS wallet_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            log_text TEXT
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS stamp_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            serial TEXT,
            client_name TEXT,
            action TEXT,
            stamps_before INTEGER,
            stamps_after INTEGER,
            visits_after INTEGER,
            redeemed_after INTEGER,
            created_at_utc TEXT,
            created_at_sonora TEXT,
            detail TEXT
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS birthday_push_logs (
            serial TEXT,
            year INTEGER,
            sent_at_sonora TEXT,
            mode TEXT DEFAULT 'auto',
            PRIMARY KEY (serial, year)
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS hub_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            serial TEXT,
            message TEXT,
            sentiment TEXT,
            has_photo INTEGER DEFAULT 0,
            created_at_sonora TEXT
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS gift_cards (
            code TEXT PRIMARY KEY,
            gift_type TEXT DEFAULT 'balance',
            package_key TEXT DEFAULT '',
            package_title TEXT DEFAULT '',
            package_desc TEXT DEFAULT '',
            initial_amount REAL DEFAULT 0,
            current_balance REAL DEFAULT 0,
            total_uses INTEGER DEFAULT 1,
            remaining_uses INTEGER DEFAULT 1,
            sender_name TEXT DEFAULT '',
            sender_phone TEXT DEFAULT '',
            recipient_name TEXT DEFAULT '',
            recipient_phone TEXT DEFAULT '',
            dedication TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            converted_serial TEXT DEFAULT '',
            created_at_utc TEXT,
            created_at_sonora TEXT,
            activated_at_sonora TEXT DEFAULT '',
            updated_at TEXT,
            last_action_detail TEXT DEFAULT ''
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS gift_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT,
            action TEXT,
            amount_delta REAL DEFAULT 0,
            balance_after REAL DEFAULT 0,
            detail TEXT,
            created_at_utc TEXT,
            created_at_sonora TEXT
        )
    ''')

    # Sembrar 2 Gift Cards de demostración listas para probar en el entorno DEMO
    now_utc_seed = get_utc_timestamp()
    now_son_seed = get_sonora_datetime_str()
    c.execute(
        """INSERT OR IGNORE INTO gift_cards (
               code, gift_type, package_key, package_title, package_desc,
               initial_amount, current_balance, total_uses, remaining_uses,
               sender_name, sender_phone, recipient_name, recipient_phone,
               dedication, status, converted_serial, created_at_utc, created_at_sonora,
               activated_at_sonora, updated_at, last_action_detail
           ) VALUES (?, 'balance', '', 'Saldo de Regalo $250 MXN', 'Válido en cualquier bebida, frappé o postre del menú',
                     250.0, 250.0, 1, 1,
                     'Gonzalo Gómez', '6421138069', 'Ana Sofía', '',
                     '🎂 ¡Feliz cumpleaños Ana Sofía! Te invito tu café y postre favorito en Demo Café ☕✨',
                     'active', '', ?, ?, ?, ?, '✅ Gift Card activa con $250.00 MXN disponibles')""",
        ("GIFT-DEMO01", now_utc_seed, now_son_seed, now_son_seed, now_utc_seed)
    )
    c.execute(
        """INSERT OR IGNORE INTO gift_cards (
               code, gift_type, package_key, package_title, package_desc,
               initial_amount, current_balance, total_uses, remaining_uses,
               sender_name, sender_phone, recipient_name, recipient_phone,
               dedication, status, converted_serial, created_at_utc, created_at_sonora,
               activated_at_sonora, updated_at, last_action_detail
           ) VALUES (?, 'combo', 'cita_para_dos', 'Pack «Cita para Dos»', '2 bebidas + 2 postres o paninis para compartir',
                     280.0, 280.0, 1, 1,
                     'Carlos Rivera', '6421002030', 'Mariana & Luis', '',
                     '💛 ¡Disfruten una tarde increíble juntos con un buen café!',
                     'active', '', ?, ?, ?, ?, '✅ Vale activo: Pack «Cita para Dos»')""",
        ("GIFT-COMBO1", now_utc_seed, now_son_seed, now_son_seed, now_utc_seed)
    )

    c.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    default_settings = {
        "shop_lat": str(DEFAULT_LAT),
        "shop_lng": str(DEFAULT_LNG),
        "shop_relevant_text": DEFAULT_GEO_TEXT,
        "bday_auto_enabled": "1",
        "bday_auto_hour": "08:00",
        "bday_auto_template": "🎂 ¡Feliz cumpleaños, {nombre}! Hoy tienes una bebida o postre de cortesía en Demo Café ☕️✨",
        "hub_slogan": "Tu rincón favorito de café de especialidad",
        "hub_wifi_ssid": "Demo_Cafe_WiFi",
        "hub_wifi_pass": "democafe2026",
        "hub_google_review_url": "https://www.google.com/maps",
        "hub_instagram_url": "https://www.instagram.com/",
        "hub_instagram_handle": "@democafe",
        "hub_address": "Av. Principal #100, Col. Centro",
        "hub_phone": "642 113 8069",
    }
    for k, v in default_settings.items():
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

    # Migrar valores preexistentes de Maisha's Café a Demo Café en la base de datos DEMO
    rebrand_done = c.execute("SELECT value FROM settings WHERE key = 'demo_rebrand_v1'").fetchone()
    if not rebrand_done:
        for k, v in default_settings.items():
            if k in ("shop_relevant_text", "bday_auto_template", "hub_slogan", "hub_wifi_ssid", "hub_wifi_pass", "hub_google_review_url", "hub_instagram_url", "hub_instagram_handle", "hub_address"):
                c.execute("UPDATE settings SET value = ? WHERE key = ?", (v, k))
        cfg_row = c.execute("SELECT value FROM settings WHERE key = 'campaign_config_json'").fetchone()
        if cfg_row and cfg_row[0]:
            try:
                cfg_obj = json.loads(cfg_row[0])
                if isinstance(cfg_obj, dict):
                    for field_k in ("shopName", "termsText", "relevantText"):
                        if isinstance(cfg_obj.get(field_k), str):
                            cfg_obj[field_k] = (
                                cfg_obj[field_k]
                                .replace("Maisha's Café", "Demo Café")
                                .replace("Maishas Café", "Demo Café")
                                .replace("MAISHA'S CAFÉ", "DEMO CAFÉ")
                                .replace("MAISHAS CAFÉ", "DEMO CAFÉ")
                                .replace("Maisha's", "Demo Café")
                                .replace("Maishas", "Demo Café")
                            )
                    c.execute(
                        "UPDATE settings SET value = ? WHERE key = 'campaign_config_json'",
                        (json.dumps(cfg_obj, ensure_ascii=False),)
                    )
            except Exception:
                pass
        c.execute(
            "UPDATE clients SET message = REPLACE(REPLACE(message, 'Maisha''s Café', 'Demo Café'), 'Maishas Café', 'Demo Café') WHERE message LIKE '%Maisha%'"
        )
        if os.path.isfile(CUSTOM_LOGO_PATH):
            try:
                os.remove(CUSTOM_LOGO_PATH)
            except Exception:
                pass
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('demo_rebrand_v1', '1')")

    conn.commit()
    conn.close()


init_db()


def get_hub_config():
    """Obtiene los ajustes del Hub de Experiencia en Mesa (Wi-Fi, Google Reviews, Eslogan, Instagram, Dirección)."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    rows = dict(c.execute(
        "SELECT key, value FROM settings WHERE key LIKE 'hub_%'"
    ).fetchall())
    conn.close()

    def _clean_maisha(val, fallback):
        s = (val or fallback).strip()
        for old, new in [
            ("Maisha's Café", "Demo Café"),
            ("Maishas Café", "Demo Café"),
            ("Maishas_Cafe", "Demo_Cafe_WiFi"),
            ("cafemaishas", "democafe2026"),
            ("https://www.instagram.com/maishascafe/", "https://www.instagram.com/"),
            ("@maishascafe", "@democafe"),
            ("Matamoros entre No Reelección y Pesqueira #220", "Av. Principal #100, Col. Centro"),
            ("Tu rincón favorito de café en Navojoa", "Tu rincón favorito de café de especialidad"),
        ]:
            s = s.replace(old, new)
        if "Maishas+Cafe" in s:
            s = "https://www.google.com/maps"
        return s

    return {
        "slogan": _clean_maisha(rows.get("hub_slogan"), "Tu rincón favorito de café de especialidad"),
        "wifi_ssid": _clean_maisha(rows.get("hub_wifi_ssid"), "Demo_Cafe_WiFi"),
        "wifi_pass": _clean_maisha(rows.get("hub_wifi_pass"), "democafe2026"),
        "google_review_url": _clean_maisha(rows.get("hub_google_review_url"), "https://www.google.com/maps"),
        "instagram_url": _clean_maisha(rows.get("hub_instagram_url"), "https://www.instagram.com/"),
        "instagram_handle": _clean_maisha(rows.get("hub_instagram_handle"), "@democafe"),
        "address": _clean_maisha(rows.get("hub_address"), "Av. Principal #100, Col. Centro"),
        "phone": (rows.get("hub_phone") or "642 113 8069").strip(),
    }


DEFAULT_BDAY_AUTO_TEMPLATE = "🎂 ¡Feliz cumpleaños, {nombre}! Hoy tienes una bebida o postre de cortesía en Demo Café ☕️✨"


def get_birthday_auto_config():
    """Obtiene la configuración del envío automático de felicitación de cumpleaños a las 8:00 AM (Hora Sonora)."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    rows = dict(c.execute("SELECT key, value FROM settings WHERE key IN ('bday_auto_enabled', 'bday_auto_hour', 'bday_auto_template')").fetchall())
    now_son = datetime.now(SONORA_TZ)
    sent_rows = c.execute(
        "SELECT serial, sent_at_sonora, mode FROM birthday_push_logs WHERE year = ?",
        (now_son.year,)
    ).fetchall()
    conn.close()
    enabled = str(rows.get("bday_auto_enabled", "1")).strip() == "1"
    hour_str = (rows.get("bday_auto_hour") or "08:00").strip()
    template = (rows.get("bday_auto_template") or DEFAULT_BDAY_AUTO_TEMPLATE).strip() or DEFAULT_BDAY_AUTO_TEMPLATE
    template = template.replace("Maisha's Café", "Demo Café").replace("Maishas Café", "Demo Café")
    sent_map = {r[0]: {"sent_at": r[1], "mode": r[2]} for r in sent_rows}
    return {
        "enabled": enabled,
        "hour": hour_str,
        "template": template,
        "sent_map": sent_map,
        "sent_this_year_count": len(sent_map),
    }


def run_auto_birthday_dispatch(force_now=False):
    """
    Revisa si el envío automático de cumpleaños está activo (check activado) y si en Navojoa, Sonora
    ya son las 8:00 AM o más. Envía automáticamente el regalo/felicitación Push a los clientes activos
    que cumplen años HOY y aún no han recibido su felicitación este año.
    """
    try:
        cfg = get_birthday_auto_config()
        if not cfg["enabled"] and not force_now:
            return 0
        now_son = datetime.now(SONORA_TZ)
        if not force_now and now_son.hour < 8:
            return 0

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        rows = c.execute(
            "SELECT serial, name, birthdate, message FROM clients WHERE COALESCE(status, 'active') = 'active' AND birthdate IS NOT NULL AND TRIM(birthdate) != ''"
        ).fetchall()
        already_sent = set(
            r[0] for r in c.execute("SELECT serial FROM birthday_push_logs WHERE year = ?", (now_son.year,)).fetchall()
        )

        to_dispatch = []
        for serial_id, full_name, bdate_str, prev_msg in rows:
            if serial_id in already_sent:
                continue
            binfo = calculate_birthday_info(bdate_str, ref_date=now_son.date())
            if binfo.get("is_today"):
                parts = [p for p in (full_name or "Cliente").strip().split() if p]
                first_name = parts[0] if parts else (full_name or "Cliente")
                msg_text = (
                    cfg["template"]
                    .replace("{nombre}", first_name)
                    .replace("[Nombre]", first_name)
                    .replace("{name}", first_name)
                ).strip()[:200]
                msg_to_save = msg_text + ("\u200b" if prev_msg == msg_text else "")
                now_utc = get_utc_timestamp()
                sonora_dt = get_sonora_datetime_str()
                c.execute(
                    "UPDATE clients SET message = ?, updated_at = ? WHERE serial = ?",
                    (msg_to_save, now_utc, serial_id)
                )
                c.execute(
                    "INSERT OR REPLACE INTO birthday_push_logs (serial, year, sent_at_sonora, mode) VALUES (?, ?, ?, 'auto')",
                    (serial_id, now_son.year, sonora_dt)
                )
                c.execute(
                    "INSERT INTO wallet_logs (created_at, log_text) VALUES (?, ?)",
                    (now_utc, f"🎂 Auto-Cumpleaños 8:00 AM Sonora enviado a {full_name} ({serial_id})")
                )
                to_dispatch.append((serial_id, msg_text))

        conn.commit()
        conn.close()

        for serial_id, msg_text in to_dispatch:
            try:
                send_apns_push_for_serial(serial_id, notification_body=f"📣 {msg_text}")
            except Exception:
                pass
        return len(to_dispatch)
    except Exception as e:
        print(f"⚠️ Error en run_auto_birthday_dispatch: {e}")
        return 0



def get_shop_location():
    """Obtiene la configuración de geolocalización GPS de la cafetería."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    rows = dict(c.execute("SELECT key, value FROM settings").fetchall())
    conn.close()
    try:
        lat = float(rows.get("shop_lat", DEFAULT_LAT))
    except (TypeError, ValueError):
        lat = DEFAULT_LAT
    try:
        lng = float(rows.get("shop_lng", DEFAULT_LNG))
    except (TypeError, ValueError):
        lng = DEFAULT_LNG
    relevant_text = (rows.get("shop_relevant_text") or DEFAULT_GEO_TEXT).replace("Maisha's Café", "Demo Café").replace("Maishas Café", "Demo Café")
    return {"lat": lat, "lng": lng, "relevant_text": relevant_text}


def get_campaign_config():
    """Obtiene la configuración integral de la Tarjeta/Campaña (Marca, Reglas y Ubicación)."""
    loc = get_shop_location()
    has_custom_strip = os.path.isfile(CUSTOM_STRIP_BG_PATH)
    logo_ver = str(int(os.path.getmtime(CUSTOM_LOGO_PATH))) if os.path.isfile(CUSTOM_LOGO_PATH) else "1"
    strip_ver = str(int(os.path.getmtime(CUSTOM_STRIP_BG_PATH))) if has_custom_strip else "1"
    defaults = {
        "shopName": "Demo Café",
        "backgroundColor": "#12100E",
        "foregroundColor": "#F5F2EB",
        "labelColor": "#D4AF37",
        "logoDataUrl": "/api/campaign/logo.png",
        "stripDataUrl": f"/api/campaign/strip-bg.png?v={strip_ver}" if has_custom_strip else "",
        "hasCustomStrip": has_custom_strip,
        "useCustomStrip": has_custom_strip,
        "useCustomStripBg": has_custom_strip,
        "stampGoal": 10,
        "rewardText": "¡1 Café Gratis!",
        "termsText": (
            "1. Consigue 1 sello por cada visita que hagas.\n"
            "2. Acumula 10 sellos para obtener 1 café o bebida gratis.\n"
            "3. La validez de las tarjetas, sellos y premios es ilimitada.\n"
            "4. Los sellos y las recompensas no se pueden cambiar, devolver, reemplazar ni comprar con efectivo.\n"
            "5. Las tarjetas no se pueden transferir ni combinar con otras tarjetas.\n"
            "6. Demo Café se reserva el derecho de rechazar los servicios o modificar el programa."
        ),
        "menuUrl": "https://wa.me/526421138069",
        "lat": loc["lat"],
        "lng": loc["lng"],
        "relevantText": loc["relevant_text"],
        "logoVersion": logo_ver,
        "stripVersion": strip_ver,
    }
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT value FROM settings WHERE key = 'campaign_config_json'").fetchone()
        conn.close()
        if row and row[0]:
            saved = json.loads(row[0])
            if isinstance(saved, dict):
                defaults.update(saved)
    except Exception:
        pass
    for k_str in ("shopName", "termsText", "relevantText"):
        if isinstance(defaults.get(k_str), str):
            defaults[k_str] = (
                defaults[k_str]
                .replace("Maisha's Café", "Demo Café")
                .replace("Maishas Café", "Demo Café")
                .replace("MAISHA'S CAFÉ", "DEMO CAFÉ")
                .replace("MAISHAS CAFÉ", "DEMO CAFÉ")
                .replace("Maisha's", "Demo Café")
                .replace("Maishas", "Demo Café")
            )
    defaults["lat"] = loc["lat"]
    defaults["lng"] = loc["lng"]
    defaults["relevantText"] = loc["relevant_text"]
    defaults["stampGoal"] = max(4, min(12, int(defaults.get("stampGoal") or 10)))
    defaults["logoVersion"] = logo_ver
    defaults["stripVersion"] = strip_ver
    defaults["hasCustomStrip"] = has_custom_strip
    use_strip_flag = bool(defaults.get("useCustomStrip", defaults.get("useCustomStripBg", has_custom_strip)))
    defaults["useCustomStrip"] = use_strip_flag
    defaults["useCustomStripBg"] = use_strip_flag
    return defaults


# ---------------------------------------------------------
# PLANTILLA 1: PORTAL PÚBLICO DE REGISTRO (iOS + ANDROID)
# ---------------------------------------------------------
REGISTRATION_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Demo Café — Club de Fidelización Digital</title>
    <style>
        :root {
            --bg-deep: #0D0C0B;
            --bg-surface: rgba(26, 23, 20, 0.78);
            --gold: #D4AF37;
            --gold-light: #E6C280;
            --text-primary: #F5F2EB;
            --text-muted: #A39B90;
            --border-glass: rgba(255, 255, 255, 0.09);
            --rose: #FF375F;
            --android-green: #3DDC84;
        }
        * { box-sizing: border-box; -webkit-font-smoothing: antialiased; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", "Roboto", sans-serif;
            background-color: var(--bg-deep);
            background-image:
                radial-gradient(circle at 50% 0%, rgba(212, 175, 55, 0.16) 0%, transparent 55%),
                radial-gradient(circle at 85% 90%, rgba(198, 166, 117, 0.08) 0%, transparent 45%);
            color: var(--text-primary);
            min-height: 100vh;
            margin: 0;
            padding: 24px 16px 40px;
            display: flex;
            justify-content: center;
            align-items: center;
        }
        .layout-wrapper {
            width: 100%;
            max-width: 440px;
            display: flex;
            flex-direction: column;
            gap: 18px;
        }
        .brand-header { text-align: center; }
        .brand-pill {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 5px 12px;
            border-radius: 999px;
            background: rgba(212, 175, 55, 0.12);
            border: 1px solid rgba(212, 175, 55, 0.28);
            color: var(--gold-light);
            font-size: 11px;
            font-weight: 600;
            letter-spacing: 0.6px;
            text-transform: uppercase;
            margin-bottom: 8px;
        }
        .brand-header h1 {
            margin: 0;
            font-size: 28px;
            font-weight: 700;
            letter-spacing: -0.5px;
        }
        .brand-header p {
            margin: 5px 0 0;
            font-size: 13.5px;
            color: var(--text-muted);
        }

        .form-card {
            background: var(--bg-surface);
            backdrop-filter: blur(24px);
            -webkit-backdrop-filter: blur(24px);
            border: 1px solid var(--border-glass);
            border-radius: 24px;
            padding: 26px 22px;
            box-shadow: 0 16px 40px rgba(0, 0, 0, 0.45);
        }
        .field-group { margin-bottom: 15px; text-align: left; }
        .field-label {
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 13px;
            font-weight: 600;
            color: #E5E0D8;
            margin-bottom: 6px;
        }
        .optional-tag {
            font-size: 11px;
            font-weight: 500;
            color: var(--text-muted);
            background: rgba(255, 255, 255, 0.06);
            padding: 2px 8px;
            border-radius: 99px;
        }
        input {
            width: 100%;
            padding: 13px 14px;
            border-radius: 12px;
            border: 1px solid rgba(255, 255, 255, 0.12);
            background: rgba(13, 12, 11, 0.65);
            color: var(--text-primary);
            font-size: 16px;
            font-family: inherit;
        }
        input:focus {
            outline: none;
            border-color: var(--gold);
            box-shadow: 0 0 0 3px rgba(212, 175, 55, 0.2);
        }
        input[type="date"] { color-scheme: dark; }
        .bday-incentive {
            margin-top: 6px;
            display: flex;
            align-items: center;
            gap: 6px;
            font-size: 12px;
            color: #FFB3C1;
            background: rgba(255, 55, 95, 0.12);
            border: 1px solid rgba(255, 55, 95, 0.25);
            padding: 7px 11px;
            border-radius: 10px;
        }
        .btn-submit-wallet {
            width: 100%;
            margin-top: 10px;
            padding: 15px 18px;
            border-radius: 14px;
            border: 1px solid rgba(255, 255, 255, 0.18);
            background: linear-gradient(135deg, #D4AF37 0%, #B58E24 100%);
            color: #12100E;
            font-size: 16px;
            font-weight: 700;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
            box-shadow: 0 8px 24px rgba(212, 175, 55, 0.3);
            transition: transform 0.15s ease, box-shadow 0.15s ease;
            user-select: none;
            -webkit-user-select: none;
            -webkit-touch-callout: none;
            touch-action: manipulation;
        }
        .btn-submit-wallet * {
            pointer-events: none;
            user-select: none;
            -webkit-user-select: none;
        }
        .btn-submit-wallet:active {
            transform: scale(0.98);
        }
        .safari-hint {
            text-align: center;
            font-size: 12px;
            color: var(--text-muted);
            margin-top: 12px;
            margin-bottom: 0;
        }
        .demo-top-banner {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            padding: 7px 16px;
            border-radius: 999px;
            background: rgba(56, 189, 248, 0.16);
            border: 1.5px solid rgba(56, 189, 248, 0.55);
            color: #7DD3FC;
            font-size: 12px;
            font-weight: 800;
            letter-spacing: 0.7px;
            text-transform: uppercase;
            margin-bottom: 10px;
            box-shadow: 0 4px 18px rgba(56, 189, 248, 0.2);
        }
    </style>
    <title>[DEMO] {{ campaign.shopName if campaign is defined else 'Demo Café' }} — Club de Fidelización Digital</title>
</head>
<body>
    <div class="layout-wrapper">
        <div class="brand-header">
            <div>
                <span class="demo-top-banner">🧪 SITIO DEMO · PRUEBAS Y DEMOSTRACIÓN</span>
            </div>
            <span class="brand-pill">☕️ {{ campaign.shopName if campaign is defined else 'Demo Café' }} Loyalty Club</span>
            <h1>{{ campaign.shopName if campaign is defined else 'Demo Café' }} <span style="font-size:15px; color:#38BDF8; font-weight:800; vertical-align:middle;">(DEMO)</span></h1>
            <p>Regístrate y acumula {{ campaign.stampGoal if campaign is defined else 10 }} sellos para ganar: {{ campaign.rewardText if campaign is defined else '¡1 Café Gratis!' }}</p>
        </div>

        <!-- Formulario de Registro Limpio con Detección Automática de Dispositivo -->
        <div class="form-card">
            <form action="/api/register" method="POST">
                <input type="hidden" name="platform" id="platform-input" value="">

                <div class="field-group">
                    <label class="field-label"><span>Nombre y Apellido</span></label>
                    <input type="text" name="name" placeholder="Ej. Ana Sofía López" required>
                </div>

                <div class="field-group">
                    <label class="field-label"><span>Correo Electrónico</span></label>
                    <input type="email" name="email" placeholder="tu@correo.com" required>
                </div>

                <div class="field-group">
                    <label class="field-label">
                        <span>Número de Celular</span>
                        <span class="optional-tag">Opcional</span>
                    </label>
                    <input type="tel" name="phone" placeholder="Ej. 667 123 4567">
                </div>

                <div class="field-group">
                    <label class="field-label">
                        <span>🎂 Fecha de Nacimiento</span>
                        <span class="optional-tag">Opcional</span>
                    </label>
                    <input type="date" name="birthdate">
                    <div class="bday-incentive">
                        <span>🎁</span>
                        <span>Recibe un regalo especial en tu tarjeta el día de tu cumpleaños</span>
                    </div>
                </div>

                <button type="submit" id="submit-wallet-btn" class="btn-submit-wallet">
                    <span>📲 Obtener mi Tarjeta Digital</span>
                </button>
            </form>
            <p class="safari-hint">Se instalará automáticamente en tu teléfono al registrarte.</p>
        </div>
    </div>

    <script>
        // Detección 100% automática y silenciosa del tipo de celular (iPhone vs Android)
        (function detectDeviceAutomatically() {
            const ua = navigator.userAgent || navigator.vendor || window.opera || '';
            const platInput = document.getElementById('platform-input');
            const submitBtn = document.getElementById('submit-wallet-btn');
            const formEl = document.querySelector('.form-card form');
            if (/android/i.test(ua)) {
                platInput.value = 'android';
                if (submitBtn) {
                    submitBtn.innerHTML = `
                        <svg width="28" height="24" viewBox="21 12.5 36 30" fill="none" xmlns="http://www.w3.org/2000/svg" style="flex-shrink:0;">
                            <path d="M57 23.791H21V18.1456C21 15.0809 23.6422 12.5001 26.7798 12.5001H51.2202C54.3578 12.5001 57 15.0809 57 18.1456V23.791Z" fill="#34A853"/>
                            <path d="M57 29H21V23C21 19.7429 23.6422 17 26.7798 17H51.2202C54.3578 17 57 19.7429 57 23V29Z" fill="#FBBC04"/>
                            <path d="M57 34H21V28C21 24.7429 23.6422 22 26.7798 22H51.2202C54.3578 22 57 24.7429 57 28V34Z" fill="#EA4335"/>
                            <path d="M21 25.2409L43.8493 30.4025C46.4795 31.0477 49.4384 30.4025 51.5753 28.7895L57 24.9183V37.0157C57 40.0804 54.3699 42.4999 51.2466 42.4999H26.7534C23.6301 42.4999 21 40.0804 21 37.0157V25.2409Z" fill="#4285F4"/>
                        </svg>
                        <span>Obtener Tarjeta en Google Wallet</span>
                    `;
                }
            } else if (/iPad|iPhone|iPod/.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1)) {
                platInput.value = 'ios';
            }
            if (formEl && submitBtn) {
                formEl.addEventListener('submit', function(e) {
                    if (formEl.dataset.submitting === '1') {
                        e.preventDefault();
                        return false;
                    }
                    formEl.dataset.submitting = '1';
                    submitBtn.style.opacity = '0.8';
                    submitBtn.innerHTML = '<span>⏳ Abriendo tu tarjeta...</span>';
                    setTimeout(function() {
                        formEl.dataset.submitting = '0';
                    }, 8000);
                });
            }
        })();
    </script>
</body>
</html>
"""


# ---------------------------------------------------------
# PLANTILLA 2: TARJETA DIGITAL INTERACTIVA ANDROID (PWA + WEB PUSH + LIVE SYNC + GPS)
# ---------------------------------------------------------
ANDROID_CARD_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <meta name="theme-color" content="#12100E">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="mobile-web-app-capable" content="yes">
    <link rel="manifest" href="/card/{{ client.serial }}/manifest.webmanifest">
    <link rel="icon" type="image/png" href="/card/icon-192.png">
    <title>Demo Café — Tarjeta {{ client.name }}</title>
    <style>
        :root {
            --bg-deep: #0D0C0B;
            --card-bg: #12100E;
            --gold: #D4AF37;
            --gold-light: #E6C280;
            --label-gold: #C6A675;
            --text-cream: #F5F2EB;
            --text-muted: #A39B90;
            --apple-green: #30D158;
        }
        * { box-sizing: border-box; -webkit-font-smoothing: antialiased; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Roboto", "SF Pro Display", sans-serif;
            font-variant-numeric: tabular-nums;
            background-color: var(--bg-deep);
            background-image: radial-gradient(circle at 50% 0%, rgba(212, 175, 55, 0.14) 0%, transparent 60%);
            color: var(--text-cream);
            margin: 0;
            padding: 16px 14px 48px;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            align-items: center;
        }

        /* Banner de Notificación Push en Vivo (Estilo Lock Screen / Heads-up) */
        .push-banner {
            position: fixed;
            top: 14px;
            left: 50%;
            transform: translateX(-50%) translateY(-150%);
            z-index: 9999;
            width: 94%;
            max-width: 420px;
            background: rgba(24, 21, 18, 0.96);
            backdrop-filter: blur(24px);
            -webkit-backdrop-filter: blur(24px);
            border: 1px solid rgba(212, 175, 55, 0.55);
            box-shadow: 0 20px 50px rgba(0, 0, 0, 0.85), 0 0 25px rgba(212, 175, 55, 0.25);
            border-radius: 18px;
            padding: 13px 15px;
            display: flex;
            align-items: center;
            gap: 12px;
            transition: transform 0.45s cubic-bezier(0.16, 1, 0.3, 1);
        }
        .push-banner.show {
            transform: translateX(-50%) translateY(0);
        }
        .pb-icon {
            width: 38px;
            height: 38px;
            border-radius: 10px;
            background: linear-gradient(135deg, #D4AF37, #8F6B1A);
            color: #12100E;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 19px;
            flex-shrink: 0;
        }
        .pb-content { flex: 1; min-width: 0; }
        .pb-title {
            font-size: 11px;
            font-weight: 700;
            color: var(--gold-light);
            letter-spacing: 0.5px;
            text-transform: uppercase;
            display: flex;
            justify-content: space-between;
        }
        .pb-body {
            font-size: 13.5px;
            font-weight: 600;
            color: var(--text-cream);
            margin-top: 2px;
            line-height: 1.35;
        }

        /* Barra Superior de Estado Wallet */
        .wallet-top-bar {
            width: 100%;
            max-width: 410px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
            padding: 0 4px;
        }
        .sync-badge {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            font-size: 11.5px;
            font-weight: 600;
            color: #4AE072;
            background: rgba(48, 209, 88, 0.12);
            border: 1px solid rgba(48, 209, 88, 0.3);
            padding: 5px 11px;
            border-radius: 99px;
        }
        .sync-dot {
            width: 7px;
            height: 7px;
            border-radius: 50%;
            background: #30D158;
            box-shadow: 0 0 8px #30D158;
            animation: blinkDot 2s infinite;
        }
        @keyframes blinkDot {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.35; }
        }
        .btn-flip {
            background: rgba(255, 255, 255, 0.08);
            border: 1px solid rgba(255, 255, 255, 0.14);
            color: var(--text-cream);
            border-radius: 99px;
            padding: 6px 13px;
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
        }

        /* Tarjeta Principal Estilo PassKit (Frente y Reverso) */
        .pass-card {
            width: 100%;
            max-width: 410px;
            background: linear-gradient(170deg, #181512 0%, #100E0C 100%);
            border-radius: 24px;
            border: 1px solid rgba(212, 175, 55, 0.38);
            box-shadow: 0 24px 60px rgba(0, 0, 0, 0.8), inset 0 1px 0 rgba(255, 255, 255, 0.12);
            overflow: hidden;
            position: relative;
        }
        .pass-header {
            padding: 16px 20px 14px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .pass-brand {
            display: flex;
            align-items: center;
            gap: 10px;
            font-weight: 800;
            font-size: 14px;
            letter-spacing: 0.8px;
        }
        .pass-brand img {
            width: 30px;
            height: 30px;
            border-radius: 50%;
        }
        .pass-header-field {
            text-align: right;
        }
        .ph-lbl {
            font-size: 10px;
            font-weight: 700;
            color: var(--label-gold);
            letter-spacing: 0.8px;
            display: block;
        }
        .ph-val {
            font-size: 19px;
            font-weight: 800;
            color: var(--text-cream);
        }

        /* Strip Dinámico Generado con Pillow */
        .pass-strip-wrap {
            width: 100%;
            background: #12100E;
            border-top: 1px solid rgba(198, 166, 117, 0.18);
            border-bottom: 1px solid rgba(198, 166, 117, 0.18);
            line-height: 0;
        }
        .pass-strip-wrap img {
            width: 100%;
            height: auto;
            display: block;
        }

        /* Campos Secundarios y Auxiliares */
        .pass-fields-grid {
            padding: 15px 20px 10px;
            display: grid;
            grid-template-columns: 1.25fr 1fr 1.15fr;
            gap: 10px;
        }
        .p-field {
            min-width: 0;
        }
        .p-field .lbl {
            font-size: 9.5px;
            font-weight: 700;
            color: var(--label-gold);
            letter-spacing: 0.6px;
            text-transform: uppercase;
            display: block;
            margin-bottom: 3px;
        }
        .p-field .val {
            font-size: 13px;
            font-weight: 600;
            color: var(--text-cream);
            display: block;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        @media (max-width: 430px) {
            .pass-fields-grid {
                grid-template-columns: 1.25fr 1fr;
                row-gap: 12px;
            }
            .pass-fields-grid .p-field:last-child {
                grid-column: 1 / -1;
                text-align: left !important;
                padding-top: 8px;
                border-top: 1px solid rgba(255, 255, 255, 0.06);
            }
        }
        .pass-promo-row {
            padding: 8px 20px 14px;
            border-bottom: 1px dashed rgba(255, 255, 255, 0.08);
        }
        .pass-promo-row .lbl {
            font-size: 9.5px;
            font-weight: 700;
            color: var(--label-gold);
            letter-spacing: 0.6px;
            text-transform: uppercase;
            display: block;
            margin-bottom: 3px;
        }
        .pass-promo-row .val {
            font-size: 13px;
            color: var(--gold-light);
            font-weight: 500;
            line-height: 1.35;
        }

        /* Zona de Código QR de Alto Contraste para Escáner Mostrador */
        .pass-qr-zone {
            padding: 20px;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
        }
        .qr-white-box {
            background: #FFFFFF;
            padding: 12px;
            border-radius: 16px;
            box-shadow: 0 8px 28px rgba(0, 0, 0, 0.6);
            display: inline-block;
        }
        .qr-white-box img {
            width: 168px;
            height: 168px;
            display: block;
        }
        .qr-alt-text {
            margin-top: 8px;
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            font-size: 12.5px;
            color: var(--text-muted);
            letter-spacing: 1px;
        }

        /* Reverso de la Tarjeta (Detalles del Pase) */
        .pass-back-view {
            display: none;
            padding: 20px;
        }
        .back-section {
            padding: 12px 0;
            border-bottom: 1px solid rgba(255, 255, 255, 0.08);
        }
        .back-section:last-child { border-bottom: none; }
        .back-lbl {
            font-size: 10.5px;
            font-weight: 700;
            color: var(--label-gold);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 4px;
        }
        .back-val {
            font-size: 13.5px;
            color: var(--text-cream);
            line-height: 1.45;
        }

        /* Caja Superior Destacada para Guardar en Billetera Android / Google Wallet */
        .wallet-cta-box {
            width: 100%;
            max-width: 410px;
            background: linear-gradient(160deg, rgba(34, 29, 22, 0.95) 0%, rgba(20, 17, 14, 0.95) 100%);
            border: 1.5px solid rgba(212, 175, 55, 0.55);
            border-radius: 20px;
            padding: 14px 16px;
            margin-bottom: 14px;
            box-shadow: 0 14px 34px rgba(0, 0, 0, 0.65), 0 0 22px rgba(212, 175, 55, 0.14);
            display: flex;
            flex-direction: column;
            gap: 10px;
        }
        .wallet-cta-title {
            font-size: 13px;
            font-weight: 700;
            color: var(--gold-light);
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 8px;
        }
        .wallet-step-hint {
            display: none;
            background: rgba(48, 209, 88, 0.14);
            border: 1px solid rgba(48, 209, 88, 0.4);
            color: #66F08B;
            border-radius: 12px;
            padding: 10px 12px;
            font-size: 12.5px;
            line-height: 1.4;
            font-weight: 600;
        }
        .wallet-sub-links {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
        }
        .btn-mini-intent {
            flex: 1;
            min-width: 140px;
            padding: 9px 11px;
            border-radius: 11px;
            border: 1px solid rgba(255, 255, 255, 0.14);
            background: rgba(255, 255, 255, 0.06);
            color: var(--text-cream);
            font-size: 12px;
            font-weight: 600;
            text-align: center;
            text-decoration: none;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
            user-select: none;
            -webkit-user-select: none;
            -webkit-touch-callout: none;
            touch-action: manipulation;
        }

        /* Botón Oficial Agregar a Google Wallet (Negro Píldora + Ícono Multicolor Oficial) */
        .btn-google-wallet-official {
            width: 100%;
            min-height: 56px;
            padding: 12px 22px;
            border-radius: 999px;
            border: 1.5px solid #747775;
            background: #1F1F1F;
            color: #FFFFFF;
            text-decoration: none;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 14px;
            box-shadow: 0 10px 26px rgba(0, 0, 0, 0.65);
            transition: transform 0.12s ease, background 0.15s ease, border-color 0.15s ease;
            user-select: none;
            -webkit-user-select: none;
            -webkit-touch-callout: none;
            -webkit-user-drag: none;
            touch-action: manipulation;
            -webkit-tap-highlight-color: transparent;
        }
        .btn-google-wallet-official:active {
            transform: scale(0.98);
            background: #2C2C2C;
            border-color: #A8C7FA;
        }
        .btn-google-wallet-official * {
            pointer-events: none;
            user-select: none;
            -webkit-user-select: none;
        }

        /* Botones de Acción Android (Notificaciones Push e Instalar en Inicio) */
        .android-actions-stack {
            width: 100%;
            max-width: 410px;
            margin-top: 14px;
            display: flex;
            flex-direction: column;
            gap: 10px;
        }
        .btn-action-gold {
            width: 100%;
            padding: 15px 16px;
            border-radius: 14px;
            border: 1px solid rgba(255, 255, 255, 0.22);
            background: linear-gradient(135deg, #E2BE52 0%, #B88E24 100%);
            color: #12100E;
            font-size: 15px;
            font-weight: 800;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            text-decoration: none;
            box-shadow: 0 8px 24px rgba(212, 175, 55, 0.3);
            user-select: none;
            -webkit-user-select: none;
            -webkit-touch-callout: none;
            touch-action: manipulation;
        }
        .btn-action-gold * {
            pointer-events: none;
            user-select: none;
            -webkit-user-select: none;
        }
        .btn-action-glass {
            width: 100%;
            padding: 13px 16px;
            border-radius: 14px;
            border: 1px solid rgba(255, 255, 255, 0.12);
            background: rgba(255, 255, 255, 0.05);
            color: var(--text-cream);
            font-size: 13.5px;
            font-weight: 600;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            text-decoration: none;
            user-select: none;
            -webkit-user-select: none;
            -webkit-touch-callout: none;
            touch-action: manipulation;
        }
    </style>
</head>
<body>

    <!-- Banner de Notificación Push en Vivo -->
    <div class="push-banner" id="live-push-banner">
        <div class="pb-icon">☕️</div>
        <div class="pb-content">
            <div class="pb-title">
                <span>DEMO CAFÉ</span>
                <span>ahora</span>
            </div>
            <div class="pb-body" id="live-push-text">¡Tarjeta sincronizada!</div>
        </div>
    </div>

    <!-- Barra Superior -->
    <div class="wallet-top-bar">
        <div style="display:flex; align-items:center; gap:6px; flex-wrap:wrap;">
            <span style="background:rgba(56,189,248,0.18); border:1px solid #38BDF8; color:#7DD3FC; padding:4px 9px; border-radius:99px; font-size:10.5px; font-weight:800; letter-spacing:0.6px;">🧪 DEMO</span>
            <div class="sync-badge">
                <span class="sync-dot"></span>
                <span id="sync-status-label">ID: {{ client.serial }} · Sincronizado</span>
            </div>
        </div>
        <button type="button" class="btn-flip" id="btn-flip-toggle" onclick="toggleBackView()">
            ⓘ Detalles / Reverso
        </button>
    </div>

    <!-- CAJA PRINCIPAL ARRIBA: GUARDAR EN GOOGLE WALLET + ALERTAS EN VIVO -->
    <div class="wallet-cta-box">
        <div class="wallet-cta-title">
            <span>📲 Guardar Tarjeta en tu Celular</span>
            <span style="font-size:11px; color:var(--gold); font-weight:700; font-family:ui-monospace, monospace;">{{ client.serial }}</span>
        </div>

        {% if gw_enabled %}
        <a href="{{ gw_save_url }}"
           id="btn-google-wallet"
           class="btn-google-wallet-official"
           target="_top"
           rel="noopener"
           draggable="false">
            <svg width="36" height="30" viewBox="21 12.5 36 30" fill="none" xmlns="http://www.w3.org/2000/svg" style="flex-shrink:0;">
                <path d="M57 23.791H21V18.1456C21 15.0809 23.6422 12.5001 26.7798 12.5001H51.2202C54.3578 12.5001 57 15.0809 57 18.1456V23.791Z" fill="#34A853"/>
                <path d="M57 29H21V23C21 19.7429 23.6422 17 26.7798 17H51.2202C54.3578 17 57 19.7429 57 23V29Z" fill="#FBBC04"/>
                <path d="M57 34H21V28C21 24.7429 23.6422 22 26.7798 22H51.2202C54.3578 22 57 24.7429 57 28V34Z" fill="#EA4335"/>
                <path d="M21 25.2409L43.8493 30.4025C46.4795 31.0477 49.4384 30.4025 51.5753 28.7895L57 24.9183V37.0157C57 40.0804 54.3699 42.4999 51.2466 42.4999H26.7534C23.6301 42.4999 21 40.0804 21 37.0157V25.2409Z" fill="url(#gw_blue_grad)"/>
                <defs>
                    <linearGradient id="gw_blue_grad" x1="37.2843" y1="34.0448" x2="18.7823" y2="55.7227" gradientUnits="userSpaceOnUse">
                        <stop stop-color="#4285F4"/>
                        <stop offset="1" stop-color="#1B74E8"/>
                    </linearGradient>
                </defs>
            </svg>
            <span style="display:flex; flex-direction:column; align-items:flex-start; line-height:1.15;">
                <span style="font-size:11.5px; font-weight:500; color:#E3E3E3;">Agregar a la</span>
                <span id="gw-btn-main-text" style="font-size:16.5px; font-weight:700; color:#FFFFFF; letter-spacing:0.1px;">Billetera de Google</span>
            </span>
        </a>
        <div style="font-size:11.5px; color:var(--text-muted); text-align:center; line-height:1.4;">
            Tus sellos ({{ client.stamps }}/{{ campaign.stampGoal }}) y avisos se actualizan solos dentro de Google Wallet.
        </div>
        {% else %}
        <div id="push-required-notice" style="background:rgba(212,175,55,0.13); border:1px solid rgba(212,175,55,0.4); border-radius:12px; padding:9px 12px; font-size:12px; color:#F5E3B3; line-height:1.4;">
            🔔 <strong>Importante en Android:</strong> Al tocar el botón dorado selecciona <strong>«Permitir notificaciones»</strong> para que tus sellos y avisos te lleguen al instante.
        </div>
        <a href="/card/{{ client.serial }}/download.pkpass?v={{ client.updated_at | urlencode }}"
           id="btn-save-wallet-top"
           class="btn-action-gold"
           onclick="onSaveToWalletClick()">
            <span id="btn-save-wallet-label">📲 Activar Alertas y Guardar en Wallet ({{ client.stamps }}/{{ campaign.stampGoal }})</span>
        </a>
        <div id="wallet-download-hint" class="wallet-step-hint">
            ✅ <strong>Pase {{ client.serial }} descargado ({{ client.stamps }}/{{ campaign.stampGoal }} sellos):</strong> Toca <strong>«Abrir»</strong> en la descarga para guardarlo en tu <strong>Billetera</strong>, y toca <strong>«Permitir»</strong> si tu celular te pregunta por las notificaciones.
        </div>

        <div class="wallet-sub-links">
            <button type="button" id="btn-enable-push-top" class="btn-mini-intent" onclick="enableAndroidPush(false)" style="border-color:rgba(212,175,55,0.45); color:#E6C280; width:100%;">
                🔔 Activar Notificaciones Push en este Android
            </button>
        </div>
        {% endif %}
    </div>

    <!-- TARJETA DIGITAL INTERACTIVA -->
    <div class="pass-card" style="background: {{ campaign.backgroundColor }}; color: {{ campaign.foregroundColor }};">
        <!-- VISTA FRONTAL -->
        <div id="pass-front-view">
            <div class="pass-header">
                <div class="pass-brand">
                    <img src="/api/campaign/logo.png?v={{ campaign.logoVersion }}" alt="{{ campaign.shopName }}">
                    <span style="color: {{ campaign.foregroundColor }};">{{ campaign.shopName | upper }}</span>
                </div>
                <div class="pass-header-field">
                    <span class="ph-lbl" style="color: {{ campaign.labelColor }};">SELLOS</span>
                    <span class="ph-val" id="val-stamps" style="color: {{ campaign.foregroundColor }};">{{ client.stamps }} / {{ campaign.stampGoal }}</span>
                </div>
            </div>

            <div class="pass-strip-wrap">
                <img id="card-strip-img" src="/card/{{ client.serial }}/strip.png?v={{ client.updated_at }}" alt="Sellos {{ campaign.shopName }}">
            </div>

            <div class="pass-fields-grid">
                <div class="p-field">
                    <span class="lbl" style="color: {{ campaign.labelColor }};">CLIENTE</span>
                    <span class="val" id="val-name" style="color: {{ campaign.foregroundColor }};">{{ client.name }}</span>
                </div>
                <div class="p-field">
                    <span class="lbl" style="color: {{ campaign.labelColor }};">NIVEL VIP</span>
                    <span class="val" id="val-tier" style="color: {{ campaign.foregroundColor }};">{{ client.tier.name }}</span>
                </div>
                <div class="p-field" style="text-align:right;">
                    <span class="lbl" style="color: {{ campaign.labelColor }};">PREMIO</span>
                    <span class="val" id="val-reward" style="color: {{ campaign.foregroundColor }};">{{ client.reward_status }}</span>
                </div>
            </div>

            <div class="pass-promo-row">
                <span class="lbl" style="color: {{ campaign.labelColor }};">AVISO {{ campaign.shopName | upper }}</span>
                <span class="val" id="val-promo">{{ client.message }}</span>
            </div>

            <div class="pass-qr-zone">
                <div class="qr-white-box">
                    <img src="/card/{{ client.serial }}/qr.png" alt="QR {{ client.serial }}">
                </div>
                <div class="qr-alt-text">{{ client.serial }}</div>
            </div>
        </div>

        <!-- VISTA REVERSO (DETALLES DEL PASE ESTILO APPLE WALLET) -->
        <div id="pass-back-view" class="pass-back-view" style="background:#1C1C1E; color:#FFFFFF; font-size:13.5px;">
            <div class="back-section">
                <div style="font-size:13px; font-weight:600; color:#FFFFFF;">Último Mensaje</div>
                <div class="back-val" id="back-val-msg" style="color:#98989F; margin-top:3px;">{{ client.message }}</div>
            </div>
            <div class="back-section">
                <div style="font-size:12.5px; color:#98989F;">Formulario de comentarios</div>
                <div class="back-val" style="margin-top:3px;">
                    <a href="/?serial={{ client.serial }}" style="color:#0A84FF; text-decoration:none; font-weight:500;">⭐ ¿Disfrutaste tu última visita?</a>
                </div>
            </div>
            <div class="back-section" style="display:flex; justify-content:space-between; align-items:center;">
                <span style="color:#FFFFFF;">Total acumulado</span>
                <span id="back-val-total-stamps" style="color:#98989F;">{{ client.total_visits }} {{ 'sello' if client.total_visits == 1 else 'sellos' }}</span>
            </div>
            <div class="back-section" style="display:flex; justify-content:space-between; align-items:center;">
                <span style="color:#FFFFFF;">Para la siguiente recompensa</span>
                <span id="back-val-remaining" style="color:#98989F;">{{ [0, campaign.stampGoal - client.stamps] | max }} {{ 'sello' if ([0, campaign.stampGoal - client.stamps] | max) == 1 else 'sellos' }}</span>
            </div>
            <div class="back-section" style="display:flex; justify-content:space-between; align-items:center;">
                <span style="color:#FFFFFF;">Disponible</span>
                <span id="back-val-available-rewards" style="color:#98989F;">{{ 1 if client.stamps >= campaign.stampGoal else 0 }} {{ 'premio' if client.stamps >= campaign.stampGoal else 'premios' }}</span>
            </div>
            <div class="back-section">
                <div style="color:#FFFFFF;">Cómo conseguir un sello</div>
                <div style="color:#98989F; margin-top:3px;">Un sello = Una visita</div>
            </div>
            <div class="back-section" style="display:flex; justify-content:space-between; align-items:center;">
                <span style="color:#FFFFFF;">Total acumulado</span>
                <span id="back-val-total-rewards" style="color:#98989F;">{{ client.redeemed_count + (1 if client.stamps >= campaign.stampGoal else 0) }} {{ 'premio' if (client.redeemed_count + (1 if client.stamps >= campaign.stampGoal else 0)) == 1 else 'premios' }}</span>
            </div>
            <div class="back-section">
                <div style="font-size:12.5px; color:#98989F;">Descripción del premio</div>
                <div style="color:#98989F; margin-top:3px;">{{ campaign.stampGoal }} sellos = {{ campaign.rewardText }}</div>
            </div>
            <div class="back-section" style="display:flex; justify-content:space-between; align-items:center;">
                <span style="color:#FFFFFF;">La tarjeta es válida hasta</span>
                <span style="color:#98989F;">Ilimitado</span>
            </div>
            <div class="back-section">
                <div style="color:#FFFFFF; margin-bottom:4px;">Términos de Uso</div>
                <div style="color:#98989F; font-size:12.5px; line-height:1.5; white-space:pre-line;">{{ campaign.termsText }}</div>
                <div id="back-val-history" style="display:none;">{{ client.tier.name }}</div>
            </div>
            <div class="back-section">
                <a href="tel:+526421138069" style="color:#0A84FF; text-decoration:none; font-weight:500;">📱 Teléfono</a>
            </div>
            <div class="back-section">
                <a href="https://www.instagram.com/" target="_blank" rel="noopener" style="color:#0A84FF; text-decoration:none; font-weight:500;">Instagram</a>
            </div>
            <div class="back-section" style="display:flex; justify-content:space-between; align-items:center;">
                <span style="color:#FFFFFF;">Nombre de empresa</span>
                <span style="color:#98989F;">{{ campaign.shopName }}</span>
            </div>
            <div class="back-section">
                <div style="font-size:12.5px; color:#98989F;">Información del emisor</div>
                <div style="color:#98989F; margin-top:3px;">{{ campaign.shopName }}</div>
                <div><a href="tel:+526421138069" style="color:#0A84FF; text-decoration:none;">+526421138069</a></div>
            </div>
            <div class="back-section" style="display:flex; justify-content:space-between; align-items:center;">
                <span style="color:#FFFFFF;">Número de serie</span>
                <span style="color:#98989F; font-family:ui-monospace, monospace; font-size:12.5px;">{{ client.serial }}</span>
            </div>
            <div class="back-section" style="display:flex; justify-content:space-between; align-items:center;">
                <span style="color:#FFFFFF;">Actualizado</span>
                <span id="back-val-updated-at" style="color:#98989F; font-size:12.5px;">{{ client.updated_at_display if client.updated_at_display is defined else client.updated_at }}</span>
            </div>
        </div>
    </div>

    <!-- BOTONES SECUNDARIOS DE ACCIÓN -->
    <div class="android-actions-stack">
        <button type="button" id="btn-install-pwa" class="btn-action-glass" style="border-color:rgba(48,209,88,0.45); color:#66F08B;" onclick="installPWA()">
            <span>📲 Instalar Tarjeta Automática en Pantalla de Inicio</span>
        </button>

        {% if not gw_enabled %}
        <button type="button" id="btn-enable-push" class="btn-action-glass" style="border-color:rgba(212,175,55,0.35); color:#E6C280;" onclick="enableAndroidPush(false)">
            <span>🔔 Activar Notificaciones Push en mi Celular</span>
        </button>
        {% endif %}
    </div>

    <script>
        const SERIAL = "{{ client.serial }}";
        const STAMP_GOAL = {{ campaign.stampGoal }};
        const VAPID_PUBLIC_KEY = "{{ vapid_public_key }}";
        const SHOP_LAT = {{ shop_loc.lat }};
        const SHOP_LNG = {{ shop_loc.lng }};
        const SHOP_GEO_TEXT = {{ shop_loc.relevant_text | tojson }};

        let currentUpdatedAt = {{ client.updated_at | tojson }};
        let currentStamps = {{ client.stamps }};
        let currentMessage = {{ client.message | tojson }};
        let currentTier = {{ client.tier.name | tojson }};
        let showingBack = false;

        function onSaveToWalletClick() {
            const hint = document.getElementById('wallet-download-hint');
            if (hint) hint.style.display = 'block';
            showLiveBanner('📲 Pase ' + SERIAL + ' (' + currentStamps + '/' + STAMP_GOAL + ') descargado. Toca "Abrir" arriba para guardarlo en Wallet.');
            try {
                enableAndroidPush(true);
            } catch (e) {}
        }

        function triggerPkpassDownload() {
            onSaveToWalletClick();
            window.location.href = `/card/${SERIAL}/download.pkpass?v=${Date.now()}`;
        }

        function toggleBackView() {
            showingBack = !showingBack;
            document.getElementById('pass-front-view').style.display = showingBack ? 'none' : 'block';
            document.getElementById('pass-back-view').style.display = showingBack ? 'block' : 'none';
            document.getElementById('btn-flip-toggle').textContent = showingBack ? '← Volver al Frente' : 'ⓘ Detalles / Reverso';
        }

        function showLiveBanner(text) {
            const banner = document.getElementById('live-push-banner');
            document.getElementById('live-push-text').textContent = text;
            banner.classList.add('show');
            try {
                if (navigator.vibrate) navigator.vibrate([120, 60, 180]);
            } catch (e) {}
            setTimeout(() => {
                banner.classList.remove('show');
            }, 6500);
        }

        // 1. Registrar latido / instalación activa en Android automáticamente
        async function registerAndroidHeartbeat() {
            try {
                await fetch(`/api/android/heartbeat/${SERIAL}`, { method: 'POST' });
            } catch (e) {}
        }
        registerAndroidHeartbeat();

        // 2. Sincronización en tiempo real cada 3 segundos (actualiza sellos, strip.png y avisos al instante)
        async function pollCardState() {
            try {
                const res = await fetch(`/api/android/state/${SERIAL}?t=${Date.now()}`, { cache: 'no-store' });
                if (!res.ok) return;
                const data = await res.json();
                const goal = data.stamp_goal || STAMP_GOAL;
                if (data.updated_at && data.updated_at !== currentUpdatedAt) {
                    const prevStamps = currentStamps;
                    const prevMsg = currentMessage;
                    const prevTier = currentTier;

                    currentUpdatedAt = data.updated_at;
                    currentStamps = data.stamps;
                    currentMessage = data.message;
                    currentTier = data.tier_name;

                    // Actualizar textos e imagen dinámica strip.png
                    document.getElementById('val-stamps').textContent = `${data.stamps} / ${goal}`;
                    document.getElementById('val-tier').textContent = data.tier_name;
                    document.getElementById('val-reward').textContent = data.reward_status;
                    document.getElementById('val-promo').textContent = data.message;
                    const elBackMsg = document.getElementById('back-val-msg');
                    if (elBackMsg) elBackMsg.textContent = data.message;
                    const elBackRem = document.getElementById('back-val-remaining');
                    if (elBackRem) {
                        const rem = Math.max(0, goal - data.stamps);
                        elBackRem.textContent = `${rem} ${rem === 1 ? 'sello' : 'sellos'}`;
                    }
                    const elBackAvail = document.getElementById('back-val-available-rewards');
                    if (elBackAvail) {
                        const av = data.stamps >= goal ? 1 : 0;
                        elBackAvail.textContent = `${av} ${av === 1 ? 'premio' : 'premios'}`;
                    }
                    document.getElementById('card-strip-img').src = `/card/${SERIAL}/strip.png?v=${encodeURIComponent(data.updated_at)}`;

                    const saveBtn = document.getElementById('btn-save-wallet-top');
                    if (saveBtn) {
                        saveBtn.href = `/card/${SERIAL}/download.pkpass?v=${encodeURIComponent(data.updated_at)}`;
                        const saveLbl = document.getElementById('btn-save-wallet-label');
                        if (saveLbl) {
                            saveLbl.textContent = `🔄 Actualizar mi pase en Billetera (${data.stamps}/${goal} sellos)`;
                        }
                    }

                    // Mostrar alerta estilo pantalla bloqueada según el cambio
                    if (data.stamps !== prevStamps) {
                        if (data.stamps >= goal) {
                            showLiveBanner(`🎉 ¡Felicidades! Completaste ${goal}/${goal} sellos. ¡Tienes tu recompensa disponible!`);
                        } else if (data.stamps === 0) {
                            showLiveBanner(`🎁 ¡Recompensa canjeada! Tu tarjeta se ha reiniciado a 0 / ${goal}.`);
                        } else if (data.stamps < prevStamps) {
                            showLiveBanner(`☕️ Tu tarjeta está al día. Sellos acumulados: ${data.stamps} / ${goal}`);
                        } else {
                            showLiveBanner(`☕️ ¡Has ganado 1 nuevo sello! Total: ${data.stamps} / ${goal}`);
                        }
                    } else if (data.tier_name !== prevTier) {
                        showLiveBanner(`🏆 ¡Subiste de nivel! Ahora eres: ${data.tier_name}`);
                    } else if (data.message !== prevMsg) {
                        showLiveBanner(`📣 ${data.message}`);
                    } else {
                        showLiveBanner(`✨ Tu tarjeta de fidelidad se ha actualizado.`);
                    }
                }
            } catch (e) {}
        }
        setInterval(pollCardState, 3000);
        document.addEventListener('visibilitychange', () => {
            if (!document.hidden) pollCardState();
        });

        // Escuchar mensajes en tiempo real desde el Service Worker cuando llega un Push
        if ('serviceWorker' in navigator) {
            navigator.serviceWorker.addEventListener('message', (event) => {
                if (event.data && event.data.type === 'MAISHAS_PUSH') {
                    pollCardState();
                }
            });
        }

        // 3. Web Push (VAPID) + Service Worker para recibir notificaciones incluso con el navegador cerrado
        function urlBase64ToUint8Array(base64String) {
            const padding = '='.repeat((4 - base64String.length % 4) % 4);
            const base64 = (base64String + padding).replace(/\\-/g, '+').replace(/_/g, '/');
            const rawData = window.atob(base64);
            const outputArray = new Uint8Array(rawData.length);
            for (let i = 0; i < rawData.length; ++i) {
                outputArray[i] = rawData.charCodeAt(i);
            }
            return outputArray;
        }

        function markPushActiveUI() {
            const btn = document.getElementById('btn-enable-push');
            const btnTop = document.getElementById('btn-enable-push-top');
            const notice = document.getElementById('push-required-notice');
            if (btn) {
                btn.innerHTML = '<span>✅ Notificaciones Push Activadas en este Android</span>';
                btn.style.background = 'rgba(48, 209, 88, 0.2)';
                btn.style.color = '#4AE072';
                btn.style.borderColor = 'rgba(48, 209, 88, 0.45)';
            }
            if (btnTop) {
                btnTop.innerHTML = '✅ Alertas Push en Vivo Activadas';
                btnTop.style.background = 'rgba(48, 209, 88, 0.2)';
                btnTop.style.color = '#4AE072';
                btnTop.style.borderColor = 'rgba(48, 209, 88, 0.45)';
            }
            if (notice) {
                notice.style.background = 'rgba(48, 209, 88, 0.14)';
                notice.style.borderColor = 'rgba(48, 209, 88, 0.4)';
                notice.style.color = '#66F08B';
                notice.innerHTML = '✅ <strong>Notificaciones Push Activas:</strong> Recibirás alertas en tu celular cuando te agreguen sellos o promociones. Esta tarjeta digital se actualiza sola en tiempo real.';
            }
        }

        async function enableAndroidPush(silentMode) {
            if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
                if (!silentMode) showLiveBanner('✅ Sincronización automática en vivo activada en tu navegador.');
                return false;
            }
            try {
                const perm = await Notification.requestPermission();
                if (perm !== 'granted') {
                    if (!silentMode) {
                        alert('Para recibir alertas cuando te agreguen sellos o te envíen promociones, permite las notificaciones en tu navegador Chrome.');
                    }
                    return false;
                }
                const reg = await navigator.serviceWorker.register('/sw.js');
                await navigator.serviceWorker.ready;
                const sub = await reg.pushManager.subscribe({
                    userVisibleOnly: true,
                    applicationServerKey: urlBase64ToUint8Array(VAPID_PUBLIC_KEY)
                });
                await fetch(`/api/android/subscribe/${SERIAL}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ subscription: sub.toJSON() })
                });
                markPushActiveUI();
                if (!silentMode) {
                    showLiveBanner('🔔 ¡Notificaciones Push activadas! Recibirás alertas de tus sellos y promociones.');
                }
                return true;
            } catch (err) {
                if (!silentMode) showLiveBanner('✅ Tarjeta vinculada y sincronizada en tiempo real.');
                return false;
            }
        }

        {% if not gw_enabled %}
        // Si ya dio permiso antes, registrar y sincronizar Service Worker automáticamente al abrir
        if ('serviceWorker' in navigator && 'Notification' in window && Notification.permission === 'granted') {
            enableAndroidPush(true);
        }
        {% endif %}

        // Si viene desde el registro o notificación Push, abrir Google Wallet o actualizar automáticamente
        (function checkUrlParams() {
            const params = new URLSearchParams(window.location.search);
            if (params.get('autowallet') === '1') {
                try {
                    window.history.replaceState({}, '', window.location.pathname);
                } catch (e) {}
                const gwBtn = document.getElementById('btn-google-wallet');
                if (gwBtn && gwBtn.href) {
                    setTimeout(() => { window.location.href = gwBtn.href; }, 180);
                }
            } else if (params.get('auto_pkpass') === '1') {
                setTimeout(() => { triggerPkpassDownload(); }, 400);
            } else if (params.get('from_push') === '1') {
                showLiveBanner(`✨ ¡Tu tarjeta está actualizada! Tienes ${currentStamps} / 10 sellos.`);
            }
        })();

        // 4. Instalación PWA ("Agregar a Pantalla de Inicio" en Android)
        let deferredPrompt = null;
        window.addEventListener('beforeinstallprompt', (e) => {
            e.preventDefault();
            deferredPrompt = e;
        });

        async function installPWA() {
            if ('Notification' in window && Notification.permission !== 'granted') {
                await enableAndroidPush(true);
            }
            if (deferredPrompt) {
                deferredPrompt.prompt();
                await deferredPrompt.userChoice;
                deferredPrompt = null;
            } else {
                alert('📲 Para fijar tu Tarjeta Automática en tu inicio de Android: 1) Toca los 3 puntos (⋮) arriba a la derecha en Chrome. 2) Selecciona «Agregar a la pantalla principal» o «Instalar aplicación». ¡Así tus sellos siempre estarán actualizados en vivo!');
            }
        }
    </script>
</body>
</html>
"""


# ---------------------------------------------------------
# PLANTILLA 3: PANEL DEL BARISTA (APPLE HIG POS — iOS + ANDROID)
# ---------------------------------------------------------
BARISTA_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <title>Demo Café — Barista POS</title>
    <meta name="theme-color" content="#F4ECE6">
    <meta name="mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="default">
    <meta name="apple-mobile-web-app-title" content="Barista POS">
    <link rel="manifest" href="/barista/manifest.webmanifest" crossorigin="use-credentials">
    <link rel="apple-touch-icon" href="/barista/icon-512.png">
    <link rel="icon" type="image/png" href="/card/icon-192.png">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Calligraffitti&family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/jsqr@1.4.0/dist/jsQR.min.js"></script>
    <style>
        :root {
            color-scheme: light;
            --bg-canvas: #F4ECE6;
            --bg-elevated: #FAF6F3;
            --bg-card: #EADFD7;
            --bg-glass: #EADFD7;
            --bg-glass-subtle: #FAF6F3;
            --border-subtle: rgba(18, 18, 18, 0.08);
            --border-gold: rgba(74, 93, 35, 0.35);
            --gold: #4A5D23;
            --gold-light: #4E301B;
            --coffee-bean: #4E301B;
            --apple-green: #248A3D;
            --apple-rose: #D93848;
            --apple-amber: #B86200;
            --text-primary: #121212;
            --text-secondary: #6E655F;
            --text-tertiary: #8C827A;
        }
        * { box-sizing: border-box; -webkit-font-smoothing: antialiased; }
        html, body {
            overflow-x: hidden;
            width: 100%;
            max-width: 100vw;
            touch-action: manipulation;
            -webkit-text-size-adjust: 100%;
        }
        body {
            font-family: "Plus Jakarta Sans", -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", sans-serif;
            font-variant-numeric: tabular-nums;
            background-color: var(--bg-canvas);
            background-image:
                radial-gradient(circle at 15% 0%, rgba(234, 223, 215, 0.7) 0%, transparent 45%),
                radial-gradient(circle at 85% 15%, rgba(232, 108, 120, 0.06) 0%, transparent 40%);
            color: var(--text-primary);
            margin: 0;
            padding: 0 0 60px;
            min-height: 100vh;
        }
        input, textarea, select {
            font-size: 16px !important;
            max-width: 100%;
        }

        .dynamic-island-wrap {
            position: fixed;
            top: 14px;
            left: 50%;
            transform: translateX(-50%);
            z-index: 9999;
            width: 92%;
            max-width: 640px;
            pointer-events: none;
        }
        .dynamic-island {
            pointer-events: auto;
            background: #121212;
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border: 1px solid rgba(52, 199, 89, 0.45);
            box-shadow: 0 14px 36px rgba(18, 18, 18, 0.22);
            color: #FAF6F3;
            padding: 12px 18px;
            border-radius: 999px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            font-size: 13.5px;
            font-weight: 600;
            animation: islandDrop 0.4s cubic-bezier(0.16, 1, 0.3, 1);
        }
        @keyframes islandDrop {
            0% { opacity: 0; transform: translateY(-24px) scale(0.92); }
            100% { opacity: 1; transform: translateY(0) scale(1); }
        }
        .di-dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: #34C759;
            box-shadow: 0 0 10px #34C759;
            flex-shrink: 0;
        }
        .di-close {
            background: rgba(255, 255, 255, 0.14);
            border: none;
            color: #EADFD7;
            width: 24px;
            height: 24px;
            border-radius: 50%;
            cursor: pointer;
            font-size: 12px;
            display: flex;
            align-items: center;
            justify-content: center;
            flex-shrink: 0;
        }

        .sticky-header {
            position: sticky;
            top: 0;
            z-index: 100;
            background: rgba(244, 236, 230, 0.88);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border-bottom: 1px solid var(--border-subtle);
            padding: calc(12px + env(safe-area-inset-top, 0px)) 24px 12px;
        }
        .header-inner {
            max-width: 1280px;
            margin: 0 auto;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
            flex-wrap: wrap;
        }
        .brand-Cluster { display: flex; align-items: center; gap: 12px; }
        .brand-emblem {
            width: 40px;
            height: 40px;
            border-radius: 50%;
            background: #121212;
            border: 2.5px solid #4A5D23;
            color: #FAF6F3;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 19px;
            box-shadow: 0 4px 12px rgba(18, 18, 18, 0.12);
        }
        .brand-title { font-size: 18px; font-weight: 800; letter-spacing: -0.02em; margin: 0; color: var(--text-primary); }
        .brand-sub { font-size: 11px; color: var(--gold); font-weight: 700; letter-spacing: 0.4px; text-transform: uppercase; }

        .kpi-cluster { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
        .kpi-pill {
            background: #FAF6F3;
            border: 1px solid var(--border-subtle);
            padding: 6px 12px;
            border-radius: 999px;
            font-size: 12px;
            color: var(--text-secondary);
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }
        .kpi-pill strong { color: var(--text-primary); font-size: 13.5px; font-weight: 800; }
        .kpi-pill.kpi-bday {
            border-color: rgba(232, 108, 120, 0.4);
            background: rgba(232, 108, 120, 0.14);
            color: #B82937;
            cursor: pointer;
        }
        .kpi-pill.kpi-bday strong { color: #D93848; }

        .spotlight-wrap { position: relative; min-width: 0; width: 100%; flex: 1 1 220px; max-width: 340px; }
        .spotlight-input {
            width: 100%;
            padding: 9px 52px 9px 34px;
            border-radius: 12px;
            border: 1.5px solid rgba(18, 18, 18, 0.10);
            background: #FAF6F3;
            color: var(--text-primary);
            font-size: 16px;
            font-family: inherit;
        }
        .spotlight-input::placeholder { color: var(--text-tertiary); }
        .spotlight-input:focus {
            outline: none;
            border-color: #121212;
            background: #FFFFFF;
            box-shadow: 0 0 0 3px rgba(18, 18, 18, 0.08);
        }
        .spotlight-icon {
            position: absolute;
            left: 11px;
            top: 50%;
            transform: translateY(-50%);
            font-size: 13px;
            color: var(--text-secondary);
            pointer-events: none;
        }
        .spotlight-kbd {
            position: absolute;
            right: 10px;
            top: 50%;
            transform: translateY(-50%);
            font-size: 10px;
            font-weight: 700;
            color: var(--text-secondary);
            background: #EADFD7;
            border: 1px solid rgba(18, 18, 18, 0.10);
            border-radius: 5px;
            padding: 2px 6px;
            pointer-events: none;
        }

        .main-container { max-width: 1280px; margin: 22px auto 0; padding: 0 24px; width: 100%; }
        .bento-grid {
            display: grid;
            grid-template-columns: 1fr;
            gap: 18px;
            margin-bottom: 22px;
        }
        .tab-split-grid {
            display: grid;
            grid-template-columns: 1.15fr 0.85fr;
            gap: 18px;
            margin-bottom: 22px;
        }
        .bento-grid > *, .tab-split-grid > * { min-width: 0; max-width: 100%; }
        @media (max-width: 960px) { .tab-split-grid { grid-template-columns: 1fr; } }
        @media (max-width: 760px) {
            .bento-grid { grid-template-columns: 1fr; }
            .sticky-header { padding: calc(12px + env(safe-area-inset-top, 0px)) 16px 12px; }
            .main-container { padding: 0 14px; }
            .spotlight-wrap { max-width: 100%; flex-basis: 100%; }
        }

        .bento-card {
            background: var(--bg-card);
            border: 1px solid var(--border-subtle);
            border-radius: 20px;
            padding: 20px;
            box-shadow: 0 6px 20px rgba(78, 48, 27, 0.04);
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            min-width: 0;
            max-width: 100%;
        }
        .bento-header { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 12px; flex-wrap: wrap; }
        .bento-title { font-size: 15px; font-weight: 800; letter-spacing: -0.02em; margin: 0; display: flex; align-items: center; gap: 8px; color: var(--text-primary); }
        .bento-subtitle { font-size: 12.5px; color: var(--text-secondary); margin: 0 0 12px; line-height: 1.45; }

        .segmented-control {
            display: inline-flex;
            background: #FAF6F3;
            border: 1px solid var(--border-subtle);
            border-radius: 10px;
            padding: 3px;
            gap: 2px;
        }
        .seg-btn {
            background: transparent;
            border: none;
            color: var(--text-secondary);
            font-size: 11.5px;
            font-weight: 700;
            padding: 5px 10px;
            border-radius: 7px;
            cursor: pointer;
            font-family: inherit;
        }
        .seg-btn.active {
            background: #121212;
            color: #FAF6F3;
            box-shadow: 0 2px 6px rgba(18, 18, 18, 0.14);
        }

        .qr-hud-wrapper {
            position: relative;
            background: #FAF6F3;
            border-radius: 16px;
            border: 1.5px dashed rgba(78, 48, 27, 0.22);
            overflow: hidden;
            min-height: 155px;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            margin-bottom: 12px;
        }
        #qr-video { width: 100%; max-height: 220px; object-fit: cover; display: none; }
        .qr-idle-placeholder { padding: 24px 16px; text-align: center; }
        .qr-reticle {
            width: 120px;
            height: 120px;
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            border-radius: 20px;
            box-shadow: 0 0 0 999px rgba(18, 18, 18, 0.35);
            border: 2px solid #4A5D23;
            pointer-events: none;
            display: none;
            animation: reticlePulse 2s infinite ease-in-out;
        }
        @keyframes reticlePulse {
            0%, 100% { transform: translate(-50%, -50%) scale(1); border-color: #4A5D23; }
            50% { transform: translate(-50%, -50%) scale(1.05); border-color: #34C759; }
        }
        .scan-flash {
            position: absolute;
            inset: 0;
            background: rgba(52, 199, 89, 0.35);
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.25s ease;
        }
        .btn-cam-launch {
            background: #121212;
            color: #FAF6F3;
            border: none;
            padding: 11px 20px;
            border-radius: 14px;
            font-size: 13.5px;
            font-weight: 700;
            font-family: inherit;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 8px;
            box-shadow: 0 6px 16px rgba(18, 18, 18, 0.14);
            transition: transform 0.12s ease;
        }
        .btn-cam-launch:active { transform: scale(0.98); }
        .quick-scan-form { display: flex; gap: 8px; }
        .input-dark {
            width: 100%;
            min-width: 0;
            padding: 10px 13px;
            border-radius: 12px;
            border: 1.5px solid rgba(18, 18, 18, 0.10);
            background: #FAF6F3;
            color: var(--text-primary);
            font-size: 16px;
            font-family: inherit;
        }
        .input-dark::placeholder { color: var(--text-tertiary); }
        .input-dark:focus {
            outline: none;
            border-color: #121212;
            background: #FFFFFF;
            box-shadow: 0 0 0 3px rgba(18, 18, 18, 0.07);
        }
        .btn-gold-sm {
            background: #FAF6F3;
            border: 1.5px solid rgba(78, 48, 27, 0.25);
            color: var(--gold-light);
            border-radius: 12px;
            padding: 10px 14px;
            font-size: 13px;
            font-weight: 700;
            font-family: inherit;
            cursor: pointer;
            white-space: nowrap;
            transition: background 0.15s ease;
        }
        .btn-gold-sm:hover { background: #F4ECE6; }

        .bday-card-widget {
            border-color: rgba(232, 108, 120, 0.28);
            background: #EADFD7;
        }
        .bday-list { display: flex; flex-direction: column; gap: 8px; max-height: 215px; overflow-y: auto; padding-right: 2px; }
        .bday-item {
            background: #FAF6F3;
            border: 1px solid var(--border-subtle);
            border-radius: 14px;
            padding: 10px 12px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
        }
        .bday-item.is-today { border-color: rgba(232, 108, 120, 0.5); background: rgba(232, 108, 120, 0.10); }
        .bday-badge-today {
            background: #E86C78;
            color: #FFF;
            font-size: 10.5px;
            font-weight: 800;
            padding: 3px 8px;
            border-radius: 99px;
            display: inline-block;
            animation: pulseRose 1.8s infinite;
        }
        @keyframes pulseRose {
            0%, 100% { box-shadow: 0 0 0 0 rgba(232, 108, 120, 0.55); }
            50% { box-shadow: 0 0 0 6px rgba(232, 108, 120, 0); }
        }
        .bday-badge-soon {
            background: rgba(184, 98, 0, 0.12);
            border: 1px solid rgba(184, 98, 0, 0.28);
            color: #9A5000;
            font-size: 10.5px;
            font-weight: 700;
            padding: 2px 8px;
            border-radius: 99px;
            display: inline-block;
        }
        .btn-bday-gift {
            background: #E86C78;
            color: #FFF;
            border: none;
            border-radius: 10px;
            padding: 7px 11px;
            font-size: 11.5px;
            font-weight: 700;
            font-family: inherit;
            cursor: pointer;
            white-space: nowrap;
        }

        .chip-row { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 8px; }
        .action-chip {
            background: #FAF6F3;
            border: 1px solid var(--border-subtle);
            color: var(--text-secondary);
            border-radius: 999px;
            padding: 5px 11px;
            font-size: 11px;
            font-weight: 700;
            font-family: inherit;
            cursor: pointer;
        }
        .action-chip:hover {
            background: #121212;
            border-color: #121212;
            color: #FAF6F3;
        }
        .ios-lockscreen-preview {
            background: #FAF6F3;
            border: 1px solid rgba(18, 18, 18, 0.10);
            border-radius: 14px;
            padding: 10px 12px;
            margin: 8px 0;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .ios-notif-icon {
            width: 32px;
            height: 32px;
            border-radius: 8px;
            background: #121212;
            border: 1.5px solid #4A5D23;
            color: #FAF6F3;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 15px;
            flex-shrink: 0;
        }
        .ios-notif-body { flex: 1; min-width: 0; }
        .ios-notif-top { display: flex; justify-content: space-between; font-size: 10px; font-weight: 700; color: var(--text-secondary); }
        .ios-notif-text { font-size: 12px; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 1px; font-weight: 600; }

        .clients-section {
            background: var(--bg-card);
            border: 1px solid var(--border-subtle);
            border-radius: 22px;
            padding: 22px;
            box-shadow: 0 6px 20px rgba(78, 48, 27, 0.04);
        }
        .clients-top-bar { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }
        .filter-tabs { display: flex; gap: 6px; flex-wrap: wrap; }
        .filter-pill {
            background: #FAF6F3;
            border: 1px solid var(--border-subtle);
            color: var(--text-secondary);
            padding: 7px 14px;
            border-radius: 999px;
            font-size: 12.5px;
            font-weight: 700;
            font-family: inherit;
            cursor: pointer;
            transition: all 0.15s ease;
        }
        .filter-pill.active {
            background: #121212;
            border-color: #121212;
            color: #FAF6F3;
            font-weight: 700;
        }

        .client-list { display: flex; flex-direction: column; gap: 10px; }
        .client-card-row {
            background: #FAF6F3;
            border: 1px solid var(--border-subtle);
            border-radius: 16px;
            padding: 14px 16px;
            display: grid;
            grid-template-columns: 1.4fr 1.1fr 0.95fr auto;
            align-items: center;
            gap: 16px;
            content-visibility: auto;
            contain-intrinsic-size: auto 130px;
            transition: background 0.15s ease;
        }
        .client-card-row:hover {
            background: #FFFFFF;
        }
        .client-card-row.highlight-row {
            background: rgba(52, 199, 89, 0.12);
            border-color: rgba(36, 138, 61, 0.45);
        }
        @media (max-width: 960px) { .client-card-row { grid-template-columns: 1fr; gap: 12px; } }

        .client-identity { display: flex; align-items: flex-start; gap: 12px; min-width: 0; }
        .avatar-circle {
            width: 42px;
            height: 42px;
            border-radius: 13px;
            background: #EADFD7;
            border: 1.5px solid rgba(78, 48, 27, 0.22);
            color: #4E301B;
            font-weight: 800;
            font-size: 15px;
            display: flex;
            align-items: center;
            justify-content: center;
            flex-shrink: 0;
        }
        .avatar-circle.tier-gold { background: linear-gradient(135deg, #D4AF37, #AA7C11); color: #12100E; border-color: #B38A22; }
        .avatar-circle.tier-silver { background: linear-gradient(135deg, #E0E5EC, #9FAAB8); color: #12100E; border-color: #8A95A5; }
        .client-name-line { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
        .client-name { font-size: 15px; font-weight: 800; color: var(--text-primary); letter-spacing: -0.01em; }
        .serial-tag {
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            font-size: 11px;
            font-weight: 600;
            color: var(--text-secondary);
            background: #EADFD7;
            padding: 2px 7px;
            border-radius: 6px;
            white-space: nowrap;
            display: inline-block;
        }
        .vip-pill { font-size: 10.5px; font-weight: 700; padding: 2px 8px; border-radius: 99px; display: inline-block; white-space: nowrap; }
        .client-meta { font-size: 12px; color: var(--text-secondary); margin-top: 4px; display: flex; flex-wrap: wrap; gap: 10px; }
        .client-msg-preview { font-size: 11.5px; color: var(--gold); margin-top: 4px; font-weight: 600; }

        .stamp-progress-col { display: flex; flex-direction: column; gap: 5px; }
        .stamp-top-row { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
        .stamp-big-num { font-size: 17px; font-weight: 800; color: var(--gold-light); }
        .stamp-visits-meta { font-size: 11.5px; color: var(--text-secondary); font-weight: 500; }
        .stamp-dots-row { display: flex; gap: 5px; align-items: center; }
        .s-dot {
            width: 12px;
            height: 12px;
            border-radius: 50%;
            border: 1.5px solid rgba(78, 48, 27, 0.28);
            background: #EADFD7;
            display: inline-block;
        }
        .s-dot.filled { background: #4E301B; border-color: #4E301B; box-shadow: 0 2px 5px rgba(78, 48, 27, 0.25); }
        .s-dot.full-10 { background: #248A3D; border-color: #248A3D; box-shadow: 0 0 8px rgba(36, 138, 61, 0.45); }

        .telemetry-col { display: flex; flex-direction: column; gap: 4px; align-items: flex-start; }
        .status-pill-on {
            background: rgba(52, 199, 89, 0.15);
            border: 1px solid rgba(36, 138, 61, 0.32);
            color: #1E7A34;
            font-size: 11px;
            font-weight: 700;
            padding: 3px 9px;
            border-radius: 99px;
            white-space: nowrap;
            display: inline-block;
        }
        .status-pill-off {
            background: #EADFD7;
            border: 1px solid rgba(18, 18, 18, 0.10);
            color: var(--text-secondary);
            font-size: 11px;
            font-weight: 600;
            padding: 3px 9px;
            border-radius: 99px;
            white-space: nowrap;
            display: inline-block;
        }
        .tele-line { font-size: 11px; color: var(--text-secondary); }

        .row-actions { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }
        .btn-stamp-plus {
            background: #121212;
            color: #FAF6F3;
            border: none;
            border-radius: 12px;
            padding: 9px 14px;
            font-size: 13px;
            font-weight: 800;
            font-family: inherit;
            cursor: pointer;
            box-shadow: 0 4px 12px rgba(18, 18, 18, 0.15);
            transition: transform 0.12s ease;
        }
        .btn-stamp-plus:active { transform: scale(0.97); }
        .btn-redeem-glow {
            background: #248A3D;
            color: #FFFFFF;
            border: 1px solid #1E7A34;
            border-radius: 12px;
            padding: 9px 14px;
            font-size: 13px;
            font-weight: 800;
            font-family: inherit;
            cursor: pointer;
            animation: redeemPulse 1.6s infinite;
        }
        @keyframes redeemPulse {
            0%, 100% { box-shadow: 0 0 0 0 rgba(36, 138, 61, 0.45); }
            50% { box-shadow: 0 0 0 8px rgba(36, 138, 61, 0); }
        }
        .btn-redeem-normal {
            background: rgba(184, 98, 0, 0.12);
            border: 1px solid rgba(184, 98, 0, 0.30);
            color: #9A5000;
            border-radius: 10px;
            padding: 8px 11px;
            font-size: 12px;
            font-weight: 700;
            font-family: inherit;
            cursor: pointer;
        }
        .btn-icon-sec {
            background: #EADFD7;
            border: 1px solid rgba(18, 18, 18, 0.08);
            color: var(--text-primary);
            border-radius: 10px;
            padding: 8px 10px;
            font-size: 12px;
            font-weight: 700;
            font-family: inherit;
            cursor: pointer;
            text-decoration: none;
            display: inline-flex;
            align-items: center;
            transition: background 0.15s ease;
        }
        .btn-icon-sec:hover { background: #E2D4C9; color: var(--text-primary); }
        .btn-icon-del:hover { background: rgba(217, 56, 72, 0.15); border-color: rgba(217, 56, 72, 0.38); color: #C81D40; }

        /* Navegación Principal Superior (Mostrador POS vs Base de Datos) */
        .main-nav-tabs {
            display: flex;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
        }
        .nav-tab-btn {
            background: #EADFD7;
            border: 1px solid var(--border-subtle);
            color: var(--text-secondary);
            padding: 9px 15px;
            border-radius: 13px;
            font-size: 13px;
            font-weight: 700;
            font-family: inherit;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 7px;
            transition: all 0.18s ease;
            text-decoration: none;
        }
        .nav-tab-btn:hover {
            color: var(--text-primary);
            background: #E2D4C9;
        }
        .nav-tab-btn.active {
            background: #121212;
            border-color: #121212;
            color: #FAF6F3;
            box-shadow: 0 4px 12px rgba(18, 18, 18, 0.14);
        }
        .btn-export-csv {
            background: #121212;
            color: #FAF6F3;
            border: 1px solid #121212;
            padding: 9px 15px;
            border-radius: 12px;
            font-size: 13px;
            font-weight: 700;
            font-family: inherit;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 7px;
            text-decoration: none;
            box-shadow: 0 4px 12px rgba(18, 18, 18, 0.12);
        }

        /* Vista de Tabla Ejecutiva / Base de Datos de Clientes */
        .db-view-section {
            display: none;
        }
        .db-header-card {
            background: var(--bg-card);
            border: 1px solid var(--border-subtle);
            border-radius: 20px;
            padding: 20px 22px;
            margin-bottom: 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 14px;
        }
        .db-table-wrap {
            background: #FAF6F3;
            border: 1px solid var(--border-subtle);
            border-radius: 20px;
            overflow-x: auto;
            overflow-y: auto;
            max-height: 580px;
            -webkit-overflow-scrolling: touch;
            box-shadow: 0 6px 20px rgba(78, 48, 27, 0.05);
        }
        .db-table {
            width: 100%;
            border-collapse: separate;
            border-spacing: 0;
            font-size: 13px;
            text-align: left;
            min-width: 980px;
        }
        .db-table th {
            position: sticky;
            top: 0;
            z-index: 3;
            background: #EADFD7;
            color: var(--gold-light);
            font-size: 11px;
            font-weight: 800;
            text-transform: uppercase;
            letter-spacing: 0.6px;
            padding: 14px 16px;
            border-bottom: 1px solid rgba(18, 18, 18, 0.10);
            white-space: nowrap;
        }
        .db-table td {
            padding: 13px 16px;
            border-bottom: 1px solid rgba(18, 18, 18, 0.06);
            color: var(--text-primary);
            vertical-align: middle;
        }
        .db-table tr:hover td {
            background: rgba(234, 223, 215, 0.55);
        }

        /* Switch / Check accesible para Automatización de Cumpleaños (8:00 AM Sonora) */
        .bday-auto-card {
            background: #EADFD7;
            border: 1px solid rgba(232, 108, 120, 0.32);
            border-radius: 20px;
            padding: 18px 22px;
            margin-bottom: 18px;
            display: flex;
            flex-direction: column;
            gap: 14px;
            box-shadow: 0 6px 20px rgba(78, 48, 27, 0.04);
        }
        .bday-auto-top {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 16px;
            flex-wrap: wrap;
        }
        .bday-check-label {
            display: inline-flex;
            align-items: center;
            gap: 11px;
            cursor: pointer;
            padding: 9px 15px;
            border-radius: 14px;
            background: #FAF6F3;
            border: 1.5px solid rgba(74, 93, 35, 0.32);
            font-size: 13.5px;
            font-weight: 700;
            color: var(--text-primary);
            user-select: none;
            transition: border-color 0.2s ease, background 0.2s ease;
        }
        .bday-check-label:hover {
            border-color: var(--gold);
            background: #FFFFFF;
        }
        input[type="checkbox"][switch] {
            accent-color: var(--gold, #4A5D23);
            width: 20px;
            height: 20px;
            margin: 0;
            cursor: pointer;
            color-scheme: light;
        }
        input[type="checkbox"][switch]:focus-visible {
            outline: 3px solid var(--gold, #4A5D23);
            outline-offset: 4px;
        }

        /* Resumen de Corte de Turno en la Bitácora (3 Contadores del Día en Hora Sonora) */
        .shift-summary-grid {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 14px;
            margin-bottom: 16px;
        }
        .shift-kpi-card {
            background: #FAF6F3;
            border: 1px solid var(--border-subtle);
            border-radius: 18px;
            padding: 16px 18px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            box-shadow: 0 4px 14px rgba(78, 48, 27, 0.04);
        }
        .shift-kpi-label {
            font-size: 12.5px;
            font-weight: 700;
            color: var(--text-primary);
            line-height: 1.35;
        }
        .shift-kpi-sub {
            font-size: 11px;
            color: var(--text-secondary);
            margin-top: 3px;
        }
        .shift-kpi-value {
            font-size: 28px;
            font-weight: 800;
            font-variant-numeric: tabular-nums;
            line-height: 1;
            white-space: nowrap;
        }
        @media (max-width: 780px) {
            .shift-summary-grid {
                grid-template-columns: 1fr;
                gap: 10px;
            }
        }

        /* Modal Personalizado Estilo Candour / Hub (Fondo #FAF6F3) */
        .custom-modal-backdrop {
            position: fixed;
            inset: 0;
            background: rgba(18, 18, 18, 0.42);
            backdrop-filter: blur(5px);
            -webkit-backdrop-filter: blur(5px);
            z-index: 10000;
            display: none;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        .custom-modal-backdrop.open {
            display: flex;
        }
        .custom-modal-box {
            width: 100%;
            max-width: 420px;
            background: #FAF6F3;
            border: 1px solid rgba(18, 18, 18, 0.10);
            border-radius: 26px;
            padding: 26px 24px 22px;
            box-shadow: 0 24px 60px rgba(18, 18, 18, 0.22);
            text-align: center;
            animation: modalPop 0.2s cubic-bezier(0.16, 1, 0.3, 1);
        }
        @keyframes modalPop {
            from { transform: scale(0.94); opacity: 0; }
            to { transform: scale(1); opacity: 1; }
        }
        .cm-icon {
            width: 56px;
            height: 56px;
            border-radius: 18px;
            margin: 0 auto 14px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 26px;
        }
        .cm-icon.danger {
            background: rgba(232, 108, 120, 0.16);
            border: 1px solid rgba(232, 108, 120, 0.4);
        }
        .cm-icon.warn {
            background: #EADFD7;
            border: 1px solid rgba(78, 48, 27, 0.22);
        }
        .cm-icon.success {
            background: rgba(52, 199, 89, 0.16);
            border: 1px solid rgba(36, 138, 61, 0.35);
        }
        .cm-brand {
            font-size: 11px;
            font-weight: 800;
            letter-spacing: 0.7px;
            text-transform: uppercase;
            color: var(--gold);
            margin-bottom: 4px;
        }
        .cm-title {
            font-size: 19px;
            font-weight: 800;
            color: var(--text-primary);
            margin: 0 0 8px;
            letter-spacing: -0.02em;
        }
        .cm-desc {
            font-size: 13.5px;
            color: var(--text-secondary);
            line-height: 1.45;
            margin: 0 0 22px;
        }
        .cm-actions {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 10px;
        }
        .cm-btn-cancel {
            background: #EADFD7;
            border: 1px solid var(--border-subtle);
            color: var(--text-primary);
            padding: 12px 16px;
            border-radius: 14px;
            font-size: 14px;
            font-weight: 700;
            font-family: inherit;
            cursor: pointer;
        }
        .cm-btn-confirm {
            border: none;
            padding: 12px 16px;
            border-radius: 14px;
            font-size: 14px;
            font-weight: 800;
            font-family: inherit;
            cursor: pointer;
        }
        .cm-btn-confirm.danger {
            background: #D93848;
            color: #FFFFFF;
        }
        .cm-btn-confirm.warn {
            background: #121212;
            color: #FAF6F3;
        }
        .cm-btn-confirm.success {
            background: #248A3D;
            color: #FFFFFF;
        }
        .visually-hidden {
            position: absolute;
            clip-path: inset(50%);
            overflow: hidden;
            width: 1px;
            height: 1px;
            margin: -1px;
            padding: 0;
            border: 0;
            white-space: nowrap;
        }
        input[type="checkbox"],
        input[type="radio"],
        input[type="range"] {
            accent-color: var(--gold);
        }
        .input-dark:user-invalid {
            border-color: #D93848;
            box-shadow: 0 0 0 2px rgba(217, 56, 72, 0.16);
        }
        .input-dark:user-valid {
            border-color: rgba(36, 138, 61, 0.45);
        }

        /* Layout Responsivo para Dashboard de Campaña (Desktop 2 columnas vs Celular 1 columna sin empalmes) */
        .campaign-layout-grid {
            display: grid;
            grid-template-columns: minmax(0, 1.35fr) minmax(320px, 0.95fr);
            gap: 20px;
            align-items: start;
        }
        .campaign-editor-card {
            padding: 20px;
            min-width: 0;
        }
        .campaign-preview-card {
            padding: 20px;
            position: sticky;
            top: 92px;
            min-width: 0;
        }
        .campaign-subtabs-bar {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 8px;
            background: #FAF6F3;
            padding: 6px;
            border-radius: 14px;
            border: 1px solid var(--border-subtle);
            margin-bottom: 18px;
        }
        .campaign-colors-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 12px;
            margin-bottom: 18px;
        }
        .campaign-dropzones-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 14px;
        }
        .campaign-gps-grid {
            display: grid;
            grid-template-columns: 1fr 1fr auto;
            gap: 10px;
            margin-bottom: 16px;
        }
        .campaign-gps-btn-col {
            display: flex;
            align-items: flex-end;
        }

        @media (max-width: 960px) {
            .campaign-layout-grid {
                grid-template-columns: minmax(0, 1fr);
                gap: 16px;
            }
            .campaign-preview-card {
                position: static;
                top: auto;
                padding: 16px;
            }
            .campaign-editor-card {
                padding: 16px;
            }
        }

        @media (max-width: 600px) {
            .campaign-subtabs-bar {
                grid-template-columns: 1fr;
                gap: 6px;
            }
            .campaign-colors-grid {
                grid-template-columns: 1fr;
                gap: 10px;
            }
            .campaign-dropzones-grid {
                grid-template-columns: 1fr;
                gap: 12px;
            }
            .campaign-gps-grid {
                grid-template-columns: 1fr 1fr;
            }
            .campaign-gps-btn-col {
                grid-column: 1 / -1;
            }
            .campaign-gps-btn-col button {
                width: 100%;
                justify-content: center;
            }
        }
    </style>
</head>
<body>

    {% if ok %}
    <div class="dynamic-island-wrap" id="status-toast">
        <div class="dynamic-island">
            <span class="di-dot"></span>
            <span style="flex:1;">{{ ok }}</span>
            <button class="di-close" onclick="document.getElementById('status-toast').style.display='none'">✕</button>
        </div>
    </div>
    {% endif %}

    <!-- MODAL DE CONFIRMACIÓN PERSONALIZADO DEMO CAFÉ (Sin alerta del navegador) -->
    <div class="custom-modal-backdrop" id="maishas-confirm-modal">
        <div class="custom-modal-box">
            <div class="cm-icon warn" id="cm-icon-box">⚠️</div>
            <div class="cm-brand">Demo Café · Confirmación</div>
            <h3 class="cm-title" id="cm-title-text">¿Confirmar acción?</h3>
            <p class="cm-desc" id="cm-desc-text"></p>
            <div class="cm-actions">
                <button type="button" class="cm-btn-cancel" onclick="closeConfirmModal()">Cancelar</button>
                <button type="button" class="cm-btn-confirm warn" id="cm-confirm-btn" onclick="executeConfirmedForm()">Confirmar</button>
            </div>
        </div>
    </div>

    <!-- MODAL DE INSTALACIÓN PWA (iPhone / iPad / Android / Tablet) -->
    <div class="custom-modal-backdrop" id="maishas-pwa-modal">
        <div class="custom-modal-box" style="max-width:450px; text-align:left;">
            <div style="text-align:center;">
                <div class="cm-icon warn" style="font-size:26px;">📲</div>
                <div class="cm-brand">Demo Café · App Barista POS</div>
                <h3 class="cm-title">Instalar en tu Teléfono o Tablet</h3>
                <p class="cm-desc" style="margin-bottom:14px;">Agrega la consola a tu pantalla de inicio para abrirla a pantalla completa como aplicación nativa:</p>
            </div>
            <div style="background:#EADFD7; border:1px solid var(--border-subtle); border-radius:14px; padding:12px 14px; margin-bottom:10px; font-size:13px; line-height:1.5; color:var(--text-primary);">
                <strong style="color:var(--gold-light);">🍎 En iPhone o iPad (Safari):</strong><br>
                1. Toca el botón <strong>Compartir</strong> <span style="display:inline-block; padding:1px 6px; border-radius:6px; background:#FAF6F3;">⬆️</span> en la barra de Safari.<br>
                2. Selecciona <strong>«Agregar a pantalla de inicio»</strong> (➕) y toca <strong>Agregar</strong>.
            </div>
            <div style="background:#EADFD7; border:1px solid var(--border-subtle); border-radius:14px; padding:12px 14px; margin-bottom:18px; font-size:13px; line-height:1.5; color:var(--text-primary);">
                <strong style="color:#1E7A34;">🤖 En Android o Tablet (Chrome):</strong><br>
                1. Toca los <strong>3 puntitos (⋮)</strong> arriba a la derecha en Chrome.<br>
                2. Toca <strong>«Instalar aplicación»</strong> o <strong>«Agregar a la pantalla principal»</strong>.
            </div>
            <div class="cm-actions" style="grid-template-columns:1fr;">
                <button type="button" class="cm-btn-confirm warn" onclick="document.getElementById('maishas-pwa-modal').classList.remove('open')">Entendido</button>
            </div>
        </div>
    </div>

    <!-- MODAL DE EDICIÓN DE CLIENTE -->
    <div class="custom-modal-backdrop" id="maishas-edit-modal">
        <div class="custom-modal-box" style="max-width:440px; text-align:left;">
            <div style="text-align:center;">
                <div class="cm-icon warn" style="font-size:22px;">✏️</div>
                <div class="cm-brand">Demo Café · Ficha del Socio</div>
                <h3 class="cm-title">Editar Datos del Cliente</h3>
            </div>
            <form action="/api/update-client" method="POST" style="margin-top:12px;">
                <input type="hidden" name="serial" id="edit-client-serial">
                <div style="margin-bottom:10px;">
                    <label style="font-size:11px; color:var(--gold-light); font-weight:700; display:block; margin-bottom:4px;">Nombre completo</label>
                    <input type="text" name="name" id="edit-client-name" class="input-dark" required maxlength="80">
                </div>
                <div style="margin-bottom:10px;">
                    <label style="font-size:11px; color:var(--gold-light); font-weight:700; display:block; margin-bottom:4px;">Celular (WhatsApp)</label>
                    <input type="tel" name="phone" id="edit-client-phone" class="input-dark" maxlength="25">
                </div>
                <div style="margin-bottom:10px;">
                    <label style="font-size:11px; color:var(--gold-light); font-weight:700; display:block; margin-bottom:4px;">Correo electrónico</label>
                    <input type="email" name="email" id="edit-client-email" class="input-dark" maxlength="100">
                </div>
                <div style="margin-bottom:16px;">
                    <label style="font-size:11px; color:var(--gold-light); font-weight:700; display:block; margin-bottom:4px;">Fecha de nacimiento</label>
                    <input type="date" name="birthdate" id="edit-client-birthdate" class="input-dark" style="color-scheme:light;">
                </div>
                <div class="cm-actions">
                    <button type="button" class="cm-btn-cancel" onclick="closeEditClientModal()">Cancelar</button>
                    <button type="submit" class="cm-btn-confirm warn">💾 Guardar Cambios</button>
                </div>
            </form>
        </div>
    </div>

    <!-- MODAL DE PIN TEMPORAL DE RECUPERACIÓN (4 DÍGITOS · VÁLIDO 10 MIN) -->
    <div class="custom-modal-backdrop" id="maishas-pin-modal">
        <div class="custom-modal-box" style="max-width:410px; text-align:center;">
            <div class="cm-icon warn" style="font-size:24px;">🔑</div>
            <div class="cm-brand">Seguridad Anti-Robo de Sellos</div>
            <h3 class="cm-title">PIN de Recuperación</h3>
            <p class="cm-desc" style="margin-bottom:12px;">
                Comparte este código temporal de <strong>4 dígitos</strong> con <strong id="pin-modal-client-name" style="color:var(--text-primary);">Cliente</strong> (<span id="pin-modal-client-serial" style="font-family:monospace;">DEMO-000000</span>) para que recupere su tarjeta en su celular:
            </p>
            <div style="background:#EADFD7; border:2px dashed rgba(78,48,27,0.35); border-radius:18px; padding:16px 14px; margin-bottom:12px;">
                <div id="pin-modal-digits" style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:38px; font-weight:900; letter-spacing:10px; color:#121212; line-height:1.1;">
                    ••••
                </div>
                <div id="pin-modal-status" style="font-size:11.5px; font-weight:700; color:#4A5D23; margin-top:6px;">
                    ⏳ Válido por 10 minutos · Un solo uso
                </div>
            </div>
            <div style="display:flex; flex-direction:column; gap:8px;">
                <a id="pin-modal-wa-link" href="#" target="_blank" rel="noopener noreferrer"
                   style="display:none; width:100%; padding:12px 14px; border-radius:13px; background:#1E7A34; color:#FFFFFF; font-weight:800; font-size:13.5px; text-decoration:none; align-items:center; justify-content:center; gap:7px;">
                    <span>📲</span>
                    <span>Enviar PIN al WhatsApp registrado</span>
                </a>
                <div class="cm-actions" style="margin-top:4px;">
                    <button type="button" class="cm-btn-cancel" onclick="regenerateRecoveryPin()">🔄 Nuevo PIN</button>
                    <button type="button" class="cm-btn-confirm warn" onclick="closeRecoveryPinModal()">Listo</button>
                </div>
            </div>
        </div>
    </div>

    <!-- MODAL DE OPERACIÓN DE GIFT CARD EN MOSTRADOR POS (DESCONTAR SALDO / CANJEAR COMBO / ACTIVAR / COMPARTIR) -->
    <div class="custom-modal-backdrop" id="maishas-gift-pos-modal">
        <div class="custom-modal-box" style="max-width:460px; text-align:left;">
            <div style="text-align:center;">
                <div class="cm-icon warn" style="font-size:24px;">🎁</div>
                <div class="cm-brand" id="gpos-brand-title">GIFT CARD - {{ campaign.shopName }}</div>
                <h3 class="cm-title" id="gpos-title">Gift Card GIFT-000000</h3>
                <p class="cm-desc" id="gpos-sub" style="margin-bottom:10px;"></p>
            </div>

            <div style="background:#EADFD7; border:1px solid var(--border-subtle); border-radius:16px; padding:12px 14px; margin-bottom:12px;">
                <div style="display:flex; justify-content:space-between; align-items:center; gap:8px;">
                    <div>
                        <div style="font-size:10.5px; font-weight:800; color:var(--text-secondary); text-transform:uppercase;" id="gpos-type-lbl">SALDO DISPONIBLE</div>
                        <div style="font-size:22px; font-weight:900; color:#121212; margin-top:2px;" id="gpos-headline">$0.00 MXN</div>
                    </div>
                    <span id="gpos-status-pill" style="padding:4px 10px; border-radius:99px; font-size:11px; font-weight:800; background:rgba(30,122,52,0.14); color:#1E7A34;">
                        🟢 Activa
                    </span>
                </div>
                <div id="gpos-desc" style="font-size:12px; color:var(--text-secondary); margin-top:4px;"></div>
            </div>

            <!-- Panel A: Si está Pendiente de Activación -->
            <div id="gpos-pane-pending" style="display:none; margin-bottom:12px;">
                <form action="/api/barista/gift-action" method="POST">
                    <input type="hidden" name="code" id="gpos-form-code-activate">
                    <input type="hidden" name="action" value="activate">
                    <button type="submit" class="cm-btn-confirm success" style="width:100%; padding:13px; font-size:14.5px;">
                        🟢 Confirmar Pago y Activar Gift Card
                    </button>
                </form>
            </div>

            <!-- Panel B: Si es de Saldo y está Activa (Descontar monto en MXN) -->
            <div id="gpos-pane-balance" style="display:none; margin-bottom:12px;">
                <form action="/api/barista/gift-action" method="POST">
                    <input type="hidden" name="code" id="gpos-form-code-deduct">
                    <input type="hidden" name="action" value="deduct">
                    <label style="font-size:11.5px; color:var(--gold-light); font-weight:800; display:block; margin-bottom:6px;">
                        💳 Monto a descontar de la Gift Card ($ MXN):
                    </label>
                    <div style="display:flex; gap:6px; flex-wrap:wrap; margin-bottom:8px;">
                        <button type="button" class="action-chip" onclick="setGiftDeductAmount(40)">$40</button>
                        <button type="button" class="action-chip" onclick="setGiftDeductAmount(60)">$60</button>
                        <button type="button" class="action-chip" onclick="setGiftDeductAmount(70)">$70</button>
                        <button type="button" class="action-chip" onclick="setGiftDeductAmount(85)">$85</button>
                        <button type="button" class="action-chip" onclick="setGiftDeductAmount(140)">$140</button>
                        <button type="button" class="action-chip" onclick="setGiftDeductMax()" style="border-color:rgba(74,93,35,0.45); color:#4A5D23; font-weight:800;">Todo el saldo</button>
                    </div>
                    <div style="display:flex; gap:8px;">
                        <input type="number" step="0.5" min="1" name="amount" id="gpos-deduct-input" class="input-dark" placeholder="Ej. 85.00" required style="font-size:16px; font-weight:800;">
                        <button type="submit" class="cm-btn-confirm warn" style="white-space:nowrap; padding:0 18px;">
                            💳 Cobrar Saldo
                        </button>
                    </div>
                </form>
            </div>

            <!-- Panel C: Si es de Combo / Experiencia y está Activa -->
            <div id="gpos-pane-combo" style="display:none; margin-bottom:12px;">
                <form action="/api/barista/gift-action" method="POST">
                    <input type="hidden" name="code" id="gpos-form-code-redeem">
                    <input type="hidden" name="action" value="redeem">
                    <button type="submit" class="cm-btn-confirm success" style="width:100%; padding:13px; font-size:14.5px;">
                        🎉 Entregar Paquete y Marcar como Canjeado
                    </button>
                </form>
            </div>

            <!-- Compartir Gift Card con el Cliente (WhatsApp, Copiar Link y Abrir Gift Card) -->
            <div style="background:#FAF6F3; border:1px solid var(--border-subtle); border-radius:14px; padding:10px 12px; margin-bottom:12px;">
                <div style="font-size:11px; font-weight:800; color:var(--text-secondary); text-transform:uppercase; margin-bottom:8px;">
                    📤 Enviar Gift Card al Cliente
                </div>
                <div style="display:flex; flex-direction:column; gap:8px;">
                    <a id="gpos-wa-share-link" href="#" target="_blank" rel="noopener noreferrer"
                       style="display:flex; align-items:center; justify-content:center; gap:7px; width:100%; padding:11px 14px; border-radius:12px; background:#25D366; color:#FFFFFF; font-weight:800; font-size:13.5px; text-decoration:none; box-shadow:0 4px 12px rgba(37,211,102,0.25);">
                        <span>📲</span>
                        <span>Enviar Gift Card por WhatsApp</span>
                    </a>
                    <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px;">
                        <button type="button" id="gpos-copy-link-btn" data-gift-url="" onclick="copyGiftUrlFromBarista(this)"
                                class="action-chip" style="justify-content:center; padding:10px 12px; font-weight:800; font-size:12.5px; color:#121212; background:#EADFD7; border-color:rgba(18,18,18,0.14);">
                            📋 Copiar Link
                        </button>
                        <a id="gpos-unbox-link" href="#" target="_blank" class="action-chip" style="justify-content:center; padding:10px 12px; font-weight:800; font-size:12.5px; text-decoration:none;">
                            🎁 Abrir Gift Card ↗
                        </a>
                    </div>
                </div>
            </div>

            <div style="display:flex; justify-content:flex-end; align-items:center; gap:8px; padding-top:4px;">
                <button type="button" class="cm-btn-cancel" onclick="closeGiftPosModal()" style="padding:9px 22px; width:100%;">Cerrar</button>
            </div>
        </div>
    </div>

    <header class="sticky-header">
        <div class="header-inner">
            <div class="brand-Cluster">
                <div class="brand-emblem" style="overflow:hidden;">
                    <img src="/api/campaign/logo.png?v={{ campaign.logoVersion }}" alt="Logo" style="width:100%; height:100%; object-fit:cover; border-radius:50%;" onerror="this.style.display='none'">
                </div>
                <div>
                    <h1 class="brand-title" style="display:flex; align-items:center; gap:7px; flex-wrap:wrap;">
                        <span id="hdr-shop-name">{{ campaign.shopName }}</span>
                        <span style="background:rgba(74,93,35,0.14); border:1px solid rgba(74,93,35,0.38); color:#4A5D23; padding:2px 8px; border-radius:99px; font-size:10.5px; font-weight:800; letter-spacing:0.6px;">🧪 DEMO</span>
                    </h1>
                    <span class="brand-sub">Demo Café · Consola Barista (Entorno DEMO)</span>
                </div>
            </div>

            <!-- Pestañas de Vista Principal (Mostrador, Gift Cards, Vales/Combos, Diseño y Campaña, Cumpleañeros, Alertas Push, Base de Datos) -->
            <div class="main-nav-tabs">
                <button type="button" id="nav-btn-pos" class="nav-tab-btn active" onclick="switchMainView('pos')">
                    ☕️ Mostrador POS
                </button>
                {% set pending_gifts_cnt = (gift_cards | selectattr('status', 'equalto', 'pending') | list | length) if gift_cards is defined else 0 %}
                <button type="button" id="nav-btn-gifts" class="nav-tab-btn" onclick="switchMainView('gifts')">
                    🎁 Gift Cards ({{ gift_cards|length if gift_cards is defined else 0 }})
                    {% if pending_gifts_cnt > 0 %}
                        <span style="margin-left:5px; padding:2px 7px; border-radius:99px; background:#F59E0B; color:#12100E; font-size:10.5px; font-weight:900;">
                            ⏳ {{ pending_gifts_cnt }} por activar
                        </span>
                    {% endif %}
                </button>
                <button type="button" id="nav-btn-combos" class="nav-tab-btn" onclick="switchMainView('combos')">
                    🥐 Vales / Combos ({{ gift_combos|length if gift_combos is defined else 3 }})
                </button>
                <button type="button" id="nav-btn-campaign" class="nav-tab-btn" onclick="switchMainView('campaign')">
                    🎨 Diseño y Campaña
                </button>
                <button type="button" id="nav-btn-bday" class="nav-tab-btn" onclick="switchMainView('bday')">
                    🎂 Cumpleañeros ({{ upcoming_birthdays|length }})
                </button>
                <button type="button" id="nav-btn-push" class="nav-tab-btn" onclick="switchMainView('push')">
                    📣 Alertas Push
                </button>
                <button type="button" id="nav-btn-db" class="nav-tab-btn" onclick="switchMainView('db')">
                    📊 Base de Datos ({{ clients|length }})
                </button>
                <button type="button" class="btn-export-csv" onclick="downloadClientsCsv(this)" title="Descargar lista de clientes (.CSV para Excel)">
                    📥 Descargar Clientes
                </button>
                <button type="button" id="btn-install-barista-pwa" class="nav-tab-btn" onclick="installBaristaPwa()">
                    📲 Instalar App
                </button>
            </div>

            <div class="spotlight-wrap">
                <span class="spotlight-icon">🔍</span>
                <input type="text" id="client-search" class="spotlight-input" placeholder="Buscar nombre, código, celular..." oninput="filterClients()">
                <span class="spotlight-kbd">⌘K</span>
            </div>
        </div>
    </header>

    <main class="main-container">
        <!-- VISTA 1: OPERACIÓN EN MOSTRADOR POS (SOLO ESCÁNER QR + LISTA DE CLIENTES) -->
        <div id="view-pos-container">
        <section class="bento-grid">

            <!-- MÓDULO 1: ESCÁNER QR CON HUD ESTILO CÁMARA iOS -->
            <div class="bento-card">
                <div>
                    <div class="bento-header">
                        <h2 class="bento-title">📷 Escáner QR Mostrador</h2>
                        <div class="segmented-control">
                            <button type="button" id="seg-auto-on" class="seg-btn active" onclick="setAutoScanMode(true)">⚡ +1 Auto</button>
                            <button type="button" id="seg-auto-off" class="seg-btn" onclick="setAutoScanMode(false)">🔍 Manual</button>
                        </div>
                    </div>
                    <p class="bento-subtitle">Escanea la tarjeta del cliente (iPhone o Android).</p>

                    <div class="qr-hud-wrapper" id="qr-hud-box">
                        <div class="scan-flash" id="scan-flash-overlay"></div>
                        <video id="qr-video" playsinline muted></video>
                        <div class="qr-reticle" id="qr-reticle-guide"></div>
                        <canvas id="qr-canvas" style="display:none;"></canvas>

                        <div class="qr-idle-placeholder" id="qr-idle-view">
                            <button type="button" id="btn-toggle-cam" class="btn-cam-launch" onclick="toggleScanner()">
                                <span>📷 Activar Cámara QR</span>
                            </button>
                            <div style="font-size:11px; color:var(--text-tertiary); margin-top:8px;">Compatible con iPhone, Android, iPad y Mac</div>
                        </div>
                    </div>
                    <div id="qr-status" style="font-size:12px; color:var(--gold-light); margin-bottom:8px; display:none; text-align:center;"></div>
                </div>

                <form id="scan-form" action="/api/scan-action" method="POST" class="quick-scan-form" onsubmit="return checkDuplicateScanBeforeSubmit(event, this)">
                    <input type="hidden" id="auto-stamp-flag" value="1">
                    <input type="hidden" name="force_duplicate" id="scan-force-dup" value="0">
                    <input type="text" id="scan-serial-input" name="serial" class="input-dark" placeholder="Código de socio (QR)" required>
                    <input type="hidden" name="action" id="scan-action-input" value="add">
                    <button type="submit" class="btn-gold-sm">+1 Sello ☕️</button>
                </form>
            </div>
        </section>

        <!-- LISTA OPERATIVA DE CLIENTES -->
        <section class="clients-section">
            <div class="clients-top-bar">
                <div class="filter-tabs" id="filter-tabs-bar">
                    <button type="button" class="filter-pill active" data-tab="all" onclick="setFilterTab('all')">Todos ({{ clients|length }})</button>
                    <button type="button" class="filter-pill" data-tab="installed" onclick="setFilterTab('installed')">📲 Activos ({{ installed_count }})</button>
                    <button type="button" class="filter-pill" data-tab="ready" onclick="setFilterTab('ready')">🎁 Listos para Canjear ({{ ready_to_redeem_count }})</button>
                    <button type="button" class="filter-pill" data-tab="bday" onclick="setFilterTab('bday')">🎂 Cumpleaños ({{ upcoming_birthdays|length + month_birthdays_count }})</button>
                    <button type="button" class="filter-pill" data-tab="vip" onclick="setFilterTab('vip')">♛ Socios VIP ({{ gold_count + silver_count }})</button>
                </div>
            </div>

            <div class="client-list" id="clients-list-container">
                {% for client in clients %}
                <div id="row-{{ client.serial }}"
                     class="client-card-row client-row {{ 'highlight-row' if updated_serial == client.serial else '' }}"
                     data-search="{{ (client.name ~ ' ' ~ client.serial ~ ' ' ~ client.email ~ ' ' ~ client.phone ~ ' ' ~ client.tier.badge ~ ' ' ~ client.created_at)|lower }}"
                     data-installed="{{ '1' if client.is_installed else '0' }}"
                     data-ready="{{ '1' if client.stamps >= campaign.stampGoal else '0' }}"
                     data-bday="{{ '1' if (client.bday.is_upcoming_week or client.bday.is_this_month) else '0' }}"
                     data-vip="{{ '1' if client.total_visits >= 10 else '0' }}">

                    <div class="client-identity">
                        <div class="avatar-circle tier-{{ client.tier.key }}">{{ client.initials }}</div>
                        <div style="min-width:0;">
                            <div class="client-name-line">
                                <span class="client-name">{{ client.name }}</span>
                                <span class="serial-tag">{{ client.serial }}</span>
                                <span class="vip-pill" style="background:{{ client.tier.bg }}; color:{{ client.tier.fg }}; border:1px solid {{ client.tier.border }};">
                                    {{ client.tier.badge }}
                                </span>
                                {% if client.bday.has_birthdate %}
                                <span class="{{ 'bday-badge-today' if client.bday.is_today else 'bday-badge-soon' }}">
                                    {{ client.bday.badge_text }}
                                </span>
                                {% endif %}
                            </div>
                            <div class="client-meta">
                                {% if client.email %}<span>✉️ {{ client.email }}</span>{% endif %}
                                {% if client.phone %}<span>📱 {{ client.phone }}</span>{% endif %}
                                {% if client.created_at %}<span>🗓️ Alta: {{ client.created_at }}</span>{% endif %}
                            </div>
                            {% if client.message %}
                            <div class="client-msg-preview">💬 "{{ client.message }}"</div>
                            {% endif %}
                        </div>
                    </div>

                    <div class="stamp-progress-col">
                        <div class="stamp-top-row">
                            <span class="stamp-big-num">{{ client.stamps }} / {{ campaign.stampGoal }}</span>
                            <span class="stamp-visits-meta">
                                ☕️ {{ client.total_visits }} visitas
                                {% if client.redeemed_count > 0 %} · 🎁 {{ client.redeemed_count }} canjes{% endif %}
                            </span>
                        </div>
                        <div class="stamp-dots-row">
                            {% for idx in range(campaign.stampGoal) %}
                            <span class="s-dot {{ 'full-10' if client.stamps >= campaign.stampGoal else ('filled' if idx < client.stamps else '') }}"></span>
                            {% endfor %}
                        </div>
                    </div>

                    <div class="telemetry-col">
                        {% if client.ios_installed and client.android_installed %}
                        <span class="status-pill-on">📲 Activo en iPhone y Android</span>
                        {% elif client.ios_installed %}
                        <span class="status-pill-on"> Activo en iPhone</span>
                        {% elif client.android_installed %}
                        <span class="status-pill-on">🤖 Activo en Android</span>
                        {% else %}
                        <span class="status-pill-off">⏳ Aún no instala su tarjeta</span>
                        {% endif %}
                        {% if client.last_push_clean %}
                        <span class="tele-line">{{ client.last_push_clean }}</span>
                        {% endif %}
                        {% if client.android_installed and client.gw_pushes_24h is defined and client.gw_pushes_24h >= 3 %}
                        <span class="tele-line" style="color:#9A5000;" title="Google Wallet muestra máximo 3 alertas emergentes cada 24h; los sellos siguen actualizándose en silencio.">🔕 Cuota Google 3/3 hoy (actualiza en silencio)</span>
                        {% endif %}
                        {% if client.last_sync_clean %}
                        <span class="tele-line" style="color:#4A5D23; font-weight:600;">🔄 Hora local Sonora: {{ client.last_sync_clean }}</span>
                        {% endif %}
                    </div>

                    <div class="row-actions">
                        <form id="form-add-{{ client.serial }}" action="/api/add-stamp" method="POST" style="margin:0;"
                              data-serial="{{ client.serial }}"
                              data-name="{{ client.name|e }}"
                              data-stamps="{{ client.stamps }}"
                              data-last-add-epoch="{{ client.last_add_epoch }}"
                              onsubmit="return checkDuplicateStampBeforeSubmit(event, this)">
                            <input type="hidden" name="serial" value="{{ client.serial }}">
                            <input type="hidden" name="action" value="add">
                            <input type="hidden" name="force_duplicate" value="0">
                            <button type="submit" class="btn-stamp-plus">+1 Sello ☕️</button>
                        </form>

                        {% if client.stamps >= campaign.stampGoal %}
                        <form id="form-redeem-{{ client.serial }}" action="/api/add-stamp" method="POST" style="margin:0;">
                            <input type="hidden" name="serial" value="{{ client.serial }}">
                            <input type="hidden" name="action" value="reset">
                            <button type="button" class="btn-redeem-glow"
                                    data-name="{{ client.name|e }}"
                                    onclick="openConfirmModal('redeem', 'form-redeem-{{ client.serial }}', this.dataset.name, '{{ client.serial }}', {{ client.stamps }})">
                                🎁 Canjear Café
                            </button>
                        </form>
                        {% elif client.stamps > 0 %}
                        <form id="form-redeem-{{ client.serial }}" action="/api/add-stamp" method="POST" style="margin:0;">
                            <input type="hidden" name="serial" value="{{ client.serial }}">
                            <input type="hidden" name="action" value="reset">
                            <button type="button" class="btn-redeem-normal"
                                    data-name="{{ client.name|e }}"
                                    onclick="openConfirmModal('redeem', 'form-redeem-{{ client.serial }}', this.dataset.name, '{{ client.serial }}', {{ client.stamps }})">
                                🎁 Canjear
                            </button>
                        </form>
                        {% endif %}

                        {% if client.stamps > 0 %}
                        <form id="form-sub-{{ client.serial }}" action="/api/add-stamp" method="POST" style="margin:0;">
                            <input type="hidden" name="serial" value="{{ client.serial }}">
                            <input type="hidden" name="action" value="sub">
                            <button type="button" class="btn-icon-sec" title="Restar 1 sello"
                                    data-name="{{ client.name|e }}"
                                    onclick="openConfirmModal('sub', 'form-sub-{{ client.serial }}', this.dataset.name, '{{ client.serial }}', {{ client.stamps }})">
                                -1
                            </button>
                        </form>
                        {% endif %}

                        <button type="button" class="btn-icon-sec" title="Generar PIN temporal de 4 dígitos para recuperar tarjeta de {{ client.name }}"
                                data-serial="{{ client.serial }}"
                                data-name="{{ client.name|e }}"
                                data-phone="{{ client.phone|e }}"
                                onclick="openRecoveryPinModal(this)">🔑</button>
                        <button type="button" class="btn-icon-sec" title="Enviar mensaje directo a {{ client.name }}" data-name="{{ client.name|e }}" onclick="selectClientForPush('{{ client.serial }}', this.dataset.name)">💬</button>
                        <button type="button" class="btn-icon-sec" title="Editar datos de {{ client.name }}"
                                data-serial="{{ client.serial }}"
                                data-name="{{ client.name|e }}"
                                data-phone="{{ client.phone|e }}"
                                data-email="{{ client.email|e }}"
                                data-birthdate="{{ client.birthdate|e }}"
                                onclick="openEditClientModal(this)">✏️</button>

                        <form id="form-del-{{ client.serial }}" action="/api/delete-client" method="POST" style="margin:0;">
                            <input type="hidden" name="serial" value="{{ client.serial }}">
                            <input type="hidden" name="mode" value="deactivate">
                            <button type="button" class="btn-icon-sec btn-icon-del" title="Desactivar cliente (enviar a Papelera)"
                                    data-name="{{ client.name|e }}"
                                    onclick="openConfirmModal('delete', 'form-del-{{ client.serial }}', this.dataset.name, '{{ client.serial }}', {{ client.stamps }})">
                                🗑️
                            </button>
                        </form>
                    </div>
                </div>
                {% else %}
                <div style="text-align:center; padding:40px 20px; color:var(--text-secondary);">
                    No hay clientes registrados todavía.
                </div>
                {% endfor %}
            </div>
        </section>
        </div>

        <!-- VISTA 2: 🎂 PESTAÑA DE CUMPLEAÑEROS -->
        <section id="view-bday-container" class="db-view-section">
            <div class="db-header-card">
                <div>
                    <h2 style="margin:0; font-size:20px; font-weight:800; color:var(--text-primary);">
                        🎂 Radar y Directorio de Cumpleañeros
                    </h2>
                    <p style="margin:4px 0 0; font-size:13px; color:var(--text-secondary);">
                        Consulta quiénes cumplen años hoy, en los próximos 7 días o durante este mes, y envíales su felicitación o regalo directo al celular con 1 clic.
                    </p>
                </div>
                <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
                    <span style="font-size:12.5px; font-weight:700; color:#C81D40; background:rgba(232,108,120,0.16); padding:7px 14px; border-radius:99px; border:1px solid rgba(232,108,120,0.4);">
                        🎂 Hoy y próx. 7 días: {{ upcoming_birthdays|length }} · Este mes: {{ month_birthdays_count }}
                    </span>
                </div>
            </div>

            <!-- TARJETA DE AUTOMATIZACIÓN DE CUMPLEAÑOS A LAS 8:00 AM (HORA SONORA) CON CHECK ACTIVABLE -->
            <div class="bday-auto-card" id="bday-auto-settings-card">
                <div class="bday-auto-top">
                    <div style="min-width:240px; flex:1;">
                        <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-bottom:4px;">
                            <span style="font-size:15.5px; font-weight:800; color:var(--text-primary);">
                                ⏰ Automatización de Cumpleaños (8:00 AM · Hora Sonora)
                            </span>
                            <span id="bday-auto-status-pill" class="{{ 'status-pill-on' if bday_auto.enabled else 'status-pill-off' }}" style="{{ '' if bday_auto.enabled else 'color:#9A5000; border-color:rgba(184,98,0,0.35);' }}">
                                {{ '🟢 Envío Automático Activo (8:00 AM)' if bday_auto.enabled else '⏸️ Automático Desactivado (Solo manual)' }}
                            </span>
                        </div>
                        <p style="margin:0; font-size:12.5px; color:var(--text-secondary); line-height:1.45;">
                            Cuando la casilla está marcada, el sistema envía automáticamente a las <strong>8:00 AM (hora de Sonora)</strong> la notificación de cumpleaños al celular de cada socio activo que cumpla años ese día, sin necesidad de presionar el botón manualmente.
                        </p>
                    </div>

                    <label class="bday-check-label" for="bday-auto-check">
                        <input type="checkbox" switch id="bday-auto-check" {{ 'checked' if bday_auto.enabled else '' }} onchange="toggleBirthdayAutoSetting(this.checked)">
                        <span id="bday-auto-check-text">
                            {{ 'Función automática activada' if bday_auto.enabled else 'Activar función automática' }}
                        </span>
                    </label>
                </div>

                <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap; background:#FAF6F3; padding:11px 14px; border-radius:14px; border:1px solid var(--border-subtle);">
                    <label for="bday-auto-tpl-input" style="font-size:11.5px; font-weight:700; color:var(--gold-light); white-space:nowrap;">
                        💬 Mensaje automático (<code style="color:#C81D40;">{nombre}</code>):
                    </label>
                    <input type="text" id="bday-auto-tpl-input" class="input-dark" maxlength="190" value="{{ bday_auto.template }}" style="flex:1; min-width:220px; padding:8px 12px; font-size:13px; background:#FFFFFF;">
                    <button type="button" class="btn-gold-sm" onclick="saveBirthdayAutoTemplate()" style="padding:8px 14px; font-size:12px;">
                        💾 Guardar Mensaje
                    </button>
                </div>
                <div id="bday-auto-feedback" style="display:none; font-size:12px; font-weight:700; color:#1E7A34;"></div>
            </div>

            <div class="tab-split-grid">
                <!-- Columna 1: Radar Hoy y Próximos 7 días -->
                <div class="bento-card bday-card-widget">
                    <div>
                        <div class="bento-header">
                            <h2 class="bento-title">🎉 Cumpleaños de Hoy y Próximos 7 Días</h2>
                            <span style="font-size:11px; font-weight:700; color:#C81D40; background:rgba(232,108,120,0.16); padding:4px 9px; border-radius:99px;">
                                Prioridad
                            </span>
                        </div>
                        <p class="bday-subtitle bento-subtitle">Envía una felicitación o regalo directo a su celular (Apple Wallet / Google Wallet) con 1 clic.</p>

                        <div class="bday-list" style="max-height:420px;">
                            {% for bclient in upcoming_birthdays %}
                            <div class="bday-item {{ 'is-today' if bclient.bday.is_today else '' }}">
                                <div style="min-width:0;">
                                    <div style="display:flex; align-items:center; gap:6px; flex-wrap:wrap;">
                                        <strong style="font-size:14px; color:var(--text-primary);">{{ bclient.name }}</strong>
                                        <span class="{{ 'bday-badge-today' if bclient.bday.is_today else 'bday-badge-soon' }}">
                                            {{ bclient.bday.badge_text }}
                                        </span>
                                        {% if bclient.serial in bday_auto.sent_map %}
                                        <span class="status-pill-on" style="font-size:10.5px; padding:2px 8px;">
                                            ✅ Felicitado ({{ 'Auto 8AM' if bday_auto.sent_map[bclient.serial].mode == 'auto' else 'Manual' }})
                                        </span>
                                        {% endif %}
                                    </div>
                                    <div style="font-size:11.5px; color:var(--text-secondary); margin-top:3px;">
                                        {{ bclient.tier.badge }} · {{ bclient.stamps }}/10 sellos
                                        {% if bclient.phone %} · 📱 {{ bclient.phone }}{% endif %}
                                    </div>
                                </div>
                                <div style="display:flex; gap:6px; align-items:center;">
                                    <form action="/api/send-message" method="POST" style="margin:0;">
                                        <input type="hidden" name="target" value="{{ bclient.serial }}">
                                        <input type="hidden" name="is_bday" value="1">
                                        <input type="hidden" name="message" value="🎂 ¡Feliz cumpleaños, {{ bclient.first_name }}! Hoy tienes una bebida o postre de cortesía en Demo Café ☕️✨">
                                        <button type="submit" class="btn-bday-gift">
                                            🎁 Enviar Regalo Push
                                        </button>
                                    </form>
                                    <button type="button" class="btn-icon-sec" title="Escribir mensaje personalizado" data-name="{{ bclient.name|e }}" onclick="selectClientForPush('{{ bclient.serial }}', this.dataset.name)">💬</button>
                                </div>
                            </div>
                            {% else %}
                            <div style="text-align:center; padding:34px 16px; color:var(--text-secondary); font-size:13px; background:#FAF6F3; border-radius:14px; border:1px solid var(--border-subtle);">
                                <div style="font-size:26px; margin-bottom:8px;">🗓️</div>
                                No hay cumpleaños registrados para hoy ni en los próximos 7 días.
                            </div>
                            {% endfor %}
                        </div>
                    </div>
                </div>

                <!-- Columna 2: Todos los clientes con fecha de cumpleaños registrada -->
                <div class="bento-card">
                    <div>
                        <div class="bento-header">
                            <h2 class="bento-title">📅 Todos los Cumpleaños Registrados</h2>
                            <span style="font-size:11px; color:var(--gold-light);">Del mes: <strong>{{ month_birthdays_count }}</strong></span>
                        </div>
                        <p class="bento-subtitle">Lista completa de clientes con fecha de nacimiento registrada.</p>

                        <div class="bday-list" style="max-height:420px;">
                            {% set ns = namespace(has_any_bday=false) %}
                            {% for client in clients %}
                                {% if client.bday.has_birthdate %}
                                {% set ns.has_any_bday = true %}
                                <div class="bday-item {{ 'is-today' if client.bday.is_today else '' }}">
                                    <div style="min-width:0;">
                                        <div style="display:flex; align-items:center; gap:6px; flex-wrap:wrap;">
                                            <strong style="font-size:13.5px; color:var(--text-primary);">{{ client.name }}</strong>
                                            <span class="{{ 'bday-badge-today' if client.bday.is_today else 'bday-badge-soon' }}">
                                                {{ client.bday.badge_text }}
                                            </span>
                                            {% if client.serial in bday_auto.sent_map %}
                                            <span class="status-pill-on" style="font-size:10.5px; padding:2px 8px;">
                                                ✅ Felicitado
                                            </span>
                                            {% endif %}
                                        </div>
                                        <div style="font-size:11px; color:var(--text-secondary); margin-top:2px;">
                                            {{ client.tier.badge }} · {{ client.stamps }}/10 sellos · {{ client.serial }}
                                        </div>
                                    </div>
                                    <div style="display:flex; gap:6px; align-items:center;">
                                        <form action="/api/send-message" method="POST" style="margin:0;">
                                            <input type="hidden" name="target" value="{{ client.serial }}">
                                            <input type="hidden" name="is_bday" value="1">
                                            <input type="hidden" name="message" value="🎂 ¡Feliz cumpleaños, {{ client.first_name }}! Hoy tienes una bebida o postre de cortesía en Demo Café ☕️✨">
                                            <button type="submit" class="btn-bday-gift">
                                                🎁 Felicitación
                                            </button>
                                        </form>
                                        <button type="button" class="btn-icon-sec" title="Escribir mensaje personalizado" data-name="{{ client.name|e }}" onclick="selectClientForPush('{{ client.serial }}', this.dataset.name)">💬</button>
                                    </div>
                                </div>
                                {% endif %}
                            {% endfor %}
                            {% if not ns.has_any_bday %}
                            <div style="text-align:center; padding:34px 16px; color:var(--text-secondary); font-size:13px; background:#FAF6F3; border-radius:14px; border:1px solid var(--border-subtle);">
                                Aún no hay clientes con fecha de cumpleaños capturada.
                            </div>
                            {% endif %}
                        </div>
                    </div>
                </div>
            </div>
        </section>

        <!-- VISTA 3: 📣 PESTAÑA DE ALERTAS PUSH & GEOLOCALIZACIÓN GPS -->
        <section id="view-push-container" class="db-view-section">
            <div class="db-header-card">
                <div>
                    <h2 style="margin:0; font-size:20px; font-weight:800; color:var(--text-primary);">
                        📣 Centro de Alertas Push y Geovalla GPS
                    </h2>
                    <p style="margin:4px 0 0; font-size:13px; color:var(--text-secondary);">
                        Envía promociones o avisos en tiempo real a todos los teléfonos activos ({{ installed_count }}) o a un cliente específico en Apple Wallet y Google Wallet.
                    </p>
                </div>
            </div>

            <div class="tab-split-grid">
                <!-- Columna 1: Envío de Alertas Push -->
                <div class="bento-card">
                    <div>
                        <div class="bento-header">
                            <h2 class="bento-title" id="mkt-card-title">📣 Enviar Alerta Push</h2>
                            <span style="font-size:11px; font-weight:700; color:#4A5D23; background:rgba(74,93,35,0.12); padding:4px 10px; border-radius:99px;">
                                📲 {{ installed_count }} teléfonos activos
                            </span>
                        </div>
                        <p class="bento-subtitle">Selecciona una plantilla rápida o escribe tu mensaje personalizado:</p>

                        <div id="mkt-pane-push">
                            <div class="chip-row" style="margin-bottom:12px;">
                                <button type="button" class="action-chip" onclick="setTemplate('🥐 ¡Recién salidos del horno! Ven por tu pan calientito y suma tu sello de hoy ☕️')">🥐 Pan horneado</button>
                                <button type="button" class="action-chip" onclick="setTemplate('✨ ¡Hora Feliz en Demo Café! 2x1 en bebidas seleccionadas hoy de 4 a 6 pm ☕️')">✨ Hora Feliz 2x1</button>
                                <button type="button" class="action-chip" onclick="setTemplate('🔥 ¡Hoy tus compras valen DOBLE sello en Demo Café! Te esperamos ☕️')">🔥 Doble Sello</button>
                            </div>

                            <form action="/api/send-message" method="POST">
                                <textarea name="message" id="push-msg-box" class="input-dark" maxlength="200" rows="3" placeholder="Escribe una promoción o aviso para tus clientes..." required oninput="updateLockScreenPreview(this.value)"></textarea>

                                <div class="ios-lockscreen-preview" style="margin:12px 0;">
                                    <div class="ios-notif-icon">☕️</div>
                                    <div class="ios-notif-body">
                                        <div class="ios-notif-top">
                                            <span>DEMO CAFÉ</span>
                                            <span>ahora</span>
                                        </div>
                                        <div class="ios-notif-text" id="ios-preview-text">📣 Escribe un mensaje para previsualizar...</div>
                                    </div>
                                </div>

                                <div style="display:flex; gap:10px; margin-top:10px; flex-wrap:wrap;">
                                    <select name="target" id="push-target-select" class="input-dark" style="flex:1; min-width:160px; max-width:100%;">
                                        <option value="all">📲 Todos ({{ installed_count }} teléfonos activos)</option>
                                        {% for client in clients %}
                                        <option value="{{ client.serial }}">{{ client.name }} ({{ client.serial }})</option>
                                        {% endfor %}
                                    </select>
                                    <button type="submit" class="btn-cam-launch" style="white-space:nowrap;">Enviar Push 📲</button>
                                </div>
                            </form>
                        </div>
                    </div>
                </div>

                <!-- Columna 2: Geolocalización GPS (~150m) -->
                <div class="bento-card">
                    <div>
                        <div class="bento-header">
                            <h2 class="bento-title">📍 Geovalla GPS ({{ "%.4f"|format(shop_loc.lat) }}, {{ "%.4f"|format(shop_loc.lng) }})</h2>
                        </div>
                        <p class="bento-subtitle">Alerta automática en pantalla de bloqueo cuando el cliente pasa a ~150m de Demo Café.</p>

                        <div id="mkt-pane-gps">
                            <form action="/api/update-location" method="POST">
                                <div style="display:grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) auto; gap:8px; margin-bottom:10px;">
                                    <div style="min-width:0;">
                                        <label style="font-size:10.5px; color:var(--gold-light); font-weight:700;">Latitud</label>
                                        <input type="text" id="lat-input" name="lat" class="input-dark" value="{{ shop_loc.lat }}" required>
                                    </div>
                                    <div style="min-width:0;">
                                        <label style="font-size:10.5px; color:var(--gold-light); font-weight:700;">Longitud</label>
                                        <input type="text" id="lng-input" name="lng" class="input-dark" value="{{ shop_loc.lng }}" required>
                                    </div>
                                    <div style="display:flex; align-items:flex-end;">
                                        <button type="button" class="btn-gold-sm" onclick="detectMyGPS()">📍 Mi GPS</button>
                                    </div>
                                </div>
                                <div style="margin-bottom:14px;">
                                    <label style="font-size:10.5px; color:var(--gold-light); font-weight:700;">Mensaje al pasar cerca:</label>
                                    <input type="text" name="relevant_text" class="input-dark" value="{{ shop_loc.relevant_text }}" maxlength="160" required>
                                </div>
                                <button type="submit" class="btn-cam-launch" style="width:100%; justify-content:center;">
                                    💾 Guardar GPS y Sincronizar Pases
                                </button>
                            </form>
                        </div>
                    </div>
                </div>
            </div>
        </section>

        <!-- VISTA 4: BASE DE DATOS EJECUTIVA Y DESCARGA DE MACHOTE EXCEL (.CSV) -->
        <section id="view-db-container" class="db-view-section">
            <div class="db-header-card">
                <div>
                    <h2 style="margin:0; font-size:20px; font-weight:800; color:var(--text-primary);">
                        📊 Base de Datos General de Clientes
                    </h2>
                    <p style="margin:4px 0 0; font-size:13px; color:var(--text-secondary);">
                        Consulta todos los datos capturados, fecha de alta en hora local de Navojoa, Sonora, historial de visitas y exporta tu machote para Excel.
                    </p>
                </div>
                <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
                    <a href="#buzon-sugerencias-section" class="action-chip" style="text-decoration:none; border-color:rgba(78,48,27,0.35); color:var(--gold-light); padding:8px 13px; font-size:12.5px;">
                        💬 Buzón de Sugerencias ({{ (feedback_list or [])|length }})
                    </a>
                    <button type="button" class="btn-export-csv" onclick="downloadClientsCsv(this)">
                        📥 Descargar Clientes (.CSV)
                    </button>
                </div>
            </div>

            <div class="db-table-wrap" tabindex="0" role="region" aria-label="Directorio de clientes activos">
                <table class="db-table">
                    <thead>
                        <tr>
                            <th>Código Serial</th>
                            <th>Nombre del Cliente</th>
                            <th>Celular</th>
                            <th>Correo Electrónico</th>
                            <th>Fecha Nacimiento</th>
                            <th>Fecha de Alta (Sonora)</th>
                            <th>Teléfono / Estado</th>
                            <th>Sellos</th>
                            <th>Visitas Totales</th>
                            <th>Cafés Canjeados</th>
                            <th>Nivel VIP</th>
                            <th>Última Actividad</th>
                            <th>Acciones</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for client in clients %}
                        <tr class="db-client-row"
                            data-search="{{ (client.name ~ ' ' ~ client.serial ~ ' ' ~ client.email ~ ' ' ~ client.phone ~ ' ' ~ client.tier.badge ~ ' ' ~ client.created_at)|lower }}">
                            <td><span class="serial-tag">{{ client.serial }}</span></td>
                            <td><strong>{{ client.name }}</strong></td>
                            <td>{{ client.phone if client.phone else '—' }}</td>
                            <td>{{ client.email if client.email else '—' }}</td>
                            <td>
                                {% if client.birthdate %}
                                    {{ client.birthdate }} ({{ client.bday.short_date }})
                                {% else %}
                                    —
                                {% endif %}
                            </td>
                            <td>{{ client.created_at if client.created_at else '—' }}</td>
                            <td>
                                {% if client.ios_installed and client.android_installed %}
                                    <span class="status-pill-on"> iPhone + 🤖 Android</span>
                                {% elif client.ios_installed %}
                                    <span class="status-pill-on"> iPhone</span>
                                {% elif client.android_installed %}
                                    <span class="status-pill-on">🤖 Android</span>
                                {% else %}
                                    <span class="status-pill-off">⏳ Pendiente</span>
                                {% endif %}
                            </td>
                            <td><strong style="color:var(--gold-light);">{{ client.stamps }} / {{ campaign.stampGoal }}</strong></td>
                            <td>{{ client.total_visits }}</td>
                            <td>{{ client.redeemed_count }}</td>
                            <td>
                                <span class="vip-pill" style="background:{{ client.tier.bg }}; color:{{ client.tier.fg }}; border:1px solid {{ client.tier.border }};">
                                    {{ client.tier.badge }}
                                </span>
                            </td>
                            <td style="font-size:12px; color:var(--text-secondary);">
                                {{ client.last_sync_clean if client.last_sync_clean else (client.last_push_clean if client.last_push_clean else '—') }}
                            </td>
                            <td>
                                <div style="display:inline-flex; align-items:center; gap:6px;">
                                    <button type="button" class="btn-icon-sec" title="Generar PIN de 4 dígitos para recuperar tarjeta de {{ client.name }}"
                                            data-serial="{{ client.serial }}"
                                            data-name="{{ client.name|e }}"
                                            data-phone="{{ client.phone|e }}"
                                            onclick="openRecoveryPinModal(this)">🔑</button>
                                    <button type="button" class="btn-icon-sec" title="Editar datos de {{ client.name }}"
                                            data-serial="{{ client.serial }}"
                                            data-name="{{ client.name|e }}"
                                            data-phone="{{ client.phone|e }}"
                                            data-email="{{ client.email|e }}"
                                            data-birthdate="{{ client.birthdate|e }}"
                                            onclick="openEditClientModal(this)">✏️</button>
                                    <form id="form-db-del-{{ client.serial }}" action="/api/delete-client" method="POST" style="margin:0;">
                                        <input type="hidden" name="serial" value="{{ client.serial }}">
                                        <input type="hidden" name="mode" value="deactivate">
                                        <button type="button" class="btn-icon-sec btn-del-subtle" title="Desactivar y enviar a Papelera a {{ client.name }}"
                                                onclick="openConfirmModal('delete', 'form-db-del-{{ client.serial }}', {{ client.name|tojson|forceescape }}, '{{ client.serial }}', {{ client.stamps }})">🗑️</button>
                                    </form>
                                </div>
                            </td>
                        </tr>
                        {% else %}
                        <tr>
                            <td colspan="13" style="text-align:center; padding:32px; color:var(--text-secondary);">
                                No hay clientes activos registrados todavía.
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>

            <!-- PAPELERA / CLIENTES DESACTIVADOS (SOFT-DELETE CON REACTIVACIÓN Y OCULTAMIENTO DEFINITIVO) -->
            <div class="db-header-card" style="margin-top:22px; border-color:rgba(184,98,0,0.28);">
                <div>
                    <div style="display:inline-flex; align-items:center; gap:6px; background:rgba(184,98,0,0.12); border:1px solid rgba(184,98,0,0.32); color:#9A5000; padding:3px 10px; border-radius:99px; font-size:11px; font-weight:800; margin-bottom:6px;">
                        📦 PAPELERA DE SEGURIDAD · CLIENTES DESACTIVADOS
                    </div>
                    <h2 style="margin:0; font-size:18px; font-weight:800; color:var(--text-primary);">
                        Clientes Desactivados / Papelera ({{ (inactive_clients or [])|length }})
                    </h2>
                    <p style="margin:4px 0 0; font-size:12.5px; color:var(--text-secondary);">
                        Los clientes desactivados no aparecen en el Mostrador POS ni reciben notificaciones Push, pero conservan sus sellos e historial. Puedes <strong>♻️ Reactivarlos</strong> con un clic o <strong>🗑️ Eliminarlos Definitivamente</strong> para que ya no estén disponibles en ninguna parte del sistema.
                    </p>
                </div>
            </div>

            <div class="db-table-wrap" tabindex="0" role="region" aria-label="Clientes desactivados en papelera">
                <table class="db-table">
                    <thead>
                        <tr>
                            <th>Código Serial</th>
                            <th>Nombre del Cliente</th>
                            <th>Celular</th>
                            <th>Correo Electrónico</th>
                            <th>Sellos Guardados</th>
                            <th>Visitas / Canjes</th>
                            <th>Nivel VIP</th>
                            <th>Fecha Desactivación (Sonora)</th>
                            <th>Acciones de Papelera</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for ic in inactive_clients or [] %}
                        <tr>
                            <td><span class="serial-tag" style="opacity:0.85;">{{ ic.serial }}</span></td>
                            <td>
                                <strong>{{ ic.name }}</strong>
                                <span class="status-pill-off" style="margin-left:6px; color:#9A5000; border-color:rgba(184,98,0,0.32); background:rgba(184,98,0,0.10);">Desactivado</span>
                            </td>
                            <td>{{ ic.phone if ic.phone else '—' }}</td>
                            <td>{{ ic.email if ic.email else '—' }}</td>
                            <td><strong style="color:var(--gold-light);">{{ ic.stamps }} / {{ campaign.stampGoal }}</strong></td>
                            <td>{{ ic.total_visits }} visitas · {{ ic.redeemed_count }} canjes</td>
                            <td>
                                <span class="vip-pill" style="background:{{ ic.tier.bg }}; color:{{ ic.tier.fg }}; border:1px solid {{ ic.tier.border }};">
                                    {{ ic.tier.badge }}
                                </span>
                            </td>
                            <td style="font-size:12px; color:var(--text-secondary);">
                                {{ ic.deactivated_at if ic.deactivated_at else '—' }}
                            </td>
                            <td>
                                <div style="display:inline-flex; align-items:center; gap:8px; flex-wrap:wrap;">
                                    <form id="form-react-{{ ic.serial }}" action="/api/reactivate-client" method="POST" style="margin:0;">
                                        <input type="hidden" name="serial" value="{{ ic.serial }}">
                                        <button type="button" class="btn-export-csv" style="padding:6px 12px; font-size:11.5px; background:rgba(52,199,89,0.16); border-color:rgba(36,138,61,0.38); color:#1E7A34; box-shadow:none;"
                                                onclick="openConfirmModal('reactivate', 'form-react-{{ ic.serial }}', {{ ic.name|tojson|forceescape }}, '{{ ic.serial }}', {{ ic.stamps }})">
                                            ♻️ Reactivar
                                        </button>
                                    </form>
                                    <form id="form-purge-{{ ic.serial }}" action="/api/delete-client" method="POST" style="margin:0;">
                                        <input type="hidden" name="serial" value="{{ ic.serial }}">
                                        <input type="hidden" name="mode" value="purge">
                                        <button type="button" class="btn-export-csv" style="padding:6px 12px; font-size:11.5px; background:rgba(217,56,72,0.14); border-color:rgba(217,56,72,0.38); color:#C81D40; box-shadow:none;"
                                                onclick="openConfirmModal('purge', 'form-purge-{{ ic.serial }}', {{ ic.name|tojson|forceescape }}, '{{ ic.serial }}', {{ ic.stamps }})">
                                            🗑️ Eliminar Definitivamente
                                        </button>
                                    </form>
                                </div>
                            </td>
                        </tr>
                        {% else %}
                        <tr>
                            <td colspan="9" style="text-align:center; padding:24px; color:var(--text-secondary);">
                                La papelera está vacía. No hay clientes desactivados.
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>

            <!-- BITÁCORA DE AUDITORÍA DE MOVIMIENTOS (SELLOS Y CANJES) + RESUMEN DE CORTE DE TURNO -->
            <div class="db-header-card" style="margin-top:22px;">
                <div>
                    <div style="display:inline-flex; align-items:center; gap:6px; background:rgba(74,93,35,0.12); border:1px solid rgba(74,93,35,0.32); color:#4A5D23; padding:3px 10px; border-radius:99px; font-size:11px; font-weight:800; margin-bottom:6px;">
                        📊 CORTE DE TURNO EN VIVO · HORA SONORA ({{ shift_summary.date_label }})
                    </div>
                    <h2 style="margin:0; font-size:18px; font-weight:800; color:var(--text-primary);">
                        📜 Bitácora Reciente de Sellos y Canjes (Auditoría de Turno)
                    </h2>
                    <p style="margin:4px 0 0; font-size:12.5px; color:var(--text-secondary);">
                        Resumen automático del día para cuadrar caja en segundos e historial cronológico de los últimos movimientos (+1, -1, canjes y estados de cuenta).
                    </p>
                </div>
            </div>

            <!-- 3 CONTADORES AUTOMÁTICOS DEL DÍA DE HOY (PUNTO B #7) -->
            <div class="shift-summary-grid" role="region" aria-label="Resumen de corte de turno de hoy">
                <div class="shift-kpi-card" style="border-color:rgba(78, 48, 27, 0.22);">
                    <div>
                        <div class="shift-kpi-label">☕️ Sellos otorgados hoy</div>
                        <div class="shift-kpi-sub">
                            Movimientos +1 hoy ({{ shift_summary.date_label }})
                            {% if shift_summary.sub_today > 0 %} · {{ shift_summary.sub_today }} ajuste(s) -1{% endif %}
                        </div>
                    </div>
                    <div class="shift-kpi-value" style="color:#4E301B;" id="kpi-stamps-today">{{ shift_summary.stamps_today }}</div>
                </div>

                <div class="shift-kpi-card" style="border-color:rgba(36, 138, 61, 0.28);">
                    <div>
                        <div class="shift-kpi-label">🎁 Cafés gratis canjeados hoy</div>
                        <div class="shift-kpi-sub">Recompensas entregadas en el turno</div>
                    </div>
                    <div class="shift-kpi-value" style="color:#1E7A34;" id="kpi-redeemed-today">{{ shift_summary.redeemed_today }}</div>
                </div>

                <div class="shift-kpi-card" style="border-color:rgba(74, 93, 35, 0.28);">
                    <div>
                        <div class="shift-kpi-label">🆕 Nuevos socios registrados hoy</div>
                        <div class="shift-kpi-sub">Altas de tarjeta en el día ({{ shift_summary.date_label }})</div>
                    </div>
                    <div class="shift-kpi-value" style="color:#4A5D23;" id="kpi-new-clients-today">{{ shift_summary.new_clients_today }}</div>
                </div>
            </div>

            <div class="db-table-wrap" tabindex="0" role="region" aria-label="Bitácora reciente de sellos y canjes">
                <table class="db-table">
                    <thead>
                        <tr>
                            <th>Fecha / Hora (Sonora)</th>
                            <th>Movimiento</th>
                            <th>Cliente</th>
                            <th>Código Serial</th>
                            <th>Cambio de Sellos</th>
                            <th>Visitas Acumuladas</th>
                            <th>Canjes Totales</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for tx in recent_transactions or [] %}
                        <tr>
                            <td style="font-size:12px; color:var(--text-secondary);">{{ tx.created_at_sonora }}</td>
                            <td>
                                {% if tx.action == 'reset' %}
                                    <span class="status-pill-on" style="background:rgba(78,48,27,0.12); color:#4E301B; border:1px solid rgba(78,48,27,0.3);">🎁 Canje Café Gratis</span>
                                {% elif tx.action == 'sub' %}
                                    <span class="status-pill-off" style="color:#9A5000;">➖ Ajuste (-1 sello)</span>
                                {% elif tx.action == 'deactivate' %}
                                    <span class="status-pill-off" style="color:#9A5000; border-color:rgba(184,98,0,0.32);">📦 Desactivado</span>
                                {% elif tx.action == 'reactivate' %}
                                    <span class="status-pill-on" style="color:#1E7A34;">♻️ Reactivado</span>
                                {% else %}
                                    <span class="status-pill-on">☕️ +1 Sello</span>
                                {% endif %}
                            </td>
                            <td><strong>{{ tx.client_name }}</strong></td>
                            <td><span class="serial-tag">{{ tx.serial }}</span></td>
                            <td><strong style="color:var(--gold-light);">{{ tx.stamps_before }} → {{ tx.stamps_after }} / {{ campaign.stampGoal }}</strong></td>
                            <td>{{ tx.visits_after }}</td>
                            <td>{{ tx.redeemed_after }}</td>
                        </tr>
                        {% else %}
                        <tr>
                            <td colspan="7" style="text-align:center; padding:24px; color:var(--text-secondary);">
                                Aún no se han registrado movimientos en la bitácora.
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>

            <!-- BUZÓN DE SUGERENCIAS Y COMENTARIOS DEL HUB EN MESA (TABLA hub_feedback) -->
            <div id="buzon-sugerencias-section" class="db-header-card" style="margin-top:22px; border-color:rgba(78,48,27,0.32);">
                <div>
                    <div style="display:inline-flex; align-items:center; gap:6px; background:rgba(78,48,27,0.12); border:1px solid rgba(78,48,27,0.32); color:var(--gold-light); padding:3px 10px; border-radius:99px; font-size:11px; font-weight:800; margin-bottom:6px;">
                        💬 BUZÓN DE SUGERENCIAS EN MESA · SQLite (tabla hub_feedback)
                    </div>
                    <h2 style="margin:0; font-size:18px; font-weight:800; color:var(--text-primary);">
                        Mensajes Recibidos del Buzón de Sugerencias ({{ (feedback_list or [])|length }})
                    </h2>
                    <p style="margin:4px 0 0; font-size:12.5px; color:var(--text-secondary);">
                        Todos los mensajes enviados por los clientes desde el <strong>Buzón de Sugerencias</strong> del Hub Principal (<code>/</code>) se guardan automáticamente en la base de datos SQLite (tabla <code>hub_feedback</code>) con fecha y hora de Sonora.
                    </p>
                </div>
            </div>

            <div class="db-table-wrap" tabindex="0" role="region" aria-label="Buzón de sugerencias recibidas">
                <table class="db-table">
                    <thead>
                        <tr>
                            <th>#</th>
                            <th>Fecha / Hora (Sonora)</th>
                            <th>Tipo</th>
                            <th>Cliente / Serial</th>
                            <th>Mensaje o Sugerencia</th>
                            <th>Acción</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for fb in feedback_list or [] %}
                        <tr>
                            <td><span class="serial-tag">#{{ fb.id }}</span></td>
                            <td style="font-size:12px; color:var(--text-secondary); white-space:nowrap;">{{ fb.created_at_sonora }}</td>
                            <td>
                                {% if fb.sentiment == 'positive' %}
                                    <span class="status-pill-on">😍 Felicitación</span>
                                {% else %}
                                    <span class="status-pill-off" style="color:var(--gold-light); border-color:rgba(78,48,27,0.35); background:rgba(78,48,27,0.10);">💡 Sugerencia</span>
                                {% endif %}
                            </td>
                            <td>
                                {% if fb.client_name %}
                                    <strong>{{ fb.client_name }}</strong> <span class="serial-tag" style="margin-left:4px;">{{ fb.serial }}</span>
                                {% elif fb.serial %}
                                    <span class="serial-tag">{{ fb.serial }}</span>
                                {% else %}
                                    <span style="color:var(--text-secondary); font-size:12px;">Cliente en mesa (Anónimo)</span>
                                {% endif %}
                            </td>
                            <td style="max-width:520px; white-space:pre-wrap; line-height:1.45; font-weight:600; color:var(--text-primary);">{{ fb.message }}</td>
                            <td>
                                <form action="/api/delete-feedback" method="POST" style="margin:0;">
                                    <input type="hidden" name="feedback_id" value="{{ fb.id }}">
                                    <button type="submit" class="btn-icon-sec btn-del-subtle" title="Eliminar este mensaje del buzón">🗑️</button>
                                </form>
                            </td>
                        </tr>
                        {% else %}
                        <tr>
                            <td colspan="6" style="text-align:center; padding:24px; color:var(--text-secondary);">
                                Aún no se han recibido mensajes en el Buzón de Sugerencias.
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </section>

        <!-- VISTA 5: 🎨 DASHBOARD DE ADMINISTRACIÓN DE LA TARJETA / CAMPAÑA -->
        <section id="view-campaign-container" class="db-view-section">
            <div class="db-header-card" style="border-color:rgba(74,93,35,0.28);">
                <div>
                    <div style="display:inline-flex; align-items:center; gap:6px; background:rgba(74,93,35,0.12); border:1px solid rgba(74,93,35,0.34); color:#4A5D23; padding:3px 10px; border-radius:99px; font-size:11px; font-weight:800; margin-bottom:6px;">
                        🎨 PLANTILLA EN VIVO · APPLE WALLET & GOOGLE WALLET
                    </div>
                    <h2 style="margin:0; font-size:20px; font-weight:800; color:var(--text-primary);">
                        Dashboard de Administración de la Tarjeta / Campaña
                    </h2>
                    <p style="margin:4px 0 0; font-size:13px; color:var(--text-secondary);">
                        Personaliza colores, logotipo, banner/strip, meta de sellos, textos legales y coordenadas GPS. Los cambios se reflejan en tiempo real y sincronizan los pases de tus clientes.
                    </p>
                </div>
                <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
                    <button type="button" id="btn-save-campaign-top" class="btn-cam-launch" onclick="saveCampaignSettings()" style="padding:11px 18px; font-size:13.5px;">
                        💾 Guardar y Actualizar Plantilla
                    </button>
                </div>
            </div>

            <div id="ca-save-banner" style="display:none; margin-bottom:16px; padding:13px 16px; border-radius:14px; font-size:13.5px; font-weight:700;"></div>

            <div class="campaign-layout-grid">
                <!-- PANEL IZQUIERDO: 3 PESTAÑAS DE CONFIGURACIÓN -->
                <div class="bento-card campaign-editor-card">
                    <div>
                        <!-- Sub-navegación de las 3 pestañas del configurador -->
                        <div class="campaign-subtabs-bar">
                            <button type="button" id="ca-tab-btn-brand" class="nav-tab-btn active" style="justify-content:center; padding:10px 8px;" onclick="switchCampaignSubTab('brand')">
                                🎨 1. Marca y Diseño
                            </button>
                            <button type="button" id="ca-tab-btn-rules" class="nav-tab-btn" style="justify-content:center; padding:10px 8px;" onclick="switchCampaignSubTab('rules')">
                                🏆 2. Reglas del Programa
                            </button>
                            <button type="button" id="ca-tab-btn-location" class="nav-tab-btn" style="justify-content:center; padding:10px 8px;" onclick="switchCampaignSubTab('location')">
                                📍 3. Ubicación GPS
                            </button>
                        </div>

                        <!-- SUB-PESTAÑA 1: MARCA Y DISEÑO -->
                        <div id="ca-pane-brand">
                            <div style="margin-bottom:16px;">
                                <label for="ca-shop-name" style="font-size:11.5px; color:var(--gold-light); font-weight:700; display:block; margin-bottom:6px; text-transform:uppercase; letter-spacing:0.5px;">
                                    ☕️ Nombre de la Cafetería / Marca
                                </label>
                                <input type="text" id="ca-shop-name" class="input-dark" maxlength="40" required value="{{ campaign.shopName }}" placeholder="Ej. Demo Café" oninput="onCampaignFieldInput()">
                            </div>

                            <!-- Selectores de Color -->
                            <div class="campaign-colors-grid">
                                <div style="background:#FAF6F3; border:1px solid var(--border-subtle); border-radius:12px; padding:10px 12px;">
                                    <label for="ca-bg-hex" style="font-size:11px; color:var(--text-secondary); font-weight:700; display:block; margin-bottom:6px;">Color de Fondo</label>
                                    <div style="display:flex; align-items:center; gap:8px;">
                                        <input type="color" id="ca-bg-picker" aria-label="Selector de color de fondo" value="{{ campaign.backgroundColor }}" style="width:36px; height:34px; border:none; border-radius:8px; background:transparent; cursor:pointer;" oninput="syncColorPair('bg', this.value)">
                                        <input type="text" id="ca-bg-hex" class="input-dark" pattern="^#[0-9A-Fa-f]{6}$" value="{{ campaign.backgroundColor }}" maxlength="7" style="font-family:monospace; font-size:12.5px; padding:7px 9px; background:#FFFFFF;" oninput="syncColorPair('bg', this.value)">
                                    </div>
                                </div>

                                <div style="background:#FAF6F3; border:1px solid var(--border-subtle); border-radius:12px; padding:10px 12px;">
                                    <label for="ca-fg-hex" style="font-size:11px; color:var(--text-secondary); font-weight:700; display:block; margin-bottom:6px;">Color de Texto</label>
                                    <div style="display:flex; align-items:center; gap:8px;">
                                        <input type="color" id="ca-fg-picker" aria-label="Selector de color de texto" value="{{ campaign.foregroundColor }}" style="width:36px; height:34px; border:none; border-radius:8px; background:transparent; cursor:pointer;" oninput="syncColorPair('fg', this.value)">
                                        <input type="text" id="ca-fg-hex" class="input-dark" pattern="^#[0-9A-Fa-f]{6}$" value="{{ campaign.foregroundColor }}" maxlength="7" style="font-family:monospace; font-size:12.5px; padding:7px 9px; background:#FFFFFF;" oninput="syncColorPair('fg', this.value)">
                                    </div>
                                </div>

                                <div style="background:#FAF6F3; border:1px solid var(--border-subtle); border-radius:12px; padding:10px 12px;">
                                    <label for="ca-lbl-hex" style="font-size:11px; color:var(--text-secondary); font-weight:700; display:block; margin-bottom:6px;">Color Acento / Sellos</label>
                                    <div style="display:flex; align-items:center; gap:8px;">
                                        <input type="color" id="ca-lbl-picker" aria-label="Selector de color de acento" value="{{ campaign.labelColor }}" style="width:36px; height:34px; border:none; border-radius:8px; background:transparent; cursor:pointer;" oninput="syncColorPair('lbl', this.value)">
                                        <input type="text" id="ca-lbl-hex" class="input-dark" pattern="^#[0-9A-Fa-f]{6}$" value="{{ campaign.labelColor }}" maxlength="7" style="font-family:monospace; font-size:12.5px; padding:7px 9px; background:#FFFFFF;" oninput="syncColorPair('lbl', this.value)">
                                    </div>
                                </div>
                            </div>

                            <!-- Dropzones para Logo y Banner/Strip con validación de proporciones -->
                            <div class="campaign-dropzones-grid">
                                <!-- Dropzone Logo -->
                                <div style="background:#FAF6F3; border:1.5px dashed rgba(78,48,27,0.32); border-radius:14px; padding:14px; text-align:center;"
                                     ondragover="event.preventDefault(); this.style.borderColor='#4A5D23';"
                                     ondragleave="this.style.borderColor='rgba(78,48,27,0.32)';"
                                     ondrop="handleCampaignDrop(event, 'logo')">
                                    <label for="ca-logo-file" style="font-size:12px; font-weight:800; color:var(--gold-light); margin-bottom:4px; display:block; cursor:pointer;">🖼️ Logotipo de la Marca</label>
                                    <div style="font-size:11px; color:var(--text-secondary); margin-bottom:10px;">Proporción recomendada: <strong>1:1 cuadrada</strong> (ej. 660×660 px, PNG/JPG)</div>
                                    <img id="ca-logo-thumb" src="/api/campaign/logo.png?v={{ campaign.logoVersion }}" alt="Logo actual" style="width:60px; height:60px; border-radius:50%; object-fit:cover; border:2px solid var(--gold); margin:0 auto 10px; display:block;">
                                    <input type="file" id="ca-logo-file" class="visually-hidden" accept="image/png,image/jpeg,image/webp" onchange="handleCampaignFileSelect(this.files[0], 'logo')">
                                    <button type="button" class="btn-gold-sm" style="width:100%;" onclick="document.getElementById('ca-logo-file').click()">
                                        📤 Subir / Arrastrar Logo
                                    </button>
                                    <div id="ca-logo-feedback" style="font-size:11px; margin-top:8px; display:none; padding:6px 8px; border-radius:8px;"></div>
                                </div>

                                <!-- Dropzone Banner / Strip -->
                                <div style="background:#FAF6F3; border:1.5px dashed rgba(78,48,27,0.32); border-radius:14px; padding:14px; text-align:center;"
                                     ondragover="event.preventDefault(); this.style.borderColor='#4A5D23';"
                                     ondragleave="this.style.borderColor='rgba(78,48,27,0.32)';"
                                     ondrop="handleCampaignDrop(event, 'strip')">
                                    <label for="ca-strip-file" style="font-size:12px; font-weight:800; color:var(--gold-light); margin-bottom:4px; display:block; cursor:pointer;">🎞️ Fondo de Tira / Strip Banner</label>
                                    <div style="font-size:11px; color:var(--text-secondary); margin-bottom:10px;">Proporción recomendada: <strong>~3:1 panorámica</strong> (1125×432 o 1032×336 px)</div>
                                    <div style="display:flex; gap:6px; justify-content:center; margin-bottom:8px;">
                                        <input type="file" id="ca-strip-file" class="visually-hidden" accept="image/png,image/jpeg,image/webp" onchange="handleCampaignFileSelect(this.files[0], 'strip')">
                                        <button type="button" class="btn-gold-sm" style="flex:1;" onclick="document.getElementById('ca-strip-file').click()">
                                            📤 Subir Fondo Strip
                                        </button>
                                        <button type="button" class="btn-icon-sec" title="Usar fondo automático sin imagen" onclick="clearCustomStripBg()">↺ Auto</button>
                                    </div>
                                    <label for="ca-use-custom-strip" style="display:inline-flex; align-items:center; gap:6px; font-size:11.5px; color:var(--text-secondary); cursor:pointer;">
                                        <input type="checkbox" id="ca-use-custom-strip" {% if campaign.useCustomStrip %}checked{% endif %} onchange="onCampaignFieldInput()">
                                        Usar imagen de fondo en la tira de sellos
                                    </label>
                                    <div id="ca-strip-feedback" style="font-size:11px; margin-top:8px; display:none; padding:6px 8px; border-radius:8px;"></div>
                                </div>
                            </div>
                        </div>

                        <!-- SUB-PESTAÑA 2: REGLAS DEL PROGRAMA -->
                        <div id="ca-pane-rules" style="display:none;">
                            <div style="margin-bottom:16px;">
                                <label for="ca-stamp-goal" style="font-size:11.5px; color:var(--gold-light); font-weight:700; display:block; margin-bottom:6px; text-transform:uppercase; letter-spacing:0.5px;">
                                    🎯 Meta de Sellos para Canje (4 a 12 sellos)
                                </label>
                                <div style="display:flex; gap:8px; flex-wrap:wrap; align-items:center;">
                                    {% for g in [6, 8, 9, 10, 12] %}
                                    <button type="button" class="action-chip ca-goal-btn" data-goal="{{ g }}" onclick="setCampaignGoal({{ g }})" style="padding:8px 14px; font-size:13px; font-weight:800;">
                                        {{ g }} Sellos
                                    </button>
                                    {% endfor %}
                                    <input type="number" id="ca-stamp-goal" class="input-dark" min="4" max="12" value="{{ campaign.stampGoal }}" style="width:90px; text-align:center; font-weight:800;" oninput="onCampaignFieldInput()">
                                </div>
                            </div>

                            <div style="margin-bottom:16px;">
                                <label for="ca-reward-text" style="font-size:11.5px; color:var(--gold-light); font-weight:700; display:block; margin-bottom:6px; text-transform:uppercase; letter-spacing:0.5px;">
                                    🎁 Texto del Premio / Recompensa
                                </label>
                                <input type="text" id="ca-reward-text" class="input-dark" maxlength="80" required value="{{ campaign.rewardText }}" placeholder="Ej. 1 Café Gratis de cualquier tamaño" oninput="onCampaignFieldInput()">
                            </div>

                            <div style="margin-bottom:16px;">
                                <label for="ca-menu-url" style="font-size:11.5px; color:var(--gold-light); font-weight:700; display:block; margin-bottom:6px; text-transform:uppercase; letter-spacing:0.5px;">
                                    🔗 Enlace al Menú Digital o WhatsApp
                                </label>
                                <input type="url" id="ca-menu-url" class="input-dark" maxlength="200" value="{{ campaign.menuUrl }}" placeholder="https://wa.me/526421234567 o link de Instagram/Menú" oninput="onCampaignFieldInput()">
                            </div>

                            <div style="margin-bottom:8px;">
                                <label for="ca-terms-text" style="font-size:11.5px; color:var(--gold-light); font-weight:700; display:block; margin-bottom:6px; text-transform:uppercase; letter-spacing:0.5px;">
                                    📜 Textos Legales y Términos (Reverso de la Tarjeta)
                                </label>
                                <textarea id="ca-terms-text" class="input-dark" rows="4" maxlength="500" placeholder="Condiciones de uso del programa de lealtad..." oninput="onCampaignFieldInput()">{{ campaign.termsText }}</textarea>
                            </div>
                        </div>

                        <!-- SUB-PESTAÑA 3: UBICACIÓN Y PROXIMIDAD -->
                        <div id="ca-pane-location" style="display:none;">
                            <div class="campaign-gps-grid">
                                <div>
                                    <label for="ca-lat" style="font-size:11.5px; color:var(--gold-light); font-weight:700; display:block; margin-bottom:6px;">Latitud (GPS)</label>
                                    <input type="text" id="ca-lat" class="input-dark" value="{{ campaign.lat }}" oninput="onCampaignFieldInput()">
                                </div>
                                <div>
                                    <label for="ca-lng" style="font-size:11.5px; color:var(--gold-light); font-weight:700; display:block; margin-bottom:6px;">Longitud (GPS)</label>
                                    <input type="text" id="ca-lng" class="input-dark" value="{{ campaign.lng }}" oninput="onCampaignFieldInput()">
                                </div>
                                <div class="campaign-gps-btn-col">
                                    <button type="button" class="btn-gold-sm" onclick="detectCampaignGPS()">📍 Usar mi GPS</button>
                                </div>
                            </div>

                            <div style="margin-bottom:16px;">
                                <label for="ca-relevant-text" style="font-size:11.5px; color:var(--gold-light); font-weight:700; display:block; margin-bottom:6px;">
                                    📲 Mensaje de Notificación por Proximidad (~150m en Apple Wallet)
                                </label>
                                <input type="text" id="ca-relevant-text" class="input-dark" maxlength="160" value="{{ campaign.relevantText }}" oninput="onCampaignFieldInput()">
                            </div>

                            <!-- Vista previa de pantalla de bloqueo iOS por cercanía -->
                            <div class="ios-lockscreen-preview">
                                <div class="ios-notif-icon">☕️</div>
                                <div class="ios-notif-body">
                                    <div class="ios-notif-top">
                                        <span id="ca-gps-lock-brand">{{ campaign.shopName | upper }} · CERCA DE TI</span>
                                        <span>ahora</span>
                                    </div>
                                    <div class="ios-notif-text" id="ca-gps-lock-msg">{{ campaign.relevantText }}</div>
                                </div>
                            </div>
                        </div>

                        <div style="margin-top:20px; padding-top:16px; border-top:1px solid var(--border-subtle); display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap;">
                            <span style="font-size:12px; color:var(--text-secondary);">
                                Al guardar se actualiza la plantilla para nuevos registros y pases activos.
                            </span>
                            <button type="button" id="btn-save-campaign-bottom" class="btn-cam-launch" style="width:100%; max-width:340px; justify-content:center;" onclick="saveCampaignSettings()">
                                💾 Guardar y Actualizar Plantilla de Campaña
                            </button>
                        </div>
                    </div>
                </div>

                <!-- PANEL LATERAL DERECHO: PREVIEW EN TIEMPO REAL ESTILO APPLE WALLET -->
                <div class="bento-card campaign-preview-card">
                    <div>
                        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; flex-wrap:wrap; gap:8px;">
                            <div>
                                <span style="font-size:11px; font-weight:800; color:#4A5D23; text-transform:uppercase; letter-spacing:0.7px;">⚡ Preview en Tiempo Real</span>
                                <h3 style="margin:2px 0 0; font-size:16px; font-weight:800; color:var(--text-primary);">Simulador Apple Wallet</h3>
                            </div>
                            <div style="display:flex; gap:6px; align-items:center;">
                                <button type="button" class="action-chip" onclick="setPreviewStamps(0)">0</button>
                                <button type="button" class="action-chip" onclick="setPreviewStamps(3)">3</button>
                                <button type="button" class="action-chip" onclick="setPreviewStamps('goal')">Meta</button>
                                <button type="button" id="ca-flip-btn" class="action-chip" style="border-color:rgba(78,48,27,0.35); color:var(--gold-light);" onclick="toggleCampaignPreviewSide()">
                                    🔄 Reverso
                                </button>
                            </div>
                        </div>

                        <!-- TARJETA SIMULADA APPLE WALLET -->
                        <div id="ca-wallet-card" style="border-radius:22px; overflow:hidden; border:1px solid rgba(255,255,255,0.18); box-shadow:0 22px 50px rgba(0,0,0,0.75); transition:background 0.2s ease, color 0.2s ease; background:{{ campaign.backgroundColor }}; color:{{ campaign.foregroundColor }};">
                            <!-- CARA FRONTAL -->
                            <div id="ca-preview-front">
                                <!-- Header del Pase -->
                                <div style="padding:14px 18px 12px; display:flex; justify-content:space-between; align-items:center;">
                                    <div style="display:flex; align-items:center; gap:9px; min-width:0;">
                                        <img id="ca-prev-logo" src="/api/campaign/logo.png?v={{ campaign.logoVersion }}" alt="Logo" style="width:32px; height:32px; border-radius:50%; object-fit:cover; border:1px solid rgba(255,255,255,0.25); flex-shrink:0;">
                                        <span id="ca-prev-shop-name" style="font-weight:800; font-size:13.5px; letter-spacing:0.7px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">{{ campaign.shopName | upper }}</span>
                                    </div>
                                    <div style="text-align:right;">
                                        <div id="ca-prev-hdr-lbl" style="font-size:9.5px; font-weight:800; letter-spacing:0.8px; color:{{ campaign.labelColor }};">SELLOS</div>
                                        <div id="ca-prev-hdr-val" style="font-size:17px; font-weight:800;">3 / {{ campaign.stampGoal }}</div>
                                    </div>
                                </div>

                                <!-- Strip / Tira de Sellos en Tiempo Real -->
                                <div id="ca-prev-strip-box" style="position:relative; padding:14px 16px 12px; background:rgba(0,0,0,0.28); border-top:1px solid rgba(255,255,255,0.12); border-bottom:1px solid rgba(255,255,255,0.12); background-size:cover; background-position:center;">
                                    <div id="ca-prev-strip-overlay" style="position:relative; z-index:2;">
                                        <div style="display:flex; justify-content:space-between; align-items:center; font-size:9.5px; font-weight:800; letter-spacing:0.6px; margin-bottom:10px;">
                                            <span id="ca-prev-strip-title" style="color:{{ campaign.labelColor }};">TARJETA DE FIDELIDAD</span>
                                            <span id="ca-prev-strip-status" style="color:{{ campaign.foregroundColor }};">3/{{ campaign.stampGoal }} SELLOS</span>
                                        </div>
                                        <!-- Grid dinámico de círculos de sellos -->
                                        <div id="ca-prev-stamps-grid" style="display:grid; grid-template-columns:repeat(5, 1fr); gap:8px; justify-items:center; margin-bottom:8px;"></div>
                                        <div id="ca-prev-strip-footer" style="text-align:center; font-size:10px; font-weight:700; color:{{ campaign.labelColor }}; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">
                                            Premio: {{ campaign.rewardText }}
                                        </div>
                                    </div>
                                </div>

                                <!-- Secondary Fields -->
                                <div style="padding:12px 18px; display:grid; grid-template-columns:1fr 1.2fr; gap:10px;">
                                    <div>
                                        <div id="ca-prev-sec1-lbl" style="font-size:9px; font-weight:800; letter-spacing:0.6px; color:{{ campaign.labelColor }};">CLIENTE · VIP</div>
                                        <div style="font-size:12.5px; font-weight:700; margin-top:2px;">Socio Demo</div>
                                    </div>
                                    <div style="text-align:right;">
                                        <div id="ca-prev-sec2-lbl" style="font-size:9px; font-weight:800; letter-spacing:0.6px; color:{{ campaign.labelColor }};">PREMIO META</div>
                                        <div id="ca-prev-reward-val" style="font-size:12px; font-weight:700; margin-top:2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">{{ campaign.rewardText }}</div>
                                    </div>
                                </div>

                                <!-- Zona QR -->
                                <div style="padding:10px 18px 18px; text-align:center;">
                                    <div style="background:#FFFFFF; display:inline-block; padding:10px; border-radius:14px; box-shadow:0 8px 20px rgba(0,0,0,0.45);">
                                        <div style="width:96px; height:96px; background:repeating-conic-gradient(#12100E 0% 25%, #FFFFFF 0% 50%) 50% / 16px 16px; display:flex; align-items:center; justify-content:center; border:3px solid #12100E;">
                                            <span style="background:#FFFFFF; color:#12100E; font-size:10px; font-weight:900; padding:2px 5px; border-radius:4px;">QR PASS</span>
                                        </div>
                                    </div>
                                    <div style="font-family:ui-monospace, monospace; font-size:11px; opacity:0.75; margin-top:6px; letter-spacing:1px;">DEMO-000001</div>
                                </div>
                            </div>

                            <!-- CARA REVERSO (ESTILO NATIVO APPLE WALLET iOS) -->
                            <div id="ca-preview-back" style="display:none; background:#1C1C1E; color:#FFFFFF; padding:4px 16px; font-size:13px;">
                                <div style="padding:11px 0; border-bottom:1px solid rgba(255,255,255,0.12);">
                                    <div style="font-size:13px; font-weight:600; color:#FFFFFF;">Último Mensaje</div>
                                    <div id="ca-back-val-gps" style="margin-top:3px; color:#98989F; line-height:1.35;">{{ campaign.relevantText }}</div>
                                </div>
                                <div style="padding:11px 0; border-bottom:1px solid rgba(255,255,255,0.12);">
                                    <div style="font-size:12px; color:#98989F;">Formulario de comentarios</div>
                                    <div style="margin-top:3px;">
                                        <a href="/" target="_blank" style="color:#0A84FF; text-decoration:none; font-weight:500;">⭐ ¿Disfrutaste tu última visita?</a>
                                    </div>
                                </div>
                                <div style="padding:11px 0; border-bottom:1px solid rgba(255,255,255,0.12); display:flex; justify-content:space-between; align-items:center;">
                                    <span style="color:#FFFFFF;">Total acumulado</span>
                                    <span id="ca-back-total-stamps" style="color:#98989F;">3 sellos</span>
                                </div>
                                <div style="padding:11px 0; border-bottom:1px solid rgba(255,255,255,0.12); display:flex; justify-content:space-between; align-items:center;">
                                    <span style="color:#FFFFFF;">Para la siguiente recompensa</span>
                                    <span id="ca-back-remaining-stamps" style="color:#98989F;">{{ campaign.stampGoal - 3 }} sellos</span>
                                </div>
                                <div style="padding:11px 0; border-bottom:1px solid rgba(255,255,255,0.12); display:flex; justify-content:space-between; align-items:center;">
                                    <span style="color:#FFFFFF;">Disponible</span>
                                    <span id="ca-back-available-rewards" style="color:#98989F;">0 premios</span>
                                </div>
                                <div style="padding:11px 0; border-bottom:1px solid rgba(255,255,255,0.12);">
                                    <div style="color:#FFFFFF;">Cómo conseguir un sello</div>
                                    <div style="margin-top:2px; color:#98989F;">Un sello = Una visita</div>
                                </div>
                                <div style="padding:11px 0; border-bottom:1px solid rgba(255,255,255,0.12); display:flex; justify-content:space-between; align-items:center;">
                                    <span style="color:#FFFFFF;">Total acumulado</span>
                                    <span id="ca-back-total-rewards" style="color:#98989F;">0 premios</span>
                                </div>
                                <div style="padding:11px 0; border-bottom:1px solid rgba(255,255,255,0.12);">
                                    <div id="ca-back-lbl-reward" style="font-size:12px; color:#98989F;">Descripción del premio</div>
                                    <div id="ca-back-val-reward" style="margin-top:2px; color:#98989F;">{{ campaign.stampGoal }} sellos = {{ campaign.rewardText }}</div>
                                </div>
                                <div style="padding:11px 0; border-bottom:1px solid rgba(255,255,255,0.12); display:flex; justify-content:space-between; align-items:center;">
                                    <span style="color:#FFFFFF;">La tarjeta es válida hasta</span>
                                    <span style="color:#98989F;">Ilimitado</span>
                                </div>
                                <div style="padding:11px 0; border-bottom:1px solid rgba(255,255,255,0.12);">
                                    <div id="ca-back-lbl-terms" style="color:#FFFFFF; margin-bottom:4px;">Términos de Uso</div>
                                    <div id="ca-back-val-terms" style="color:#98989F; font-size:12px; line-height:1.45; white-space:pre-line;">{{ campaign.termsText }}</div>
                                </div>
                                <div style="padding:11px 0; border-bottom:1px solid rgba(255,255,255,0.12);">
                                    <a href="tel:+526421138069" style="color:#0A84FF; text-decoration:none; font-weight:500;">📱 Teléfono</a>
                                </div>
                                <div style="padding:11px 0; border-bottom:1px solid rgba(255,255,255,0.12);">
                                    <a href="https://www.instagram.com/" target="_blank" rel="noopener" style="color:#0A84FF; text-decoration:none; font-weight:500;">Instagram</a>
                                    <span id="ca-back-lbl-menu" style="display:none;"></span>
                                    <span id="ca-back-val-menu" style="display:none;">{{ campaign.menuUrl }}</span>
                                </div>
                                <div style="padding:11px 0; border-bottom:1px solid rgba(255,255,255,0.12); display:flex; justify-content:space-between; align-items:center;">
                                    <span style="color:#FFFFFF;">Nombre de empresa</span>
                                    <span id="ca-back-company-name" style="color:#98989F;">{{ campaign.shopName }}</span>
                                </div>
                                <div style="padding:11px 0; border-bottom:1px solid rgba(255,255,255,0.12);">
                                    <div style="font-size:12px; color:#98989F;">Información del emisor</div>
                                    <div id="ca-back-issuer-name" style="color:#98989F; margin-top:2px;">{{ campaign.shopName }}</div>
                                    <div><a href="tel:+526421138069" style="color:#0A84FF; text-decoration:none;">+526421138069</a></div>
                                </div>
                                <div style="padding:11px 0; border-bottom:1px solid rgba(255,255,255,0.12); display:flex; justify-content:space-between; align-items:center;">
                                    <span style="color:#FFFFFF;">Número de serie</span>
                                    <span style="color:#98989F; font-family:ui-monospace, monospace; font-size:12px;">DEMO-000001</span>
                                </div>
                                <div style="padding:11px 0; display:flex; justify-content:space-between; align-items:center;">
                                    <span style="color:#FFFFFF;">Actualizado</span>
                                    <span id="ca-back-updated-at" style="color:#98989F; font-size:12px;">Hoy</span>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- CONFIGURACIÓN DEL HUB DE EXPERIENCIA EN MESA (WI-FI, GOOGLE REVIEWS, ESLOGAN, INSTAGRAM) -->
            <div class="bento-card" style="margin-top:16px;">
                <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px; margin-bottom:14px;">
                    <div>
                        <span style="font-size:11px; font-weight:800; color:var(--gold-light); text-transform:uppercase; letter-spacing:0.6px;">📱 Hub de Experiencia en Mesa (Estilo Candour)</span>
                        <h3 style="margin:2px 0 0; font-size:17px; font-weight:800;">Ajustes de Wi-Fi, Google Reviews y Portada del Cliente</h3>
                    </div>
                    <a href="/" target="_blank" class="action-chip" style="text-decoration:none; border-color:rgba(212,175,55,0.45); color:var(--gold-light);">
                        🔗 Ver Hub del Cliente (/) ↗
                    </a>
                </div>
                <form action="/api/hub/settings" method="POST" style="display:grid; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); gap:12px;">
                    <div>
                        <label style="font-size:11.5px; color:var(--gold-light); font-weight:700; display:block; margin-bottom:5px;">Eslogan debajo de Demo Café</label>
                        <input type="text" name="slogan" class="input-dark" value="{{ hub.slogan if hub is defined else 'Tu rincón favorito de café de especialidad' }}" required>
                    </div>
                    <div>
                        <label style="font-size:11.5px; color:var(--gold-light); font-weight:700; display:block; margin-bottom:5px;">Nombre de Red Wi-Fi (SSID)</label>
                        <input type="text" name="wifi_ssid" class="input-dark" value="{{ hub.wifi_ssid if hub is defined else 'Demo_Cafe_WiFi' }}" required>
                    </div>
                    <div>
                        <label style="font-size:11.5px; color:var(--gold-light); font-weight:700; display:block; margin-bottom:5px;">Contraseña del Wi-Fi</label>
                        <input type="text" name="wifi_pass" class="input-dark" value="{{ hub.wifi_pass if hub is defined else 'democafe2026' }}" required>
                    </div>
                    <div>
                        <label style="font-size:11.5px; color:var(--gold-light); font-weight:700; display:block; margin-bottom:5px;">Enlace de Google Reviews / Maps</label>
                        <input type="url" name="google_review_url" class="input-dark" value="{{ hub.google_review_url if hub is defined else '' }}" required>
                    </div>
                    <div>
                        <label style="font-size:11.5px; color:var(--gold-light); font-weight:700; display:block; margin-bottom:5px;">Usuario de Instagram</label>
                        <input type="text" name="instagram_handle" class="input-dark" value="{{ hub.instagram_handle if hub is defined else '@democafe' }}">
                    </div>
                    <div style="display:flex; align-items:flex-end;">
                        <button type="submit" class="btn-cam-launch" style="width:100%; justify-content:center;">
                            💾 Guardar Ajustes del Hub
                        </button>
                    </div>
                </form>
            </div>
        </section>

        <!-- VISTA 6: MÓDULO DE GIFT CARDS DIGITALES (EMISIÓN EN CAJA, ACTIVACIÓN Y COBRO DE SALDO / COMBOS) -->
        <section id="view-gifts-container" style="display:none;">
            <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(320px, 1fr)); gap:18px; margin-bottom:20px;">
                <!-- Emitir Nueva Gift Card en Mostrador -->
                <div class="bento-card">
                    <div class="bento-header">
                        <h2 class="bento-title">🎁 Emitir Nueva Gift Card en Caja</h2>
                        <span style="font-size:11px; font-weight:800; color:#1E7A34; background:rgba(30,122,52,0.12); padding:3px 9px; border-radius:99px;">🟢 Sale Activa</span>
                    </div>
                    <p class="bento-subtitle" style="margin-bottom:14px;">
                        Crea una Gift Card ya cobrada en mostrador y comparte el Sobre Digital o QR con el comprador.
                    </p>
                    <form action="/api/barista/gift-create" method="POST">
                        <input type="hidden" name="gift_type" id="bgc-type-input" value="balance">
                        <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px; background:#FAF6F3; padding:5px; border-radius:12px; border:1px solid var(--border-subtle); margin-bottom:12px;">
                            <button type="button" id="bgc-tab-balance" class="seg-btn active" onclick="toggleBaristaGiftCreateType('balance')" style="padding:8px; font-weight:800;">
                                💳 Saldo en Pesos
                            </button>
                            <button type="button" id="bgc-tab-combo" class="seg-btn" onclick="toggleBaristaGiftCreateType('combo')" style="padding:8px; font-weight:800;">
                                🥐 Vale / Combo
                            </button>
                        </div>

                        <div id="bgc-pane-balance" style="margin-bottom:12px;">
                            <label style="font-size:11.5px; color:var(--gold-light); font-weight:700; display:block; margin-bottom:5px;">Monto de Saldo ($ MXN)</label>
                            <div style="display:flex; gap:6px; flex-wrap:wrap; margin-bottom:6px;">
                                <button type="button" class="action-chip" onclick="document.getElementById('bgc-amount-input').value='100'">$100</button>
                                <button type="button" class="action-chip" onclick="document.getElementById('bgc-amount-input').value='200'">$200</button>
                                <button type="button" class="action-chip" onclick="document.getElementById('bgc-amount-input').value='300'">$300</button>
                                <button type="button" class="action-chip" onclick="document.getElementById('bgc-amount-input').value='500'">$500</button>
                            </div>
                            <input type="number" name="amount" id="bgc-amount-input" class="input-dark" value="200" min="50" max="5000" step="10">
                        </div>

                        <div id="bgc-pane-combo" style="display:none; margin-bottom:12px;">
                            <label style="font-size:11.5px; color:var(--gold-light); font-weight:700; display:block; margin-bottom:5px;">Selecciona el Paquete / Experiencia</label>
                            <select name="package_key" id="bgc-package-select" class="input-dark" style="font-weight:700;">
                                {% if gift_combos is defined and gift_combos %}
                                    {% for cmb in gift_combos %}
                                    <option value="{{ cmb.key }}" {{ 'selected' if loop.first else '' }}>{{ cmb.icon }} {{ cmb.title }} (${{ '%.0f'|format(cmb.price) }} MXN — {{ cmb.desc }})</option>
                                    {% endfor %}
                                {% else %}
                                    <option value="cafe_individual">☕ Vale «Te invito un Café» ($85 MXN — 1 bebida)</option>
                                    <option value="manana_perfecta" selected>🥐 Pack «Mañana Perfecta» ($160 MXN — 1 bebida + 1 postre/panini)</option>
                                    <option value="cita_para_dos">👯 Pack «Cita para Dos» ($280 MXN — 2 bebidas + 2 postres/paninis)</option>
                                {% endif %}
                            </select>
                            <div style="margin-top:6px; font-size:11.5px; color:var(--text-secondary);">
                                💡 ¿Quieres agregar o editar paquetes? Ve a la pestaña <a href="javascript:void(0)" onclick="switchMainView('combos')" style="color:var(--gold-light); font-weight:800; text-decoration:underline;">🥐 Vales / Combos</a>.
                            </div>
                        </div>

                        <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:10px;">
                            <div>
                                <label style="font-size:11.5px; color:var(--gold-light); font-weight:700; display:block; margin-bottom:4px;">Para (Destinatario)</label>
                                <input type="text" name="recipient_name" class="input-dark" placeholder="Ej. Ana Sofía" required>
                            </div>
                            <div>
                                <label style="font-size:11.5px; color:var(--gold-light); font-weight:700; display:block; margin-bottom:4px;">De parte de (Comprador)</label>
                                <input type="text" name="sender_name" class="input-dark" placeholder="Ej. Gonzalo" required>
                            </div>
                        </div>

                        <div style="margin-bottom:12px;">
                            <label style="font-size:11.5px; color:var(--gold-light); font-weight:700; display:block; margin-bottom:4px;">Dedicatoria personalizada (Opcional)</label>
                            <input type="text" name="dedication" class="input-dark" placeholder="Ej. ¡Feliz cumpleaños! Te invito tu café favorito ☕🎂">
                        </div>

                        <button type="submit" class="btn-cam-launch" style="width:100%; justify-content:center;">
                            🎁 Emitir Gift Card Activa
                        </button>
                    </form>
                </div>

                <!-- Guía Rápida de Operación de Gift Cards -->
                <div class="bento-card" style="display:flex; flex-direction:column; justify-content:space-between;">
                    <div>
                        <div class="bento-header">
                            <h2 class="bento-title">⚡ Cómo Operar las Gift Cards</h2>
                        </div>
                        <div style="display:flex; flex-direction:column; gap:10px; font-size:13px; color:var(--text-primary); line-height:1.45; margin-top:6px;">
                            <div style="background:#FAF6F3; border:1px solid var(--border-subtle); border-radius:14px; padding:11px 13px;">
                                <strong style="color:#B86200;">1. Activación de Gift Cards Creadas por Clientes:</strong><br>
                                Cuando un cliente crea una Gift Card desde el Hub, aparece abajo como <strong>⏳ Por Activar</strong> con su identificador <code style="font-weight:800;">GIFT-XXXXXX</code>. Al recibir su pago en caja o transferencia por WhatsApp, toca <strong>✅ Activar Gift Card</strong> y al cliente se le habilitará de inmediato el envío por WhatsApp.
                            </div>
                            <div style="background:#FAF6F3; border:1px solid var(--border-subtle); border-radius:14px; padding:11px 13px;">
                                <strong style="color:var(--gold-light);">2. Escaneo Directo y Saldo Parcial:</strong><br>
                                Usa la misma cámara QR de <strong>☕️ Mostrador POS</strong>. Cada descuento de saldo actualiza al instante el Apple Wallet / Google Wallet del cliente con una notificación Push indicando el monto descontado, y cuando el saldo llega a $0 la tarjeta expira automáticamente.
                            </div>
                            <div style="background:#FAF6F3; border:1px solid var(--border-subtle); border-radius:14px; padding:11px 13px;">
                                <strong style="color:#1E7A34;">3. Conversión a Cliente Frecuente (+1 Sello Gratis):</strong><br>
                                Desde su regalo digital (<code style="font-weight:800;">/gift/GIFT-XXXXXX</code>), la persona que recibió el regalo puede convertir su Gift Card en su propia Tarjeta de Lealtad empezando con <strong>1 sello gratis</strong>.
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Lista de Gift Cards Activas, Pendientes y Canjeadas -->
            <div class="bento-card" id="barista-gift-cards-list-card">
                <div class="bento-header" style="margin-bottom:12px;">
                    <h2 class="bento-title">📋 Gift Cards Registradas ({{ gift_cards|length if gift_cards is defined else 0 }})</h2>
                </div>
                <div class="client-list">
                    {% if gift_cards is defined and gift_cards %}
                        {% for g in gift_cards %}
                        <div class="client-card-row {{ 'highlight-row' if gift_updated == g.code else '' }}" style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px; {{ 'border:1.5px solid rgba(245,158,11,0.55); background:rgba(245,158,11,0.06);' if g.status == 'pending' else '' }}">
                            <div style="display:flex; align-items:center; gap:12px; min-width:240px;">
                                <div class="avatar-circle tier-gold" style="font-size:18px;">
                                    {{ '🥐' if g.gift_type == 'combo' else '🎁' }}
                                </div>
                                <div>
                                    <div style="display:flex; align-items:center; gap:7px; flex-wrap:wrap;">
                                        <span class="client-name">Para: {{ g.recipient_name }}</span>
                                        <span class="serial-tag">{{ g.code }}</span>
                                        {% if g.status == 'active' %}
                                            <span class="status-pill-on">🟢 Activa</span>
                                        {% elif g.status == 'redeemed' %}
                                            <span class="status-pill-off" style="color:#4A5D23; border-color:rgba(74,93,35,0.4);">🎉 Canjeada / Expirada</span>
                                        {% else %}
                                            <span class="status-pill-off" style="color:#B86200; background:rgba(245,158,11,0.18); border-color:rgba(184,98,0,0.55); font-weight:800;">⏳ Pendiente de Activar por Barista</span>
                                        {% endif %}
                                        {% if g.converted_serial %}
                                            <span class="status-pill-on" style="background:rgba(74,93,35,0.14); color:#4A5D23;">✨ Convertida a Socio ({{ g.converted_serial }})</span>
                                        {% endif %}
                                    </div>
                                    <div style="font-size:12.5px; color:var(--text-secondary); margin-top:3px;">
                                        <strong>{{ g.value_headline }}</strong> ({{ g.balance_display }}) · De parte de: <strong>{{ g.sender_name }}</strong> · Creada: {{ g.created_at_sonora }}
                                    </div>
                                    <div style="font-size:11.5px; color:var(--text-tertiary); margin-top:2px;">
                                        💌 “{{ g.dedication }}”
                                    </div>
                                </div>
                            </div>

                            <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
                                {% if g.status == 'pending' %}
                                <form action="/api/barista/gift-action" method="POST" style="margin:0;" onsubmit="saveBaristaUiState()">
                                    <input type="hidden" name="code" value="{{ g.code }}">
                                    <input type="hidden" name="action" value="activate">
                                    <button type="submit" style="padding:9px 14px; border-radius:10px; border:none; background:#1E7A34; color:#FFF; font-weight:800; font-size:12.5px; cursor:pointer; box-shadow:0 4px 12px rgba(30,122,52,0.28);">
                                        ✅ Activar Gift Card (Pago Recibido)
                                    </button>
                                </form>
                                {% endif %}
                                <button type="button"
                                        class="btn-gold-sm"
                                        data-code="{{ g.code }}"
                                        data-type="{{ g.gift_type }}"
                                        data-status="{{ g.status }}"
                                        data-headline="{{ g.value_headline }}"
                                        data-desc="{{ g.package_desc }}"
                                        data-balance="{{ g.current_balance }}"
                                        data-recipient="{{ g.recipient_name }}"
                                        data-sender="{{ g.sender_name }}"
                                        data-wa-url="{{ g.whatsapp_share_url }}"
                                        data-gift-url="{{ g.gift_url }}"
                                        onclick="openGiftPosModalFromBtn(this)">
                                    {% if g.status == 'pending' %}
                                        ⚙️ Opciones
                                    {% elif g.gift_type == 'combo' %}
                                        🎉 Canjear Paquete
                                    {% else %}
                                        💳 Descontar Saldo
                                    {% endif %}
                                </button>
                                <a href="{{ g.whatsapp_share_url }}" target="_blank" rel="noopener" class="action-chip" style="text-decoration:none; background:rgba(37,211,102,0.14); border-color:rgba(30,122,52,0.35); color:#1E7A34; font-weight:800;">
                                    📲 Enviar WhatsApp
                                </a>
                                <button type="button" class="action-chip" data-gift-url="{{ g.gift_url }}" onclick="copyGiftUrlFromBarista(this)" style="cursor:pointer; font-weight:800;">
                                    📋 Copiar Link
                                </button>
                                <a href="/gift/{{ g.code }}" target="_blank" class="action-chip" style="text-decoration:none;">
                                    🎁 Abrir ↗
                                </a>
                            </div>
                        </div>
                        {% endfor %}
                    {% else %}
                        <div style="text-align:center; padding:28px; color:var(--text-secondary); font-size:13.5px;">
                            Aún no hay Gift Cards registradas. Puedes emitir una arriba o desde el Hub del Cliente.
                        </div>
                    {% endif %}
                </div>
            </div>
        </section>

        <!-- VISTA 7: PESTAÑA DE VALES / COMBOS DE GIFT CARDS ("Selecciona el Paquete / Experiencia") -->
        <section id="view-combos-container" class="db-view-section" style="display:none;">
            <div class="db-header-card" style="margin-bottom:18px;">
                <div>
                    <h2 style="margin:0; font-size:20px; font-weight:800; color:var(--text-primary);">
                        🥐 Catálogo de Vales y Combos para Gift Cards
                    </h2>
                    <p style="margin:4px 0 0; font-size:13px; color:var(--text-secondary);">
                        Agrega, edita o elimina los paquetes de la sección <strong>«Selecciona el Paquete / Experiencia»</strong> que ven tanto tus clientes en el Hub como el barista al emitir una Gift Card.
                    </p>
                </div>
                <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
                    <button type="button" class="action-chip" onclick="switchMainView('gifts')" style="font-weight:800; cursor:pointer;">
                        🎁 Ir a Emitir Gift Cards →
                    </button>
                </div>
            </div>

            <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(320px, 1fr)); gap:18px; align-items:start;">
                <!-- Formulario para Agregar / Editar Vale o Combo -->
                <div class="bento-card">
                    <div class="bento-header">
                        <h2 class="bento-title" id="combo-form-title">➕ Agregar Nuevo Vale / Combo</h2>
                        <button type="button" id="combo-form-cancel-btn" onclick="resetBaristaGiftComboForm()" style="display:none; background:none; border:none; color:var(--gold-light); font-size:12px; font-weight:800; cursor:pointer; text-decoration:underline;">
                            Cancelar edición
                        </button>
                    </div>
                    <p class="bento-subtitle" style="margin-bottom:14px;">
                        Aparecerá al instante en la opción <strong>«Selecciona el Paquete / Experiencia»</strong> al crear una Gift Card.
                    </p>
                    <form action="/api/barista/gift-combos/save" method="POST" onsubmit="saveBaristaUiState()">
                        <input type="hidden" name="combo_key" id="combo-form-key" value="">

                        <div style="display:grid; grid-template-columns:84px 1fr; gap:10px; margin-bottom:12px;">
                            <div>
                                <label style="font-size:11.5px; color:var(--gold-light); font-weight:700; display:block; margin-bottom:4px;">Ícono</label>
                                <input type="text" name="icon" id="combo-form-icon" class="input-dark" value="🥐" maxlength="4" style="text-align:center; font-size:20px;" required>
                            </div>
                            <div>
                                <label style="font-size:11.5px; color:var(--gold-light); font-weight:700; display:block; margin-bottom:4px;">Nombre del Paquete / Experiencia</label>
                                <input type="text" name="title" id="combo-form-name" class="input-dark" placeholder="Ej. Pack «Tarde de Postre y Café»" required>
                            </div>
                        </div>

                        <div style="margin-bottom:12px;">
                            <label style="font-size:11.5px; color:var(--gold-light); font-weight:700; display:block; margin-bottom:4px;">¿Qué incluye el paquete? (Descripción)</label>
                            <input type="text" name="desc" id="combo-form-desc" class="input-dark" placeholder="Ej. 1 bebida caliente o fría + 1 rebanada de pastel" required>
                        </div>

                        <div style="margin-bottom:14px;">
                            <label style="font-size:11.5px; color:var(--gold-light); font-weight:700; display:block; margin-bottom:4px;">Precio del Paquete ($ MXN)</label>
                            <input type="number" name="price" id="combo-form-price" class="input-dark" placeholder="Ej. 185" min="10" max="10000" step="1" required>
                        </div>

                        <button type="submit" id="combo-form-submit-btn" class="btn-cam-launch" style="width:100%; justify-content:center;">
                            💾 Guardar Paquete / Experiencia
                        </button>
                    </form>
                </div>

                <!-- Lista de Vales / Combos Activos -->
                <div class="bento-card" id="barista-combos-list-card">
                    <div class="bento-header" style="margin-bottom:12px;">
                        <h2 class="bento-title">✨ Paquetes Activos ({{ gift_combos|length if gift_combos is defined else 0 }})</h2>
                    </div>
                    <div style="display:flex; flex-direction:column; gap:10px;">
                        {% if gift_combos is defined and gift_combos %}
                            {% for cmb in gift_combos %}
                            <div style="display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; padding:13px 15px; border-radius:14px; background:#FAF6F3; border:1px solid var(--border-subtle);">
                                <div style="display:flex; align-items:center; gap:12px; min-width:210px; flex:1;">
                                    <div style="width:44px; height:44px; border-radius:12px; background:rgba(122,73,40,0.12); display:flex; align-items:center; justify-content:center; font-size:24px; flex-shrink:0;">
                                        {{ cmb.icon }}
                                    </div>
                                    <div style="min-width:0;">
                                        <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
                                            <strong style="font-size:14.5px; color:var(--text-primary);">{{ cmb.title }}</strong>
                                            <span style="padding:3px 9px; border-radius:99px; background:rgba(30,122,52,0.12); color:#1E7A34; font-weight:800; font-size:12px;">
                                                ${{ '%.0f'|format(cmb.price) }} MXN
                                            </span>
                                        </div>
                                        <div style="font-size:12.5px; color:var(--text-secondary); margin-top:3px;">
                                            {{ cmb.desc }}
                                        </div>
                                    </div>
                                </div>
                                <div style="display:flex; align-items:center; gap:6px;">
                                    <button type="button"
                                            class="action-chip"
                                            data-key="{{ cmb.key }}"
                                            data-icon="{{ cmb.icon }}"
                                            data-title="{{ cmb.title }}"
                                            data-desc="{{ cmb.desc }}"
                                            data-price="{{ '%.0f'|format(cmb.price) }}"
                                            onclick="editBaristaGiftCombo(this)"
                                            style="cursor:pointer; font-weight:800;">
                                        ✏️ Editar
                                    </button>
                                    {% if (gift_combos|length) > 1 %}
                                    <form action="/api/barista/gift-combos/delete" method="POST" style="margin:0;" onsubmit="saveBaristaUiState()">
                                        <input type="hidden" name="combo_key" value="{{ cmb.key }}">
                                        <button type="submit" class="btn-icon-sec btn-icon-del" title="Eliminar paquete">🗑️</button>
                                    </form>
                                    {% endif %}
                                </div>
                            </div>
                            {% endfor %}
                        {% else %}
                            <div style="text-align:center; padding:24px; color:var(--text-secondary); font-size:13px;">
                                No hay paquetes configurados.
                            </div>
                        {% endif %}
                    </div>
                </div>
            </div>
        </section>
    </main>

    <script>
        if (window.history.replaceState && window.location.search) {
            window.history.replaceState({}, document.title, window.location.pathname);
        }

        const toastEl = document.getElementById('status-toast');
        if (toastEl) {
            setTimeout(() => {
                toastEl.style.transition = 'opacity 0.4s ease';
                toastEl.style.opacity = '0';
                setTimeout(() => toastEl.style.display = 'none', 400);
            }, 6500);
        }

        function escapeHtml(str) {
            return String(str || '')
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }

        let currentMainView = 'pos';
        let activeTab = 'all';

        function saveBaristaUiState() {
            try {
                const searchEl = document.getElementById('client-search');
                sessionStorage.setItem('maishas_barista_ui_state', JSON.stringify({
                    view: currentMainView || 'pos',
                    filterTab: activeTab || 'all',
                    searchQuery: searchEl ? searchEl.value : '',
                    scrollY: window.scrollY || 0,
                    ts: Date.now()
                }));
            } catch (err) {}
        }

        // Protección anti doble clic en formularios de sellos y acciones + persistencia de estado UI
        document.addEventListener('submit', function(e) {
            const form = e.target;
            if (!form || form.tagName !== 'FORM') return;
            if (form.dataset.submitting === '1') {
                e.preventDefault();
                return false;
            }
            saveBaristaUiState();
            form.dataset.submitting = '1';
            const btn = form.querySelector('button[type="submit"]');
            if (btn) {
                btn.style.opacity = '0.7';
                btn.style.pointerEvents = 'none';
            }
            setTimeout(() => { form.dataset.submitting = '0'; }, 6000);
        });

        let baristaRefreshingInFlight = false;
        let lastBaristaRefreshTs = 0;

        async function refreshBaristaDataSilently(forceNow) {
            if (baristaRefreshingInFlight) return;
            const now = Date.now();
            if (!forceNow && (now - lastBaristaRefreshTs < 2500)) return;
            baristaRefreshingInFlight = true;
            lastBaristaRefreshTs = now;
            try {
                const res = await fetch('/barista?_bg=' + now, {
                    headers: { 'X-Requested-With': 'XMLHttpRequest', 'Cache-Control': 'no-cache' },
                    credentials: 'include'
                });
                if (!res.ok) return;
                const htmlText = await res.text();
                const doc = new DOMParser().parseFromString(htmlText, 'text/html');

                // 1. Actualizar contadores de pestañas superiores y barra KPI
                ['nav-btn-pos', 'nav-btn-gifts', 'nav-btn-combos', 'nav-btn-bday', 'nav-btn-db'].forEach(btnId => {
                    const curBtn = document.getElementById(btnId);
                    const newBtn = doc.getElementById(btnId);
                    if (curBtn && newBtn) curBtn.innerHTML = newBtn.innerHTML;
                });
                const curKpi = document.querySelector('.kpi-bar');
                const newKpi = doc.querySelector('.kpi-bar');
                if (curKpi && newKpi) curKpi.innerHTML = newKpi.innerHTML;

                // 2. Actualizar lista de clientes y filtros en Mostrador POS
                const curFilterBar = document.getElementById('filter-tabs-bar');
                const newFilterBar = doc.getElementById('filter-tabs-bar');
                if (curFilterBar && newFilterBar) {
                    curFilterBar.innerHTML = newFilterBar.innerHTML;
                    curFilterBar.querySelectorAll('.filter-pill').forEach(btn => {
                        btn.classList.toggle('active', btn.getAttribute('data-tab') === activeTab);
                    });
                }
                const curClients = document.getElementById('clients-list-container');
                const newClients = doc.getElementById('clients-list-container');
                if (curClients && newClients) {
                    curClients.innerHTML = newClients.innerHTML;
                    filterClients();
                }

                // 3. Actualizar lista de Gift Cards y selector de paquetes
                const curGiftsCard = document.getElementById('barista-gift-cards-list-card');
                const newGiftsCard = doc.getElementById('barista-gift-cards-list-card');
                if (curGiftsCard && newGiftsCard) {
                    curGiftsCard.innerHTML = newGiftsCard.innerHTML;
                }
                const curPkgSel = document.getElementById('bgc-package-select');
                const newPkgSel = doc.getElementById('bgc-package-select');
                if (curPkgSel && newPkgSel && document.activeElement !== curPkgSel) {
                    const prevVal = curPkgSel.value;
                    curPkgSel.innerHTML = newPkgSel.innerHTML;
                    if (prevVal && Array.from(curPkgSel.options).some(o => o.value === prevVal)) {
                        curPkgSel.value = prevVal;
                    }
                }

                // 4. Actualizar lista de Vales / Combos
                const curCombosCard = document.getElementById('barista-combos-list-card');
                const newCombosCard = doc.getElementById('barista-combos-list-card');
                if (curCombosCard && newCombosCard) {
                    curCombosCard.innerHTML = newCombosCard.innerHTML;
                }

                // 5. Actualizar Cumpleañeros y Base de Datos si no hay un campo en edición activa
                const activeEl = document.activeElement;
                const isTypingInForm = activeEl && ['INPUT', 'TEXTAREA', 'SELECT'].includes(activeEl.tagName) && activeEl.id !== 'client-search' && activeEl.id !== 'scan-serial-input';
                if (!isTypingInForm) {
                    const curBday = document.getElementById('view-bday-container');
                    const newBday = doc.getElementById('view-bday-container');
                    if (curBday && newBday) curBday.innerHTML = newBday.innerHTML;

                    const curDb = document.getElementById('view-db-container');
                    const newDb = doc.getElementById('view-db-container');
                    if (curDb && newDb) {
                        curDb.innerHTML = newDb.innerHTML;
                        filterClients();
                    }
                }
            } catch (e) {
                // Ignorar errores transitorios de red
            } finally {
                baristaRefreshingInFlight = false;
            }
        }

        // Cambio entre las 7 pestañas principales con actualización automática de datos en tiempo real
        function switchMainView(view, skipAutoRefresh) {
            currentMainView = view || 'pos';
            const views = {
                pos: document.getElementById('view-pos-container'),
                gifts: document.getElementById('view-gifts-container'),
                combos: document.getElementById('view-combos-container'),
                campaign: document.getElementById('view-campaign-container'),
                bday: document.getElementById('view-bday-container'),
                push: document.getElementById('view-push-container'),
                db: document.getElementById('view-db-container')
            };
            const btns = {
                pos: document.getElementById('nav-btn-pos'),
                gifts: document.getElementById('nav-btn-gifts'),
                combos: document.getElementById('nav-btn-combos'),
                campaign: document.getElementById('nav-btn-campaign'),
                bday: document.getElementById('nav-btn-bday'),
                push: document.getElementById('nav-btn-push'),
                db: document.getElementById('nav-btn-db')
            };
            Object.keys(views).forEach(k => {
                if (views[k]) views[k].style.display = (k === view) ? 'block' : 'none';
                if (btns[k]) btns[k].classList.toggle('active', k === view);
            });
            if (view !== 'pos' && typeof scanning !== 'undefined' && scanning) {
                stopScanner();
            }
            if (view === 'campaign' && typeof renderCampaignLivePreview === 'function') {
                renderCampaignLivePreview();
            }
            if (!skipAutoRefresh) {
                refreshBaristaDataSilently(true);
            }
        }

        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'visible') {
                refreshBaristaDataSilently(false);
            }
        });
        setInterval(() => {
            if (document.visibilityState === 'visible') {
                refreshBaristaDataSilently(false);
            }
        }, 12000);

        // --- Funciones de Operación de Gift Cards y Catálogo de Vales / Combos en Mostrador POS ---
        let activeGiftModalBalance = 0;

        function toggleBaristaGiftCreateType(gType) {
            document.getElementById('bgc-type-input').value = gType;
            document.getElementById('bgc-tab-balance').classList.toggle('active', gType === 'balance');
            document.getElementById('bgc-tab-combo').classList.toggle('active', gType === 'combo');
            document.getElementById('bgc-pane-balance').style.display = (gType === 'balance') ? 'block' : 'none';
            document.getElementById('bgc-pane-combo').style.display = (gType === 'combo') ? 'block' : 'none';
        }

        function editBaristaGiftCombo(btnEl) {
            if (!btnEl) return;
            document.getElementById('combo-form-key').value = btnEl.dataset.key || '';
            document.getElementById('combo-form-icon').value = btnEl.dataset.icon || '🥐';
            document.getElementById('combo-form-name').value = btnEl.dataset.title || '';
            document.getElementById('combo-form-desc').value = btnEl.dataset.desc || '';
            document.getElementById('combo-form-price').value = btnEl.dataset.price || '';
            document.getElementById('combo-form-title').textContent = '✏️ Editar Paquete / Experiencia';
            document.getElementById('combo-form-submit-btn').textContent = '💾 Actualizar Paquete / Experiencia';
            document.getElementById('combo-form-cancel-btn').style.display = 'inline-block';
            document.getElementById('combo-form-name').focus();
        }

        function resetBaristaGiftComboForm() {
            document.getElementById('combo-form-key').value = '';
            document.getElementById('combo-form-icon').value = '🥐';
            document.getElementById('combo-form-name').value = '';
            document.getElementById('combo-form-desc').value = '';
            document.getElementById('combo-form-price').value = '';
            document.getElementById('combo-form-title').textContent = '➕ Agregar Nuevo Vale / Combo';
            document.getElementById('combo-form-submit-btn').textContent = '💾 Guardar Paquete / Experiencia';
            document.getElementById('combo-form-cancel-btn').style.display = 'none';
        }

        async function copyGiftUrlFromBarista(btnEl) {
            if (!btnEl) return;
            const url = btnEl.dataset.giftUrl || (window.location.origin + '/gift/' + (btnEl.dataset.code || ''));
            const origText = btnEl.innerHTML;
            try {
                await navigator.clipboard.writeText(url);
                btnEl.innerHTML = '✅ ¡Link Copiado!';
            } catch (e) {
                const ta = document.createElement('textarea');
                ta.value = url;
                document.body.appendChild(ta);
                ta.select();
                document.execCommand('copy');
                document.body.removeChild(ta);
                btnEl.innerHTML = '✅ ¡Link Copiado!';
            }
            setTimeout(() => { btnEl.innerHTML = origText; }, 2200);
        }

        function openGiftPosModalByObj(g) {
            if (!g || !g.code) return;
            activeGiftModalBalance = parseFloat(g.current_balance || 0);
            document.getElementById('gpos-title').textContent = 'Gift Card ' + g.code;
            document.getElementById('gpos-sub').innerHTML = 'Para: <strong>' + escapeHtml(g.recipient_name || '') + '</strong> · De parte de: <strong>' + escapeHtml(g.sender_name || '') + '</strong>';
            document.getElementById('gpos-type-lbl').textContent = (g.gift_type === 'combo') ? 'VALE / EXPERIENCIA' : 'SALDO DISPONIBLE';
            document.getElementById('gpos-headline').textContent = g.value_headline || ('$' + activeGiftModalBalance.toFixed(2) + ' MXN');
            document.getElementById('gpos-desc').textContent = g.package_desc || '';

            const pill = document.getElementById('gpos-status-pill');
            if (g.status === 'active') {
                pill.textContent = '🟢 Activa';
                pill.style.background = 'rgba(30,122,52,0.14)';
                pill.style.color = '#1E7A34';
            } else if (g.status === 'redeemed') {
                pill.textContent = '🎉 Canjeada';
                pill.style.background = 'rgba(74,93,35,0.14)';
                pill.style.color = '#4A5D23';
            } else {
                pill.textContent = '⏳ Por Activar';
                pill.style.background = 'rgba(184,98,0,0.16)';
                pill.style.color = '#B86200';
            }

            document.getElementById('gpos-form-code-activate').value = g.code;
            document.getElementById('gpos-form-code-deduct').value = g.code;
            document.getElementById('gpos-form-code-redeem').value = g.code;

            const giftFullUrl = g.gift_url || (window.location.origin + '/gift/' + encodeURIComponent(g.code));
            const waText = 'Hola! 🎁✨ ' + (g.sender_name || 'Alguien especial') + ' te envió un regalo de {{ campaign.shopName }} (' + (g.value_headline || '') + '). Abre tu Gift Card y guárdala en tu Wallet aquí: ' + giftFullUrl;
            const waShareUrl = g.whatsapp_share_url || ('https://api.whatsapp.com/send?text=' + encodeURIComponent(waText));

            document.getElementById('gpos-unbox-link').href = '/gift/' + encodeURIComponent(g.code);
            const waLinkEl = document.getElementById('gpos-wa-share-link');
            if (waLinkEl) waLinkEl.href = waShareUrl;
            const copyBtnEl = document.getElementById('gpos-copy-link-btn');
            if (copyBtnEl) {
                copyBtnEl.dataset.giftUrl = giftFullUrl;
                copyBtnEl.dataset.code = g.code;
            }

            document.getElementById('gpos-pane-pending').style.display = (g.status === 'pending') ? 'block' : 'none';
            document.getElementById('gpos-pane-balance').style.display = (g.status === 'active' && g.gift_type !== 'combo' && activeGiftModalBalance > 0) ? 'block' : 'none';
            document.getElementById('gpos-pane-combo').style.display = (g.status === 'active' && g.gift_type === 'combo') ? 'block' : 'none';

            const deductInput = document.getElementById('gpos-deduct-input');
            if (deductInput) {
                deductInput.max = String(activeGiftModalBalance);
                deductInput.value = '';
            }
            document.getElementById('maishas-gift-pos-modal').classList.add('open');
        }

        function openGiftPosModalFromBtn(btnEl) {
            if (!btnEl) return;
            openGiftPosModalByObj({
                code: btnEl.dataset.code || '',
                gift_type: btnEl.dataset.type || 'balance',
                status: btnEl.dataset.status || 'active',
                value_headline: btnEl.dataset.headline || '',
                package_desc: btnEl.dataset.desc || '',
                current_balance: parseFloat(btnEl.dataset.balance || '0'),
                recipient_name: btnEl.dataset.recipient || '',
                sender_name: btnEl.dataset.sender || '',
                whatsapp_share_url: btnEl.dataset.waUrl || '',
                gift_url: btnEl.dataset.giftUrl || ''
            });
        }

        function setGiftDeductAmount(val) {
            const inp = document.getElementById('gpos-deduct-input');
            if (!inp) return;
            const capped = activeGiftModalBalance > 0 ? Math.min(val, activeGiftModalBalance) : val;
            inp.value = capped;
            inp.focus();
        }

        function setGiftDeductMax() {
            const inp = document.getElementById('gpos-deduct-input');
            if (!inp) return;
            inp.value = activeGiftModalBalance.toFixed(2);
            inp.focus();
        }

        function closeGiftPosModal() {
            const m = document.getElementById('maishas-gift-pos-modal');
            if (m) m.classList.remove('open');
        }

        // Modal de Edición de Cliente
        function openEditClientModal(btnEl) {
            if (!btnEl) return;
            document.getElementById('edit-client-serial').value = btnEl.dataset.serial || '';
            document.getElementById('edit-client-name').value = btnEl.dataset.name || '';
            document.getElementById('edit-client-phone').value = btnEl.dataset.phone || '';
            document.getElementById('edit-client-email').value = btnEl.dataset.email || '';
            document.getElementById('edit-client-birthdate').value = btnEl.dataset.birthdate || '';
            document.getElementById('maishas-edit-modal').classList.add('open');
        }

        function closeEditClientModal() {
            const modal = document.getElementById('maishas-edit-modal');
            if (modal) modal.classList.remove('open');
        }

        // Modal de PIN Temporal de Recuperación (4 dígitos · Válido 10 min)
        let activePinClient = { serial: '', name: '', phone: '' };

        async function openRecoveryPinModal(btnEl) {
            if (!btnEl) return;
            activePinClient = {
                serial: btnEl.dataset.serial || '',
                name: btnEl.dataset.name || 'Cliente',
                phone: btnEl.dataset.phone || ''
            };
            document.getElementById('pin-modal-client-name').textContent = activePinClient.name;
            document.getElementById('pin-modal-client-serial').textContent = activePinClient.serial;
            document.getElementById('pin-modal-digits').textContent = '••••';
            document.getElementById('pin-modal-status').textContent = 'Generando código seguro de 4 dígitos...';
            document.getElementById('pin-modal-wa-link').style.display = 'none';
            document.getElementById('maishas-pin-modal').classList.add('open');
            await regenerateRecoveryPin();
        }

        async function regenerateRecoveryPin() {
            if (!activePinClient.serial) return;
            const digitsEl = document.getElementById('pin-modal-digits');
            const statusEl = document.getElementById('pin-modal-status');
            const waLink = document.getElementById('pin-modal-wa-link');
            digitsEl.textContent = '••••';
            statusEl.textContent = 'Generando PIN...';
            try {
                const res = await fetch('/api/barista/recovery-pin', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ serial: activePinClient.serial })
                });
                const data = await res.json();
                if (res.ok && data.ok && data.pin) {
                    digitsEl.textContent = data.pin;
                    statusEl.textContent = '⏳ Válido por 10 minutos · Un solo uso';
                    const rawDigits = (data.phone || activePinClient.phone || '').replace(/\\D+/g, '');
                    if (rawDigits.length >= 10) {
                        const ten = rawDigits.slice(-10);
                        const msg = encodeURIComponent(`Hola ${activePinClient.name} ☕️ Tu PIN temporal de seguridad para recuperar tu tarjeta de Demo Café es: *${data.pin}* (válido por 10 minutos).`);
                        waLink.href = `https://wa.me/52${ten}?text=${msg}`;
                        waLink.style.display = 'inline-flex';
                    } else {
                        waLink.style.display = 'none';
                    }
                } else {
                    digitsEl.textContent = 'ERR';
                    statusEl.textContent = data.error || 'No se pudo generar el PIN.';
                }
            } catch (e) {
                digitsEl.textContent = 'ERR';
                statusEl.textContent = 'Error de red al generar PIN.';
            }
        }

        function closeRecoveryPinModal() {
            const modal = document.getElementById('maishas-pin-modal');
            if (modal) modal.classList.remove('open');
        }

        // Modal de Confirmación Personalizado (Sin URL de navegador)
        let pendingFormId = null;
        const CURRENT_STAMP_GOAL = {{ campaign.stampGoal }};
        const SERVER_RENDER_EPOCH = {{ server_now_epoch }};
        const CLIENT_PAGE_LOAD_MS = Date.now();
        const STAMP_COOLDOWN_SECONDS = 60;

        function getCurrentServerEpoch() {
            const elapsedSec = Math.max(0, (Date.now() - CLIENT_PAGE_LOAD_MS) / 1000);
            return SERVER_RENDER_EPOCH + elapsedSec;
        }

        function openConfirmModal(actionType, formId, clientName, serial, currentStamps, extraSeconds) {
            pendingFormId = formId;
            const safeName = escapeHtml(clientName);
            const safeSerial = escapeHtml(serial);
            const modal = document.getElementById('maishas-confirm-modal');
            const iconBox = document.getElementById('cm-icon-box');
            const titleEl = document.getElementById('cm-title-text');
            const descEl = document.getElementById('cm-desc-text');
            const confirmBtn = document.getElementById('cm-confirm-btn');

            if (actionType === 'duplicate_add') {
                const secsAgo = Math.max(1, Math.round(Number(extraSeconds) || 1));
                const secWord = (secsAgo === 1) ? 'segundo' : 'segundos';
                iconBox.className = 'cm-icon warn';
                iconBox.textContent = '⚠️';
                titleEl.textContent = 'Protección Anti-Doble Sello';
                descEl.innerHTML = '⚠️ Ya se le sumó 1 sello a <strong>' + safeName + '</strong> hace <strong>' + secsAgo + ' ' + secWord + '</strong>. ¿Deseas sumarle un segundo sello?';
                confirmBtn.className = 'cm-btn-confirm warn';
                confirmBtn.textContent = 'Sí, sumar segundo sello';
            } else if (actionType === 'delete') {
                iconBox.className = 'cm-icon warn';
                iconBox.textContent = '📦';
                titleEl.textContent = '¿Desactivar cliente y enviar a Papelera?';
                descEl.innerHTML = 'El cliente <strong>' + safeName + '</strong> (' + safeSerial + ') se ocultará del Mostrador POS y no recibirá notificaciones Push, pero <strong>conservará sus ' + currentStamps + ' sellos e historial</strong> en la sección <em>📦 Papelera</em> de Base de Datos por si deseas reactivarlo.';
                confirmBtn.className = 'cm-btn-confirm warn';
                confirmBtn.textContent = 'Sí, desactivar';
            } else if (actionType === 'reactivate') {
                iconBox.className = 'cm-icon success';
                iconBox.textContent = '♻️';
                titleEl.textContent = '¿Reactivar cliente?';
                descEl.innerHTML = '<strong>' + safeName + '</strong> (' + safeSerial + ') volverá al Mostrador POS con sus <strong>' + currentStamps + ' / ' + CURRENT_STAMP_GOAL + '</strong> sellos intactos.';
                confirmBtn.className = 'cm-btn-confirm success';
                confirmBtn.textContent = 'Sí, reactivar';
            } else if (actionType === 'purge') {
                iconBox.className = 'cm-icon danger';
                iconBox.textContent = '🗑️';
                titleEl.textContent = '¿Eliminar definitivamente del sistema?';
                descEl.innerHTML = 'Estás por eliminar definitivamente a <strong>' + safeName + '</strong> (' + safeSerial + '). Esta acción no se puede deshacer y el cliente ya no estará disponible en ninguna parte del sistema.';
                confirmBtn.className = 'cm-btn-confirm danger';
                confirmBtn.textContent = 'Sí, eliminar definitivamente';
            } else if (actionType === 'sub') {
                const nextStamps = Math.max(0, currentStamps - 1);
                iconBox.className = 'cm-icon warn';
                iconBox.textContent = '☕️';
                titleEl.textContent = '¿Corregir sellos (-1)?';
                descEl.innerHTML = '¿Estás seguro de quitar 1 sello a <strong>' + safeName + '</strong>? Su tarjeta pasará de <strong>' + currentStamps + ' / ' + CURRENT_STAMP_GOAL + '</strong> a <strong>' + nextStamps + ' / ' + CURRENT_STAMP_GOAL + '</strong> sellos.';
                confirmBtn.className = 'cm-btn-confirm warn';
                confirmBtn.textContent = 'Sí, restar 1 sello';
            } else if (actionType === 'redeem') {
                iconBox.className = 'cm-icon success';
                iconBox.textContent = '🎁';
                titleEl.textContent = '¿Canjear Recompensa Gratis?';
                descEl.innerHTML = 'Se entregará la recompensa a <strong>' + safeName + '</strong> y su tarjeta se reiniciará a <strong>0 / ' + CURRENT_STAMP_GOAL + '</strong> sellos.';
                confirmBtn.className = 'cm-btn-confirm success';
                confirmBtn.textContent = 'Sí, canjear';
            }

            modal.classList.add('open');
        }

        function closeConfirmModal() {
            if (pendingFormId) {
                const form = document.getElementById(pendingFormId);
                if (form) {
                    form.dataset.submitting = '0';
                    const btn = form.querySelector('button[type="submit"]');
                    if (btn) {
                        btn.style.opacity = '1';
                        btn.style.pointerEvents = 'auto';
                    }
                }
            }
            pendingFormId = null;
            document.getElementById('maishas-confirm-modal').classList.remove('open');
        }

        function executeConfirmedForm() {
            if (pendingFormId) {
                const form = document.getElementById(pendingFormId);
                if (form) {
                    const dupInput = form.querySelector('input[name="force_duplicate"]');
                    if (dupInput) dupInput.value = '1';
                    form.dataset.submitting = '0';
                    saveBaristaUiState();
                    form.submit();
                }
            }
            pendingFormId = null;
            document.getElementById('maishas-confirm-modal').classList.remove('open');
        }

        // Protección Anti-Doble Escaneo / Sello Duplicado (< 60 segundos) en Mostrador POS y Escáner QR
        function checkDuplicateStampBeforeSubmit(e, formEl) {
            if (!formEl) return true;
            const forceInput = formEl.querySelector('input[name="force_duplicate"]');
            if (forceInput && forceInput.value === '1') return true;

            const lastAddEpoch = parseFloat(formEl.dataset.lastAddEpoch || '0');
            if (lastAddEpoch > 0) {
                const diffSec = getCurrentServerEpoch() - lastAddEpoch;
                if (diffSec >= 0 && diffSec < STAMP_COOLDOWN_SECONDS) {
                    if (e && e.preventDefault) e.preventDefault();
                    if (e && e.stopImmediatePropagation) e.stopImmediatePropagation();
                    const secsAgo = Math.max(1, Math.round(diffSec));
                    openConfirmModal(
                        'duplicate_add',
                        formEl.id,
                        formEl.dataset.name || 'Cliente',
                        formEl.dataset.serial || '',
                        parseInt(formEl.dataset.stamps || '0', 10),
                        secsAgo
                    );
                    return false;
                }
            }
            return true;
        }

        function checkDuplicateScanBeforeSubmit(e, scanFormEl) {
            if (!scanFormEl) return true;
            const forceInput = document.getElementById('scan-force-dup');
            if (forceInput && forceInput.value === '1') return true;

            const rawVal = (document.getElementById('scan-serial-input').value || '').trim().toUpperCase();
            const m = rawVal.match(/MAISHAS-[A-Z0-9]{6}/);
            const normSerial = m ? m[0] : (/^[A-Z0-9]{6}$/.test(rawVal) ? ('MAISHAS-' + rawVal) : rawVal);
            const rowForm = document.getElementById('form-add-' + normSerial);
            if (rowForm) {
                const lastAddEpoch = parseFloat(rowForm.dataset.lastAddEpoch || '0');
                if (lastAddEpoch > 0) {
                    const diffSec = getCurrentServerEpoch() - lastAddEpoch;
                    if (diffSec >= 0 && diffSec < STAMP_COOLDOWN_SECONDS) {
                        if (e && e.preventDefault) e.preventDefault();
                        if (e && e.stopImmediatePropagation) e.stopImmediatePropagation();
                        const secsAgo = Math.max(1, Math.round(diffSec));
                        openConfirmModal(
                            'duplicate_add',
                            scanFormEl.id,
                            rowForm.dataset.name || normSerial,
                            normSerial,
                            parseInt(rowForm.dataset.stamps || '0', 10),
                            secsAgo
                        );
                        return false;
                    }
                }
            }
            return true;
        }

        // Control del Check de Envío Automático de Cumpleaños a las 8:00 AM (Hora Sonora)
        async function toggleBirthdayAutoSetting(enabled) {
            const pill = document.getElementById('bday-auto-status-pill');
            const txt = document.getElementById('bday-auto-check-text');
            const fb = document.getElementById('bday-auto-feedback');
            const tplInput = document.getElementById('bday-auto-tpl-input');
            try {
                const res = await fetch('/api/birthday-auto-settings', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    credentials: 'include',
                    body: JSON.stringify({
                        enabled: Boolean(enabled),
                        template: tplInput ? tplInput.value : ''
                    })
                });
                const data = await res.json();
                if (!res.ok || !data.ok) throw new Error(data.error || 'No se pudo actualizar');

                if (pill) {
                    pill.className = data.enabled ? 'status-pill-on' : 'status-pill-off';
                    pill.style.cssText = data.enabled ? '' : 'color:#9A5000; border-color:rgba(184,98,0,0.35);';
                    pill.textContent = data.enabled
                        ? '🟢 Envío Automático Activo (8:00 AM)'
                        : '⏸️ Automático Desactivado (Solo manual)';
                }
                if (txt) {
                    txt.textContent = data.enabled
                        ? 'Función automática activada'
                        : 'Activar función automática';
                }
                if (fb) {
                    fb.style.display = 'block';
                    fb.style.color = data.enabled ? '#1E7A34' : '#9A5000';
                    fb.textContent = data.message || (data.enabled
                        ? '✅ Envío automático de cumpleaños activado (8:00 AM Hora Sonora).'
                        : '⏸️ Envío automático desactivado. Solo se enviarán felicitaciones cuando presiones el botón manualmente.');
                    setTimeout(() => { fb.style.display = 'none'; }, 5000);
                }
            } catch (err) {
                if (fb) {
                    fb.style.display = 'block';
                    fb.style.color = '#C81D40';
                    fb.textContent = '⚠️ Error al guardar: ' + err.message;
                }
            }
        }

        async function saveBirthdayAutoTemplate() {
            const chk = document.getElementById('bday-auto-check');
            await toggleBirthdayAutoSetting(chk ? chk.checked : true);
        }

        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                closeConfirmModal();
                closeEditClientModal();
            }
            if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
                e.preventDefault();
                const searchInput = document.getElementById('client-search');
                if (searchInput) {
                    searchInput.focus();
                    searchInput.select();
                }
            }
        });

        function updateLockScreenPreview(val) {
            const preview = document.getElementById('ios-preview-text');
            const clean = (val || '').trim();
            preview.textContent = clean ? ('📣 ' + clean) : '📣 Escribe un mensaje para previsualizar...';
        }

        function setTemplate(text) {
            const box = document.getElementById('push-msg-box');
            if (box) {
                box.value = text;
                updateLockScreenPreview(text);
                box.focus();
            }
        }

        function selectClientForPush(serial, name) {
            switchMainView('push');
            const sel = document.getElementById('push-target-select');
            const box = document.getElementById('push-msg-box');
            if (sel) sel.value = serial;
            if (box) {
                box.focus();
                window.scrollTo({ top: 0, behavior: 'smooth' });
            }
        }

        function switchMktTab(tab) {
            const pushPane = document.getElementById('mkt-pane-push');
            const gpsPane = document.getElementById('mkt-pane-gps');
            const btnPush = document.getElementById('tab-btn-push');
            const btnGps = document.getElementById('tab-btn-gps');
            const title = document.getElementById('mkt-card-title');
            if (tab === 'gps') {
                pushPane.style.display = 'none';
                gpsPane.style.display = 'block';
                btnPush.classList.remove('active');
                btnGps.classList.add('active');
                title.textContent = '📍 Geolocalización GPS';
            } else {
                pushPane.style.display = 'block';
                gpsPane.style.display = 'none';
                btnPush.classList.add('active');
                btnGps.classList.remove('active');
                title.textContent = '📣 Marketing Push';
            }
        }

        function setFilterTab(tab) {
            activeTab = tab;
            document.querySelectorAll('.filter-pill').forEach(btn => {
                btn.classList.toggle('active', btn.getAttribute('data-tab') === tab);
            });
            filterClients();
        }

        function filterClients() {
            const q = (document.getElementById('client-search').value || '').toLowerCase().trim();
            const rows = document.querySelectorAll('.client-row');
            rows.forEach(row => {
                const searchTxt = row.getAttribute('data-search') || '';
                const matchesQuery = !q || searchTxt.includes(q);
                let matchesTab = true;
                if (activeTab === 'installed') matchesTab = row.getAttribute('data-installed') === '1';
                else if (activeTab === 'ready') matchesTab = row.getAttribute('data-ready') === '1';
                else if (activeTab === 'bday') matchesTab = row.getAttribute('data-bday') === '1';
                else if (activeTab === 'vip') matchesTab = row.getAttribute('data-vip') === '1';
                row.style.display = (matchesQuery && matchesTab) ? '' : 'none';
            });

            const dbRows = document.querySelectorAll('.db-client-row');
            dbRows.forEach(row => {
                const searchTxt = row.getAttribute('data-search') || '';
                row.style.display = (!q || searchTxt.includes(q)) ? '' : 'none';
            });
        }

        // --- LÓGICA DEL DASHBOARD DE ADMINISTRACIÓN DE LA TARJETA / CAMPAÑA ---
        let caPreviewStamps = 3;
        let caPreviewShowBack = false;
        let caUploadedLogoDataUrl = '';
        let caUploadedStripDataUrl = {% if campaign.hasCustomStrip %}'/api/campaign/strip-bg.png?v={{ campaign.stripVersion }}'{% else %}''{% endif %};

        function switchCampaignSubTab(sub) {
            const panes = {
                brand: document.getElementById('ca-pane-brand'),
                rules: document.getElementById('ca-pane-rules'),
                location: document.getElementById('ca-pane-location')
            };
            const btns = {
                brand: document.getElementById('ca-tab-btn-brand'),
                rules: document.getElementById('ca-tab-btn-rules'),
                location: document.getElementById('ca-tab-btn-location')
            };
            Object.keys(panes).forEach(k => {
                if (panes[k]) panes[k].style.display = (k === sub) ? 'block' : 'none';
                if (btns[k]) btns[k].classList.toggle('active', k === sub);
            });
        }

        function syncColorPair(which, val) {
            const clean = (val || '').trim();
            if (which === 'bg') {
                document.getElementById('ca-bg-hex').value = clean;
                if (/^#[0-9A-Fa-f]{6}$/.test(clean)) document.getElementById('ca-bg-picker').value = clean;
            } else if (which === 'fg') {
                document.getElementById('ca-fg-hex').value = clean;
                if (/^#[0-9A-Fa-f]{6}$/.test(clean)) document.getElementById('ca-fg-picker').value = clean;
            } else if (which === 'lbl') {
                document.getElementById('ca-lbl-hex').value = clean;
                if (/^#[0-9A-Fa-f]{6}$/.test(clean)) document.getElementById('ca-lbl-picker').value = clean;
            }
            renderCampaignLivePreview();
        }

        function setCampaignGoal(goalNum) {
            const el = document.getElementById('ca-stamp-goal');
            if (el) el.value = goalNum;
            onCampaignFieldInput();
        }

        function setPreviewStamps(mode) {
            const goal = Math.max(4, Math.min(12, parseInt(document.getElementById('ca-stamp-goal').value || '10', 10)));
            if (mode === 'goal') caPreviewStamps = goal;
            else caPreviewStamps = Math.min(goal, parseInt(mode, 10) || 0);
            renderCampaignLivePreview();
        }

        function toggleCampaignPreviewSide() {
            caPreviewShowBack = !caPreviewShowBack;
            document.getElementById('ca-preview-front').style.display = caPreviewShowBack ? 'none' : 'block';
            document.getElementById('ca-preview-back').style.display = caPreviewShowBack ? 'block' : 'none';
            document.getElementById('ca-flip-btn').textContent = caPreviewShowBack ? '← Frente' : '🔄 Reverso';
        }

        function handleCampaignDrop(evt, kind) {
            evt.preventDefault();
            evt.currentTarget.style.borderColor = 'rgba(78,48,27,0.32)';
            const file = evt.dataTransfer && evt.dataTransfer.files && evt.dataTransfer.files[0];
            if (file) handleCampaignFileSelect(file, kind);
        }

        function handleCampaignFileSelect(file, kind) {
            if (!file) return;
            const fb = document.getElementById(kind === 'logo' ? 'ca-logo-feedback' : 'ca-strip-feedback');
            if (!file.type.startsWith('image/')) {
                fb.style.display = 'block';
                fb.style.background = 'rgba(217,56,72,0.14)';
                fb.style.color = '#C81D40';
                fb.textContent = '⚠️ El archivo debe ser una imagen PNG, JPG o WEBP.';
                return;
            }
            const reader = new FileReader();
            reader.onload = function(e) {
                const dataUrl = e.target.result;
                const img = new Image();
                img.onload = function() {
                    const ratio = img.width / img.height;
                    fb.style.display = 'block';
                    if (kind === 'logo') {
                        caUploadedLogoDataUrl = dataUrl;
                        document.getElementById('ca-logo-thumb').src = dataUrl;
                        document.getElementById('ca-prev-logo').src = dataUrl;
                        if (ratio >= 0.7 && ratio <= 1.4) {
                            fb.style.background = 'rgba(52,199,89,0.15)';
                            fb.style.color = '#1E7A34';
                            fb.textContent = `✅ Logo válido (${img.width}×${img.height}px · proporción ${ratio.toFixed(2)}:1)`;
                        } else {
                            fb.style.background = 'rgba(184,98,0,0.14)';
                            fb.style.color = '#9A5000';
                            fb.textContent = `⚠️ Cargado (${img.width}×${img.height}px, ${ratio.toFixed(2)}:1). Se recomienda formato cuadrado 1:1.`;
                        }
                    } else {
                        caUploadedStripDataUrl = dataUrl;
                        document.getElementById('ca-use-custom-strip').checked = true;
                        if (ratio >= 2.2 && ratio <= 4.0) {
                            fb.style.background = 'rgba(52,199,89,0.15)';
                            fb.style.color = '#1E7A34';
                            fb.textContent = `✅ Strip válido (${img.width}×${img.height}px · proporción ${ratio.toFixed(2)}:1)`;
                        } else {
                            fb.style.background = 'rgba(184,98,0,0.14)';
                            fb.style.color = '#9A5000';
                            fb.textContent = `⚠️ Cargado (${img.width}×${img.height}px, ${ratio.toFixed(2)}:1). Proporción óptima Apple Wallet: ~3:1 (1125×432px).`;
                        }
                    }
                    renderCampaignLivePreview();
                };
                img.src = dataUrl;
            };
            reader.readAsDataURL(file);
        }

        function clearCustomStripBg() {
            caUploadedStripDataUrl = '';
            document.getElementById('ca-use-custom-strip').checked = false;
            const fb = document.getElementById('ca-strip-feedback');
            fb.style.display = 'block';
            fb.style.background = 'rgba(74,93,35,0.12)';
            fb.style.color = '#4A5D23';
            fb.textContent = '✨ Usando fondo generado dinámicamente con los colores de tu marca.';
            renderCampaignLivePreview();
        }

        function detectCampaignGPS() {
            if (!navigator.geolocation) return;
            navigator.geolocation.getCurrentPosition(pos => {
                document.getElementById('ca-lat').value = pos.coords.latitude.toFixed(6);
                document.getElementById('ca-lng').value = pos.coords.longitude.toFixed(6);
                onCampaignFieldInput();
            }, () => {}, { enableHighAccuracy: true });
        }

        function onCampaignFieldInput() {
            renderCampaignLivePreview();
        }

        function renderCampaignLivePreview() {
            const shopName = (document.getElementById('ca-shop-name').value || 'Demo Café').trim();
            const bg = document.getElementById('ca-bg-hex').value || '#12100E';
            const fg = document.getElementById('ca-fg-hex').value || '#F5F2EB';
            const lbl = document.getElementById('ca-lbl-hex').value || '#D4AF37';
            const goal = Math.max(4, Math.min(12, parseInt(document.getElementById('ca-stamp-goal').value || '10', 10)));
            const rewardText = (document.getElementById('ca-reward-text').value || '1 Café Gratis').trim();
            const menuUrl = (document.getElementById('ca-menu-url').value || '').trim();
            const termsText = (document.getElementById('ca-terms-text').value || '').trim();
            const lat = (document.getElementById('ca-lat').value || '').trim();
            const lng = (document.getElementById('ca-lng').value || '').trim();
            const relevantText = (document.getElementById('ca-relevant-text').value || '').trim();
            const useCustomStrip = document.getElementById('ca-use-custom-strip').checked;

            if (caPreviewStamps > goal) caPreviewStamps = goal;

            const card = document.getElementById('ca-wallet-card');
            card.style.background = bg;
            card.style.color = fg;

            document.getElementById('ca-prev-shop-name').textContent = shopName.toUpperCase();
            document.getElementById('ca-prev-hdr-lbl').style.color = lbl;
            document.getElementById('ca-prev-hdr-val').textContent = `${caPreviewStamps} / ${goal}`;

            const stripBox = document.getElementById('ca-prev-strip-box');
            if (useCustomStrip && caUploadedStripDataUrl) {
                stripBox.style.backgroundImage = `linear-gradient(rgba(12,10,9,0.58), rgba(12,10,9,0.68)), url(${caUploadedStripDataUrl})`;
            } else {
                stripBox.style.backgroundImage = 'none';
                stripBox.style.backgroundColor = 'rgba(0,0,0,0.28)';
            }

            document.getElementById('ca-prev-strip-title').style.color = lbl;
            document.getElementById('ca-prev-strip-status').style.color = fg;
            document.getElementById('ca-prev-strip-status').textContent = caPreviewStamps >= goal ? '¡META COMPLETADA! 🎉' : `${caPreviewStamps}/${goal} SELLOS`;
            document.getElementById('ca-prev-strip-footer').style.color = lbl;
            document.getElementById('ca-prev-strip-footer').textContent = `Premio: ${rewardText}`;

            // Renderizar círculos dinámicos según la meta (4..12) con grano de café vertical simétrico
            const grid = document.getElementById('ca-prev-stamps-grid');
            const cols = goal <= 4 ? 4 : (goal <= 6 ? 3 : (goal <= 8 ? 4 : (goal === 9 ? 5 : (goal === 10 ? 5 : 6))));
            grid.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;
            const beanSvgSim = `<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M10.95 3.12 C7.35 3.85 5.35 7.45 5.35 12.0 C5.35 16.45 7.25 19.95 10.75 20.82 C12.95 15.65 8.55 8.25 10.95 3.12 Z"/><path d="M13.05 20.88 C16.65 20.15 18.65 16.55 18.65 12.0 C18.65 7.55 16.75 4.05 13.25 3.18 C11.05 8.35 15.45 15.75 13.05 20.88 Z"/></svg>`;
            let dotsHtml = '';
            for (let i = 1; i <= goal; i++) {
                const filled = i <= caPreviewStamps;
                const isLast = i === goal;
                const borderCol = filled ? lbl : 'rgba(255,255,255,0.24)';
                const bgCol = filled ? lbl : 'rgba(255,255,255,0.05)';
                const txtCol = filled ? bg : 'rgba(255,255,255,0.45)';
                const icon = filled ? beanSvgSim : (isLast ? '🎁' : i);
                dotsHtml += `<div style="width:36px; height:36px; border-radius:50%; border:2px solid ${borderCol}; background:${bgCol}; color:${txtCol}; display:flex; align-items:center; justify-content:center; font-size:12px; font-weight:800; box-shadow:${filled ? '0 0 10px rgba(212,175,55,0.35)' : 'none'};">${icon}</div>`;
            }
            grid.innerHTML = dotsHtml;

            document.getElementById('ca-prev-sec1-lbl').style.color = lbl;
            document.getElementById('ca-prev-sec2-lbl').style.color = lbl;
            document.getElementById('ca-prev-reward-val').textContent = rewardText;

            // Reverso estilo Apple Wallet nativo y pantalla bloqueo GPS
            const remStamps = Math.max(0, goal - caPreviewStamps);
            const availRew = caPreviewStamps >= goal ? 1 : 0;
            const elTotSt = document.getElementById('ca-back-total-stamps');
            if (elTotSt) elTotSt.textContent = `${caPreviewStamps} ${caPreviewStamps === 1 ? 'sello' : 'sellos'}`;
            const elRemSt = document.getElementById('ca-back-remaining-stamps');
            if (elRemSt) elRemSt.textContent = `${remStamps} ${remStamps === 1 ? 'sello' : 'sellos'}`;
            const elAvRew = document.getElementById('ca-back-available-rewards');
            if (elAvRew) elAvRew.textContent = `${availRew} ${availRew === 1 ? 'premio' : 'premios'}`;
            const elTotRew = document.getElementById('ca-back-total-rewards');
            if (elTotRew) elTotRew.textContent = `${availRew} ${availRew === 1 ? 'premio' : 'premios'}`;
            const elRewDesc = document.getElementById('ca-back-val-reward');
            if (elRewDesc) elRewDesc.textContent = `${goal} sellos = ${rewardText}`;
            const elMenuVal = document.getElementById('ca-back-val-menu');
            if (elMenuVal) elMenuVal.textContent = menuUrl || '—';
            const elGpsVal = document.getElementById('ca-back-val-gps');
            if (elGpsVal) elGpsVal.textContent = relevantText || `¡Bienvenido a ${shopName}!`;
            const elTermsVal = document.getElementById('ca-back-val-terms');
            if (elTermsVal) elTermsVal.textContent = termsText;
            const elComp = document.getElementById('ca-back-company-name');
            if (elComp) elComp.textContent = shopName;
            const elIss = document.getElementById('ca-back-issuer-name');
            if (elIss) elIss.textContent = shopName;
            const elUpd = document.getElementById('ca-back-updated-at');
            if (elUpd) {
                const now = new Date();
                const pad = (n) => String(n).padStart(2, '0');
                elUpd.textContent = `${pad(now.getDate())}/${pad(now.getMonth() + 1)}/${now.getFullYear()} ${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
            }

            document.getElementById('ca-gps-lock-brand').textContent = `${shopName.toUpperCase()} · CERCA DE TI`;
            document.getElementById('ca-gps-lock-msg').textContent = relevantText;
        }

        async function saveCampaignSettings() {
            const btnTop = document.getElementById('btn-save-campaign-top');
            const btnBot = document.getElementById('btn-save-campaign-bottom');
            const banner = document.getElementById('ca-save-banner');
            const payload = {
                shopName: (document.getElementById('ca-shop-name').value || 'Demo Café').trim(),
                backgroundColor: (document.getElementById('ca-bg-hex').value || '#12100E').trim(),
                foregroundColor: (document.getElementById('ca-fg-hex').value || '#F5F2EB').trim(),
                labelColor: (document.getElementById('ca-lbl-hex').value || '#D4AF37').trim(),
                stampGoal: Math.max(4, Math.min(12, parseInt(document.getElementById('ca-stamp-goal').value || '10', 10))),
                rewardText: (document.getElementById('ca-reward-text').value || '1 Café Gratis de cualquier tamaño').trim(),
                menuUrl: (document.getElementById('ca-menu-url').value || '').trim(),
                termsText: (document.getElementById('ca-terms-text').value || '').trim(),
                lat: parseFloat(document.getElementById('ca-lat').value || '27.0706'),
                lng: parseFloat(document.getElementById('ca-lng').value || '-109.4437'),
                relevantText: (document.getElementById('ca-relevant-text').value || '').trim(),
                useCustomStrip: document.getElementById('ca-use-custom-strip').checked,
                logoDataUrl: caUploadedLogoDataUrl.startsWith('data:image/') ? caUploadedLogoDataUrl : '',
                stripDataUrl: caUploadedStripDataUrl.startsWith('data:image/') ? caUploadedStripDataUrl : ''
            };

            try {
                if (btnTop) btnTop.textContent = '⏳ Guardando y sincronizando...';
                if (btnBot) btnBot.textContent = '⏳ Guardando y sincronizando...';
                const res = await fetch('/api/campaign-settings', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    credentials: 'include',
                    body: JSON.stringify(payload)
                });
                const data = await res.json();
                if (!res.ok || !data.ok) throw new Error(data.error || 'No se pudo guardar la configuración');

                banner.style.display = 'block';
                banner.style.background = 'rgba(52,199,89,0.16)';
                banner.style.border = '1px solid rgba(36,138,61,0.4)';
                banner.style.color = '#1E7A34';
                banner.textContent = data.message || '✅ Plantilla de campaña actualizada y sincronizada.';

                const hdrName = document.getElementById('hdr-shop-name');
                if (hdrName) hdrName.textContent = payload.shopName;
                saveBaristaUiState();
                setTimeout(() => { window.location.reload(); }, 1100);
            } catch (err) {
                banner.style.display = 'block';
                banner.style.background = 'rgba(217,56,72,0.14)';
                banner.style.border = '1px solid rgba(217,56,72,0.4)';
                banner.style.color = '#C81D40';
                banner.textContent = '⚠️ Error: ' + err.message;
            } finally {
                if (btnTop) btnTop.textContent = '💾 Guardar y Actualizar Plantilla';
                if (btnBot) btnBot.textContent = '💾 Guardar y Actualizar Plantilla de Campaña';
            }
        }

        // Inicializar preview al cargar
        setTimeout(renderCampaignLivePreview, 50);

        // Restaurar pestaña activa, filtro, búsqueda y posición de scroll tras enviar un formulario
        (function restoreBaristaUiState() {
            try {
                const raw = sessionStorage.getItem('maishas_barista_ui_state');
                if (!raw) return;
                sessionStorage.removeItem('maishas_barista_ui_state');
                const state = JSON.parse(raw);
                if (!state || (Date.now() - (state.ts || 0) > 30000)) return;
                if (state.view && ['pos', 'gifts', 'combos', 'campaign', 'bday', 'push', 'db'].includes(state.view)) {
                    switchMainView(state.view, true);
                }
                const searchEl = document.getElementById('client-search');
                if (searchEl && state.searchQuery) {
                    searchEl.value = state.searchQuery;
                }
                if (state.filterTab) {
                    setFilterTab(state.filterTab);
                } else if (state.searchQuery) {
                    filterClients();
                }
                if (typeof state.scrollY === 'number' && state.scrollY > 0) {
                    setTimeout(() => window.scrollTo({ top: state.scrollY, behavior: 'instant' }), 20);
                }
            } catch (err) {}
        })();

        function detectMyGPS() {
            if (!navigator.geolocation) {
                return;
            }
            navigator.geolocation.getCurrentPosition(
                pos => {
                    document.getElementById('lat-input').value = pos.coords.latitude.toFixed(6);
                    document.getElementById('lng-input').value = pos.coords.longitude.toFixed(6);
                },
                () => {},
                { enableHighAccuracy: true }
            );
        }

        let videoStream = null;
        let scanning = false;
        let autoScanMode = true;

        function setAutoScanMode(enabled) {
            autoScanMode = enabled;
            document.getElementById('auto-stamp-flag').value = enabled ? '1' : '0';
            document.getElementById('seg-auto-on').classList.toggle('active', enabled);
            document.getElementById('seg-auto-off').classList.toggle('active', !enabled);
        }

        function playBeep() {
            try {
                const ctx = new (window.AudioContext || window.webkitAudioContext)();
                const osc = ctx.createOscillator();
                const gain = ctx.createGain();
                osc.type = 'sine';
                osc.frequency.setValueAtTime(920, ctx.currentTime);
                gain.gain.setValueAtTime(0.16, ctx.currentTime);
                osc.connect(gain);
                gain.connect(ctx.destination);
                osc.start();
                osc.stop(ctx.currentTime + 0.12);
            } catch (e) {}
        }

        async function toggleScanner() {
            const video = document.getElementById('qr-video');
            const idleView = document.getElementById('qr-idle-view');
            const reticle = document.getElementById('qr-reticle-guide');
            const status = document.getElementById('qr-status');

            if (scanning) {
                stopScanner();
                return;
            }

            try {
                videoStream = await navigator.mediaDevices.getUserMedia({
                    video: { facingMode: 'environment' }
                });
                video.srcObject = videoStream;
                await video.play();
                scanning = true;
                idleView.style.display = 'none';
                video.style.display = 'block';
                reticle.style.display = 'block';
                status.style.display = 'block';
                status.textContent = 'Apunta al código QR del cliente o Gift Card (Clic en el video para cerrar)';
                video.onclick = stopScanner;
                requestAnimationFrame(scanTick);
            } catch (err) {
                status.style.display = 'block';
                status.textContent = '⚠️ No se pudo abrir la cámara: ' + err.message;
            }
        }

        function stopScanner() {
            scanning = false;
            const video = document.getElementById('qr-video');
            const idleView = document.getElementById('qr-idle-view');
            const reticle = document.getElementById('qr-reticle-guide');
            const status = document.getElementById('qr-status');
            if (videoStream) {
                videoStream.getTracks().forEach(t => t.stop());
                videoStream = null;
            }
            video.style.display = 'none';
            reticle.style.display = 'none';
            idleView.style.display = 'block';
            status.style.display = 'none';
        }

        function scanTick() {
            if (!scanning) return;
            const video = document.getElementById('qr-video');
            const canvas = document.getElementById('qr-canvas');
            const status = document.getElementById('qr-status');
            const flash = document.getElementById('scan-flash-overlay');

            if (video.readyState === video.HAVE_ENOUGH_DATA) {
                canvas.width = video.videoWidth;
                canvas.height = video.videoHeight;
                const ctx = canvas.getContext('2d');
                ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
                const imageData = ctx.getImageData(0, 0, canvas.width, canvas.height);
                if (window.jsQR) {
                    const code = jsQR(imageData.data, imageData.width, imageData.height, {
                        inversionAttempts: 'dontInvert'
                    });
                    if (code && code.data) {
                        const rawScan = code.data.trim().toUpperCase();
                        const matchGift = rawScan.match(/GIFT-[A-Z0-9]{6}/);
                        const matchSerial = rawScan.match(/MAISHAS-[A-Z0-9]{6}/);
                        const detected = matchGift
                            ? matchGift[0]
                            : (matchSerial ? matchSerial[0] : ((rawScan.startsWith('MAISHAS-') || rawScan.startsWith('GIFT-')) ? rawScan : ''));
                        if (detected) {
                            playBeep();
                            if (flash) {
                                flash.style.opacity = '1';
                                setTimeout(() => { flash.style.opacity = '0'; }, 260);
                            }
                            document.getElementById('scan-serial-input').value = detected;
                            stopScanner();
                            status.style.display = 'block';
                            status.textContent = '✅ Detectado: ' + detected;
                            if (detected.startsWith('GIFT-') || autoScanMode) {
                                const scanFormEl = document.getElementById('scan-form');
                                if (detected.startsWith('GIFT-') || checkDuplicateScanBeforeSubmit(null, scanFormEl) !== false) {
                                    scanFormEl.submit();
                                }
                            } else {
                                document.getElementById('client-search').value = detected;
                                filterClients();
                            }
                            return;
                        }
                    }
                }
            }
            requestAnimationFrame(scanTick);
        }

        {% if dup_confirm %}
        setTimeout(() => {
            const targetFormId = {{ dup_confirm.form_id | tojson }};
            const targetSerial = {{ dup_confirm.serial | tojson }};
            if (targetFormId === 'scan-form') {
                const scanInput = document.getElementById('scan-serial-input');
                if (scanInput) scanInput.value = targetSerial;
            }
            openConfirmModal(
                'duplicate_add',
                targetFormId,
                {{ dup_confirm.name | tojson }},
                targetSerial,
                {{ dup_confirm.stamps }},
                {{ dup_confirm.secs_ago }}
            );
        }, 60);
        {% endif %}

        {% if active_view is defined and active_view in ['pos', 'gifts', 'combos', 'campaign', 'bday', 'push', 'db'] %}
        setTimeout(() => {
            switchMainView({{ active_view | tojson }}, true);
        }, 35);
        {% elif gift_updated %}
        setTimeout(() => {
            switchMainView('gifts', true);
        }, 40);
        {% endif %}

        {% if gift_scan_obj %}
        setTimeout(() => {
            openGiftPosModalByObj({{ gift_scan_obj | tojson }});
        }, 80);
        {% endif %}

        // --- PWA Instalación en Teléfono / Tablet / iPad ---
        let deferredBaristaPrompt = null;
        const installBtn = document.getElementById('btn-install-barista-pwa');

        function checkStandaloneMode() {
            const isStandalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
            if (isStandalone && installBtn) {
                installBtn.style.display = 'none';
            }
        }
        checkStandaloneMode();

        if ('serviceWorker' in navigator) {
            navigator.serviceWorker.register('/sw.js').catch(() => {});
        }

        window.addEventListener('beforeinstallprompt', (e) => {
            e.preventDefault();
            deferredBaristaPrompt = e;
            if (installBtn) installBtn.style.display = 'inline-flex';
        });

        window.addEventListener('appinstalled', () => {
            deferredBaristaPrompt = null;
            if (installBtn) installBtn.style.display = 'none';
        });

        async function installBaristaPwa() {
            if (deferredBaristaPrompt) {
                deferredBaristaPrompt.prompt();
                try { await deferredBaristaPrompt.userChoice; } catch (e) {}
                deferredBaristaPrompt = null;
            } else {
                document.getElementById('maishas-pwa-modal').classList.add('open');
            }
        }

        async function downloadClientsCsv(btnEl) {
            const origText = btnEl ? btnEl.innerHTML : '';
            try {
                if (btnEl) btnEl.innerHTML = '⏳ Descargando...';
                const res = await fetch('/api/export-clients.csv', { credentials: 'include' });
                if (!res.ok) throw new Error('Error al generar archivo');
                const blob = await res.blob();
                const today = new Date().toISOString().slice(0, 10);
                const filename = `Clientes_DemoCafe_${today}.csv`;

                const isMobile = /iPhone|iPad|iPod|Android/i.test(navigator.userAgent || '');
                if (isMobile && navigator.canShare) {
                    const file = new File([blob], filename, { type: 'text/csv;charset=utf-8' });
                    if (navigator.canShare({ files: [file] })) {
                        await navigator.share({
                            files: [file],
                            title: 'Clientes Demo Café'
                        });
                        if (btnEl) btnEl.innerHTML = origText;
                        return;
                    }
                }

                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = filename;
                a.style.display = 'none';
                document.body.appendChild(a);
                a.click();
                setTimeout(() => {
                    document.body.removeChild(a);
                    URL.revokeObjectURL(url);
                }, 1500);
            } catch (err) {
                // Si el usuario cancela el menú compartir en iOS/Android (AbortError), no hacer nada
            } finally {
                if (btnEl) btnEl.innerHTML = origText;
            }
        }
    </script>
</body>
</html>
"""


def _active_logo_file_path():
    if os.path.isfile(CUSTOM_LOGO_PATH):
        return CUSTOM_LOGO_PATH
    brand_logo = os.path.join(PASS_TEMPLATE_DIR, "brand_logo.png")
    if os.path.isfile(brand_logo):
        return brand_logo
    return os.path.join(PASS_TEMPLATE_DIR, "icon@2x.png")


def generate_signed_pkpass(
    serial_number,
    client_name,
    stamps_count,
    host_url,
    message=None,
    phone=None,
    birthdate=None,
    total_visits=None,
    redeemed_count=0,
    last_action="add",
    for_android=False,
    is_voided=False
):
    temp_dir = tempfile.mkdtemp(prefix=f"pass_{serial_number}_")
    try:
        for item in os.listdir(PASS_TEMPLATE_DIR):
            if item in ["manifest.json", "signature", ".DS_Store"]:
                continue
            src = os.path.join(PASS_TEMPLATE_DIR, item)
            dst = os.path.join(temp_dir, item)
            if os.path.isfile(src):
                shutil.copy2(src, dst)

        cfg = get_campaign_config()
        goal = max(4, min(12, int(cfg.get("stampGoal") or 10)))
        shop_name = (cfg.get("shopName") or "Demo Café").strip()
        reward_text = (cfg.get("rewardText") or "1 Café Gratis").strip()

        # Aplicar SIEMPRE el logo oficial de la cafetería (brand_logo.png o personalizado) en círculo a icon.png y logo.png del .pkpass
        active_logo_path = _active_logo_file_path()
        if os.path.isfile(active_logo_path):
            for fname, px_size in [
                ("icon.png", 29),
                ("icon@2x.png", 58),
                ("icon@3x.png", 87),
                ("logo.png", 50),
                ("logo@2x.png", 100),
                ("logo@3x.png", 150),
            ]:
                try:
                    circ_img = _render_circular_logo_rgba(active_logo_path, px_size)
                    if circ_img is not None:
                        circ_img.save(os.path.join(temp_dir, fname), format="PNG")
                except Exception:
                    pass

        stamps = max(0, min(goal, int(stamps_count or 0)))
        visits = total_visits if total_visits is not None else stamps
        tier = get_vip_tier(visits)
        clean_visible_msg = (message or DEFAULT_MESSAGE).replace("\u200b", "")

        render_dynamic_strip(stamps, temp_dir, total_visits=visits, message=clean_visible_msg, campaign=cfg)

        pass_json_path = os.path.join(temp_dir, "pass.json")
        with open(pass_json_path, "r", encoding="utf-8") as f:
            pass_data = json.load(f)

        pass_data["serialNumber"] = serial_number
        pass_data["description"] = f"Tarjeta de Fidelidad {shop_name}"
        pass_data["organizationName"] = shop_name
        pass_data["foregroundColor"] = _hex_to_passkit_rgb(cfg.get("foregroundColor"), (245, 242, 235))
        pass_data["backgroundColor"] = _hex_to_passkit_rgb(cfg.get("backgroundColor"), (18, 16, 14))
        pass_data["labelColor"] = _hex_to_passkit_rgb(cfg.get("labelColor"), (198, 166, 117))
        pass_data["logoText"] = shop_name.upper()
        if is_voided:
            pass_data["voided"] = True
            pass_data["expirationDate"] = "2000-01-01T00:00:00Z"
        else:
            pass_data.pop("voided", None)
            pass_data.pop("expirationDate", None)

        barcode_obj = {
            "format": "PKBarcodeFormatQR",
            "message": serial_number,
            "messageEncoding": "utf-8",
            "altText": serial_number
        }
        pass_data["barcode"] = barcode_obj
        pass_data["barcodes"] = [barcode_obj]

        # Mensaje de notificación inteligente según la acción realizada
        if last_action == "sub":
            stamp_change_msg = f"☕️ Tu tarjeta {shop_name} está al día. Sellos acumulados: %@"
        elif last_action == "reset" or stamps == 0:
            stamp_change_msg = f"🎁 ¡Disfruta tu recompensa ({reward_text})! Tu tarjeta {shop_name} está en: %@"
        elif stamps >= goal:
            stamp_change_msg = f"🎉 ¡Felicidades! Completaste %@ sellos. ¡Tienes {reward_text} en {shop_name}!"
        else:
            stamp_change_msg = f"☕️ ¡Has ganado 1 nuevo sello en {shop_name}! Total: %@"

        pass_data["storeCard"]["headerFields"] = [
            {
                "key": "stamps",
                "label": "SELLOS",
                "value": f"{stamps} / {goal}",
                "textAlignment": "PKTextAlignmentRight",
                "changeMessage": stamp_change_msg
            }
        ]

        reward_status = "¡RECOMPENSA LISTA! 🎉" if stamps >= goal else f"Faltan {goal - stamps} sellos"
        tier_clean_label = (
            tier["short"]
            .replace("👑", "")
            .replace("🥈", "")
            .replace("🥉", "")
            .strip()
            .upper()
        )

        if for_android:
            # Google Wallet y lectores .pkpass de Android requieren primaryFields y auxiliaryFields no vacíos
            pass_data["storeCard"]["primaryFields"] = [
                {
                    "key": "points",
                    "label": "SELLOS ACUMULADOS",
                    "value": f"{stamps} / {goal}"
                }
            ]
            pass_data["storeCard"]["secondaryFields"] = [
                {
                    "key": "memberName",
                    "label": f"CLIENTE · {tier_clean_label}",
                    "value": client_name
                },
                {
                    "key": "rewardGoal",
                    "label": "¡PREMIO LISTO! 🎉" if stamps >= goal else "PREMIO META",
                    "value": reward_text
                }
            ]
            pass_data["storeCard"]["auxiliaryFields"] = [
                {
                    "key": "serialCode",
                    "label": "CÓDIGO ID",
                    "value": serial_number
                },
                {
                    "key": "promo",
                    "label": f"AVISO · {reward_status.upper()}",
                    "value": clean_visible_msg
                }
            ]
        else:
            # En iPhone (iOS Apple Wallet), primaryFields vacío evita texto sobre el strip.png
            # secondaryFields idéntico al Simulador Apple Wallet (CLIENTE · <NIVEL> a la izq, PREMIO META a la der)
            pass_data["storeCard"]["primaryFields"] = []
            pass_data["storeCard"]["secondaryFields"] = [
                {
                    "key": "memberName",
                    "label": f"CLIENTE · {tier_clean_label}",
                    "value": client_name
                },
                {
                    "key": "rewardGoal",
                    "label": "¡PREMIO LISTO! 🎉" if stamps >= goal else "PREMIO META",
                    "value": reward_text,
                    "textAlignment": "PKTextAlignmentRight"
                }
            ]
            pass_data["storeCard"]["auxiliaryFields"] = []

        pass_data["locations"] = [
            {
                "latitude": cfg["lat"],
                "longitude": cfg["lng"],
                "relevantText": cfg["relevantText"]
            }
        ]
        pass_data["maxDistance"] = 150

        explicit_base = (os.environ.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
        clean_host = explicit_base if explicit_base else host_url.rstrip("/")
        if not ("localhost" in clean_host or "127.0.0.1" in clean_host):
            clean_host = clean_host.replace("http://", "https://", 1)
        pass_data["webServiceURL"] = f"{clean_host}/"
        pass_data["authenticationToken"] = AUTH_TOKEN

        # Actualizar el reverso de la tarjeta (.pkpass) con la estructura completa de Demo Café
        remaining_stamps_back = max(0, goal - stamps)
        avail_rewards_back = 1 if stamps >= goal else 0
        wa_link_back = cfg.get("menuUrl") or "https://wa.me/526421138069"
        now_str_back = datetime.now().strftime("%d/%m/%Y, %H:%M")

        back_fields = [
            {
                "key": "news",
                "label": "Último Mensaje",
                "value": message or DEFAULT_MESSAGE,
                "changeMessage": "📣 %@"
            },
            {
                "key": "feedbackForm",
                "label": "Formulario de comentarios",
                "value": "⭐ ¿Disfrutaste tu última visita?",
                "attributedValue": f"<a href='{clean_host}/'>⭐ ¿Disfrutaste tu última visita?</a>"
            },
            {
                "key": "totalStamps",
                "label": "Total acumulado",
                "value": str(stamps)
            },
            {
                "key": "nextRewardRemaining",
                "label": "Para la siguiente recompensa",
                "value": f"{remaining_stamps_back} sellos"
            },
            {
                "key": "availableRewards",
                "label": "Disponible",
                "value": f"{avail_rewards_back} premios"
            },
            {
                "key": "howToGetStamp",
                "label": "Cómo conseguir un sello",
                "value": "Por cada compra de una bebida participante o consumo en sucursal obtienes 1 sello digital."
            },
            {
                "key": "totalRewardsAccumulated",
                "label": "Total acumulado",
                "value": f"{redeemed_count} premios"
            },
            {
                "key": "rewardInfo",
                "label": "Descripción del premio",
                "value": f"Obtienes un premio después de acumular {goal} sellos ({reward_text})."
            },
            {
                "key": "validUntil",
                "label": "La tarjeta es válida hasta",
                "value": "Ilimitado"
            },
            {
                "key": "terms",
                "label": "Términos de Uso",
                "value": cfg.get("termsText") or CampaignDefaults["termsText"]
            },
            {
                "key": "phoneLink",
                "label": "Teléfono",
                "value": "📱 Teléfono (+52 642 113 8069)",
                "attributedValue": f"<a href='{wa_link_back}'>📱 Teléfono</a>"
            },
            {
                "key": "instagramLink",
                "label": "Instagram",
                "value": "Instagram (@democafe)",
                "attributedValue": "<a href='https://www.instagram.com/'>Instagram</a>"
            },
            {
                "key": "companyName",
                "label": "Nombre de empresa",
                "value": shop_name
            },
            {
                "key": "issuerInfo",
                "label": "Información del emisor",
                "value": f"{shop_name}\nhola@democafe.com\n+526421138069",
                "attributedValue": f"{shop_name}\n<a href='mailto:hola@democafe.com'>hola@democafe.com</a>\n<a href='tel:+526421138069'>+526421138069</a>"
            },
            {
                "key": "memberSerialId",
                "label": "Número de serie",
                "value": serial_number
            },
            {
                "key": "updatedAt",
                "label": "Actualizado",
                "value": now_str_back
            }
        ]
        pass_data["storeCard"]["backFields"] = back_fields

        with open(pass_json_path, "w", encoding="utf-8") as f:
            json.dump(pass_data, f, indent=2)

        manifest = {}
        for root, dirs, files in os.walk(temp_dir):
            for fname in files:
                if fname in ['manifest.json', 'signature', '.DS_Store']:
                    continue
                full_path = os.path.join(root, fname)
                rel_path = os.path.relpath(full_path, temp_dir).replace('\\', '/')
                with open(full_path, 'rb') as fp:
                    manifest[rel_path] = hashlib.sha1(fp.read()).hexdigest()

        manifest_path = os.path.join(temp_dir, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as fp:
            json.dump(manifest, fp, indent=2)

        sig_path = os.path.join(temp_dir, "signature")
        cmd_sign = (
            f'openssl smime -sign -signer "{CERT_PEM}" -inkey "{KEY_PEM}" '
            f'-certfile "{WWDR_PEM}" -in "{manifest_path}" -out "{sig_path}" '
            f'-outform DER -binary'
        )
        subprocess.run(cmd_sign, shell=True, check=True)

        suffix = "_android" if for_android else "_ios"
        output_pkpass = f"/tmp/{serial_number}{suffix}.pkpass"
        with zipfile.ZipFile(output_pkpass, 'w', zipfile.ZIP_DEFLATED) as z:
            for root, dirs, files in os.walk(temp_dir):
                for fname in files:
                    if fname == '.DS_Store':
                        continue
                    full_path = os.path.join(root, fname)
                    rel_path = os.path.relpath(full_path, temp_dir)
                    z.write(full_path, rel_path)

        return output_pkpass
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------
# MOTOR DE GIFT CARDS DIGITALES (SALDO + COMBOS / EXPERIENCIAS)
# ---------------------------------------------------------

def _row_to_gift_dict(row):
    if not row:
        return None
    gift_type = (row[1] or "balance").strip().lower()
    init_amt = float(row[5] or 0.0)
    curr_bal = float(row[6] or 0.0)
    tot_uses = int(row[7] or 1)
    rem_uses = int(row[8] or 0)
    status_val = (row[14] or "pending").strip().lower()
    pkg_title = row[3] or (f"Saldo de Regalo ${init_amt:,.0f} MXN" if gift_type == "balance" else "Experiencia de Regalo")
    pkg_desc = row[4] or ("Válido en cualquier bebida, frappé o postre del menú" if gift_type == "balance" else "")

    if gift_type == "combo":
        value_headline = pkg_title
        balance_display = "1 Paquete Disponible" if (status_val == "active" and rem_uses > 0) else ("Canjeado ✅" if status_val == "redeemed" else "Por activar ⏳")
    else:
        value_headline = f"${curr_bal:,.2f} MXN"
        balance_display = f"${curr_bal:,.2f} / ${init_amt:,.0f} MXN"

    out = {
        "code": row[0],
        "gift_type": gift_type,
        "package_key": row[2] or "",
        "package_title": pkg_title,
        "combo_title": pkg_title,
        "package_desc": pkg_desc,
        "initial_amount": init_amt,
        "current_balance": curr_bal,
        "total_uses": tot_uses,
        "remaining_uses": rem_uses,
        "sender_name": row[9] or "Alguien especial",
        "sender_phone": row[10] or "",
        "recipient_name": row[11] or "Para ti",
        "recipient_phone": row[12] or "",
        "dedication": row[13] or "¡Disfruta un momento especial en Demo Café! ☕✨",
        "status": status_val,
        "converted_serial": row[15] or "",
        "created_at_utc": row[16] or "",
        "created_at_sonora": row[17] or "",
        "activated_at_sonora": row[18] or "",
        "updated_at": row[19] or "",
        "last_action_detail": row[20] or "",
        "value_headline": value_headline,
        "balance_display": balance_display,
    }
    try:
        out.update(_build_gift_whatsapp_urls(out))
    except Exception:
        pass
    return out


def get_gift_card_by_code(code_str):
    clean = (code_str or "").strip().upper()
    m = re.search(r"GIFT-[A-Z0-9]{6}", clean)
    if m:
        clean = m.group(0)
    elif re.fullmatch(r"[A-Z0-9]{6}", clean):
        clean = f"GIFT-{clean}"
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        """SELECT code, gift_type, package_key, package_title, package_desc,
                  initial_amount, current_balance, total_uses, remaining_uses,
                  sender_name, sender_phone, recipient_name, recipient_phone,
                  dedication, status, converted_serial, created_at_utc,
                  created_at_sonora, activated_at_sonora, updated_at, last_action_detail
           FROM gift_cards
           WHERE UPPER(code) = ?""",
        (clean,)
    ).fetchone()
    conn.close()
    return _row_to_gift_dict(row)


def list_gift_cards_for_barista():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT code, gift_type, package_key, package_title, package_desc,
                  initial_amount, current_balance, total_uses, remaining_uses,
                  sender_name, sender_phone, recipient_name, recipient_phone,
                  dedication, status, converted_serial, created_at_utc,
                  created_at_sonora, activated_at_sonora, updated_at, last_action_detail
           FROM gift_cards
           WHERE COALESCE(status, 'pending') != 'cancelled'
           ORDER BY CASE WHEN COALESCE(status, 'pending') = 'pending' THEN 0 WHEN status = 'active' THEN 1 ELSE 2 END, rowid DESC"""
    ).fetchall()
    conn.close()
    return [_row_to_gift_dict(r) for r in rows if r]


def generate_signed_gift_pkpass(gift_code, host_url, for_android=False):
    """
    Genera y firma un archivo .pkpass oficial de Apple Wallet para una Gift Card (GIFT-XXXXXX),
    reutilizando el certificado oficial PassKit de la cafetería para soportar notificaciones Push APNs.
    """
    gift = get_gift_card_by_code(gift_code)
    if not gift:
        raise ValueError(f"Gift Card {gift_code} no encontrada")

    serial_number = gift["code"]
    temp_dir = tempfile.mkdtemp(prefix=f"gift_{serial_number}_")
    try:
        for item in os.listdir(PASS_TEMPLATE_DIR):
            if item in ["manifest.json", "signature", ".DS_Store"]:
                continue
            src = os.path.join(PASS_TEMPLATE_DIR, item)
            dst = os.path.join(temp_dir, item)
            if os.path.isfile(src):
                shutil.copy2(src, dst)

        cfg = get_campaign_config()
        shop_name = (cfg.get("shopName") or "Demo Café").strip()

        active_logo_path = _active_logo_file_path()
        if os.path.isfile(active_logo_path):
            for fname, px_size in [
                ("icon.png", 29),
                ("icon@2x.png", 58),
                ("icon@3x.png", 87),
                ("logo.png", 50),
                ("logo@2x.png", 100),
                ("logo@3x.png", 150),
            ]:
                try:
                    circ_img = _render_circular_logo_rgba(active_logo_path, px_size)
                    if circ_img is not None:
                        circ_img.save(os.path.join(temp_dir, fname), format="PNG")
                except Exception:
                    pass

        render_gift_strip(gift, temp_dir, campaign=cfg)

        pass_json_path = os.path.join(temp_dir, "pass.json")
        with open(pass_json_path, "r", encoding="utf-8") as f:
            pass_data = json.load(f)

        pass_data["serialNumber"] = serial_number
        pass_data["description"] = f"GIFT CARD - {shop_name}"
        pass_data["organizationName"] = shop_name
        # Colores del sistema: Latte cálido (#F3ECE7), texto Espresso (#121212) y etiquetas Verde Oliva (#4A5D23)
        pass_data["backgroundColor"] = "rgb(243, 236, 231)"
        pass_data["foregroundColor"] = "rgb(18, 18, 18)"
        pass_data["labelColor"] = "rgb(74, 93, 35)"
        # Título requerido: "GIFT CARD - {empresa}" con todo el ancho del header libre
        pass_data["logoText"] = f"GIFT CARD - {shop_name.upper()}"

        is_exhausted = (
            gift["status"] in ("redeemed", "cancelled")
            or (gift["gift_type"] == "balance" and float(gift["current_balance"] or 0) <= 0)
            or (gift["gift_type"] == "combo" and int(gift["remaining_uses"] or 0) <= 0)
        )
        if is_exhausted:
            pass_data["voided"] = True
            pass_data["expirationDate"] = "2000-01-01T00:00:00Z"
        else:
            pass_data.pop("voided", None)
            pass_data.pop("expirationDate", None)

        barcode_obj = {
            "format": "PKBarcodeFormatQR",
            "message": serial_number,
            "messageEncoding": "utf-8",
            "altText": serial_number
        }
        pass_data["barcode"] = barcode_obj
        pass_data["barcodes"] = [barcode_obj]

        # Dejamos headerFields vacío para que "GIFT CARD - {empresa}" nunca se empalme a la derecha;
        # el saldo / paquete ya se muestra en grande en el strip central.
        pass_data["storeCard"]["headerFields"] = []

        if for_android:
            pass_data["storeCard"]["primaryFields"] = [
                {
                    "key": "giftMain",
                    "label": "VALE / EXPERIENCIA" if gift["gift_type"] == "combo" else "SALDO DISPONIBLE",
                    "value": gift["value_headline"]
                }
            ]
        else:
            pass_data["storeCard"]["primaryFields"] = []

        raw_recip = (gift["recipient_name"] or "Para ti").strip()
        raw_sender = (gift["sender_name"] or "Alguien especial").strip()
        recip_display = (raw_recip.title() if raw_recip.islower() else raw_recip)[:22]
        sender_display = (raw_sender.title() if raw_sender.islower() else raw_sender)[:22]

        pass_data["storeCard"]["secondaryFields"] = [
            {
                "key": "recipientName",
                "label": "PARA",
                "value": recip_display,
                "textAlignment": "PKTextAlignmentLeft"
            },
            {
                "key": "senderName",
                "label": "DE PARTE DE",
                "value": sender_display,
                "textAlignment": "PKTextAlignmentRight"
            }
        ]
        pass_data["storeCard"]["auxiliaryFields"] = []

        pass_data["locations"] = [
            {
                "latitude": cfg["lat"],
                "longitude": cfg["lng"],
                "relevantText": f"🎁 Tienes una Gift Card disponible en {shop_name}. ¡Pasa por tu café favorito!"
            }
        ]
        pass_data["maxDistance"] = 150

        explicit_base = (os.environ.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
        clean_host = explicit_base if explicit_base else host_url.rstrip("/")
        if not ("localhost" in clean_host or "127.0.0.1" in clean_host):
            clean_host = clean_host.replace("http://", "https://", 1)
        pass_data["webServiceURL"] = f"{clean_host}/"
        pass_data["authenticationToken"] = AUTH_TOKEN

        gift_unbox_url = f"{clean_host}/gift/{serial_number}"
        now_str_back = datetime.now().strftime("%d/%m/%Y, %H:%M")

        pass_data["storeCard"]["backFields"] = [
            {
                "key": "dedicationMsg",
                "label": f"💌 Dedicatoria de {gift['sender_name']}",
                "value": gift["dedication"]
            },
            {
                "key": "giftStatusDetail",
                "label": "Último movimiento y saldo",
                "value": gift["last_action_detail"] or "Gift Card registrada",
                "changeMessage": "%@"
            },
            {
                "key": "giftBenefit",
                "label": "Incluye / Beneficio",
                "value": f"{gift['package_title']} — {gift['package_desc']}"
            },
            {
                "key": "unboxLink",
                "label": "Gift Card Digital y Conversión a Lealtad (+1 Sello Gratis)",
                "value": gift_unbox_url,
                "attributedValue": f"<a href='{gift_unbox_url}'>🎁 Abrir Gift Card Digital o Convertir en Tarjeta de Lealtad (+1 Sello Gratis)</a>"
            },
            {
                "key": "howToRedeemGift",
                "label": "Cómo canjear en sucursal",
                "value": f"Muestra el código QR de esta Gift Card al barista en el mostrador de {shop_name}. Si es de saldo, puedes usarla en una o varias visitas hasta agotar tu saldo."
            },
            {
                "key": "giftSerialId",
                "label": "Código de Gift Card",
                "value": serial_number
            },
            {
                "key": "updatedAt",
                "label": "Actualizado",
                "value": now_str_back
            }
        ]

        with open(pass_json_path, "w", encoding="utf-8") as f:
            json.dump(pass_data, f, indent=2)

        manifest = {}
        for root, dirs, files in os.walk(temp_dir):
            for fname in files:
                if fname in ["manifest.json", "signature", ".DS_Store"]:
                    continue
                full_path = os.path.join(root, fname)
                rel_path = os.path.relpath(full_path, temp_dir).replace("\\", "/")
                with open(full_path, "rb") as fp:
                    manifest[rel_path] = hashlib.sha1(fp.read()).hexdigest()

        manifest_path = os.path.join(temp_dir, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as fp:
            json.dump(manifest, fp, indent=2)

        sig_path = os.path.join(temp_dir, "signature")
        cmd_sign = (
            f'openssl smime -sign -signer "{CERT_PEM}" -inkey "{KEY_PEM}" '
            f'-certfile "{WWDR_PEM}" -in "{manifest_path}" -out "{sig_path}" '
            f'-outform DER -binary'
        )
        subprocess.run(cmd_sign, shell=True, check=True)

        suffix = "_android" if for_android else "_ios"
        output_pkpass = f"/tmp/{serial_number}{suffix}.pkpass"
        with zipfile.ZipFile(output_pkpass, "w", zipfile.ZIP_DEFLATED) as z:
            for root, dirs, files in os.walk(temp_dir):
                for fname in files:
                    if fname == ".DS_Store":
                        continue
                    full_path = os.path.join(root, fname)
                    rel_path = os.path.relpath(full_path, temp_dir)
                    z.write(full_path, rel_path)

        return output_pkpass
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


GIFT_UNBOXING_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <meta name="theme-color" content="#F3ECE7">
    <title>GIFT CARD - {{ campaign.shopName }}</title>
    <link rel="icon" type="image/png" href="/card/icon-192.png">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-latte: #F3ECE7;
            --card-cream: #FAF6F3;
            --surface-white: #FFFFFF;
            --text-dark: #121212;
            --text-muted: #6E655F;
            --sand: #EADFD7;
            --olive: #4A5D23;
            --rose: #E86C78;
            --pass-bg: {{ campaign.backgroundColor or '#12100E' }};
            --pass-fg: {{ campaign.foregroundColor or '#FAF6F3' }};
            --pass-accent: {{ campaign.labelColor or '#C5A869' }};
        }
        * { box-sizing: border-box; -webkit-font-smoothing: antialiased; }
        body {
            margin: 0;
            min-height: 100vh;
            font-family: "Plus Jakarta Sans", -apple-system, BlinkMacSystemFont, sans-serif;
            background-color: var(--bg-latte);
            color: var(--text-dark);
            display: flex;
            flex-direction: column;
            align-items: center;
            padding: 20px 14px 56px;
        }
        .shell {
            width: 100%;
            max-width: 430px;
        }
        .top-pill {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 6px 14px;
            border-radius: 999px;
            background: var(--sand);
            border: 1px solid rgba(18, 18, 18, 0.08);
            color: var(--text-dark);
            font-size: 11.5px;
            font-weight: 800;
            letter-spacing: 0.5px;
            text-transform: uppercase;
        }
        .envelope-card {
            margin-top: 14px;
            background: var(--card-cream);
            border: 1px solid rgba(18, 18, 18, 0.08);
            border-radius: 26px;
            padding: 20px 18px;
            box-shadow: 0 10px 30px -5px rgba(18, 18, 18, 0.07), 0 4px 12px -2px rgba(18, 18, 18, 0.04);
            position: relative;
            overflow: hidden;
        }
        .dedication-box {
            margin: 14px 0;
            padding: 14px 16px;
            border-radius: 18px;
            background: var(--sand);
            border: 1px solid rgba(18, 18, 18, 0.07);
            color: var(--text-dark);
            font-size: 14px;
            line-height: 1.5;
            font-style: italic;
        }
        .strip-preview {
            width: 100%;
            border-radius: 16px;
            border: 1px solid rgba(18, 18, 18, 0.12);
            display: block;
            margin: 12px 0 14px;
            box-shadow: 0 10px 24px rgba(18, 18, 18, 0.14);
        }
        .summary-box {
            background: var(--surface-white);
            border: 1px solid rgba(18, 18, 18, 0.08);
            border-radius: 16px;
            padding: 12px 14px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 10px;
            margin-bottom: 14px;
        }
        .qr-box {
            background: #FFFFFF;
            border: 1px solid rgba(18, 18, 18, 0.09);
            border-radius: 20px;
            padding: 14px;
            width: 196px;
            margin: 12px auto 4px;
            text-align: center;
            box-shadow: 0 8px 22px rgba(18, 18, 18, 0.08);
        }
        .qr-box img {
            width: 100%;
            height: auto;
            display: block;
        }
        .code-pill {
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            font-size: 14px;
            font-weight: 800;
            letter-spacing: 1.5px;
            color: var(--text-dark);
            margin-top: 6px;
        }
        .btn-primary-wallet {
            width: 100%;
            padding: 14px 16px;
            border-radius: 14px;
            border: none;
            background: var(--text-dark);
            color: #FFFFFF;
            font-size: 15px;
            font-weight: 800;
            font-family: inherit;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            text-decoration: none;
            box-shadow: 0 8px 20px rgba(18, 18, 18, 0.16);
            margin-bottom: 8px;
            transition: transform 0.15s ease;
        }
        .btn-primary-wallet:active {
            transform: scale(0.98);
        }
        .hook-card {
            margin-top: 18px;
            background: var(--card-cream);
            border: 1px solid rgba(18, 18, 18, 0.08);
            border-radius: 26px;
            padding: 20px 18px;
            box-shadow: 0 10px 30px -5px rgba(18, 18, 18, 0.06);
        }
        .field-label {
            display: block;
            font-size: 12px;
            font-weight: 700;
            color: var(--text-dark);
            margin-bottom: 5px;
        }
        .input-gift {
            width: 100%;
            padding: 12px 14px;
            border-radius: 14px;
            border: 1px solid rgba(18, 18, 18, 0.15);
            background: var(--surface-white);
            color: var(--text-dark);
            font-size: 14px;
            font-family: inherit;
            outline: none;
        }
        .input-gift:focus {
            border-color: var(--text-dark);
        }
        .bday-card {
            padding: 12px 14px;
            border-radius: 16px;
            background: var(--sand);
            border: 1px solid rgba(18, 18, 18, 0.06);
            margin-top: 12px;
            margin-bottom: 12px;
        }
        .bday-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
            margin-top: 8px;
        }
        .status-badge {
            display: inline-flex;
            align-items: center;
            gap: 5px;
            padding: 5px 11px;
            border-radius: 99px;
            font-size: 11px;
            font-weight: 800;
        }
        .status-active { background: rgba(74, 93, 35, 0.14); color: var(--olive); border: 1px solid rgba(74, 93, 35, 0.3); }
        .status-pending { background: #FFFBEB; color: #92400E; border: 1px solid rgba(245, 158, 11, 0.45); }
        .status-redeemed { background: var(--sand); color: var(--text-dark); border: 1px solid rgba(18, 18, 18, 0.15); }
    </style>
</head>
<body>
    <div class="shell">
        <div style="text-align:center;">
            <span class="top-pill">🎁 GIFT CARD - {{ campaign.shopName | upper }}</span>
            <h1 style="margin:10px 0 4px; font-size:25px; font-weight:800; letter-spacing:-0.02em; color:var(--text-dark);">
                ¡Hola, {{ gift.recipient_name }}! ✨
            </h1>
            <p style="margin:0; font-size:14px; color:var(--text-muted);">
                <strong style="color:var(--text-dark);">{{ gift.sender_name }}</strong> te envió un regalo
            </p>
        </div>

        <div class="envelope-card">
            <div style="display:flex; justify-content:space-between; align-items:center; gap:8px; flex-wrap:wrap;">
                <div style="display:flex; align-items:center; gap:10px;">
                    <img src="/api/campaign/logo.png?v={{ campaign.logoVersion }}" alt="Logo" style="width:40px; height:40px; border-radius:50%; border:1.5px solid rgba(18,18,18,0.12); object-fit:cover; background:#FFFFFF;">
                    <div>
                        <div style="font-size:11px; font-weight:800; color:var(--olive); letter-spacing:0.5px; text-transform:uppercase;">GIFT CARD - {{ campaign.shopName }}</div>
                        <div style="font-size:14.5px; font-weight:800; color:var(--text-dark);">Tarjeta de Regalo Digital</div>
                    </div>
                </div>
                <span id="live-status-badge" class="status-badge {{ 'status-active' if gift.status == 'active' else ('status-redeemed' if gift.status == 'redeemed' else 'status-pending') }}">
                    {% if gift.status == 'active' %}
                        🟢 Activa en Mostrador
                    {% elif gift.status == 'redeemed' %}
                        🎉 Disfrutada al 100%
                    {% else %}
                        ⏳ Pendiente de activar
                    {% endif %}
                </span>
            </div>

            <div class="dedication-box">
                <div style="font-size:11px; font-weight:800; color:var(--olive); font-style:normal; text-transform:uppercase; letter-spacing:0.4px; margin-bottom:4px;">
                    💌 Dedicatoria de {{ gift.sender_name }}:
                </div>
                “{{ gift.dedication }}”
            </div>

            <!-- Imagen en vivo del Strip con los colores de la campaña -->
            <img id="live-gift-strip" src="/gift/{{ gift.code }}/strip.png?v={{ gift.updated_at | urlencode }}" alt="Gift Card" class="strip-preview">

            <!-- Resumen numérico de Saldo o Experiencia -->
            <div class="summary-box">
                <div>
                    <div style="font-size:11px; font-weight:700; color:var(--text-muted); text-transform:uppercase;">
                        {{ 'Experiencia incluida' if gift.gift_type == 'combo' else 'Saldo disponible' }}
                    </div>
                    <div id="live-gift-headline" style="font-size:20px; font-weight:800; color:var(--text-dark); margin-top:2px;">
                        {{ gift.value_headline }}
                    </div>
                    <div id="live-gift-desc" style="font-size:12px; color:var(--text-muted); margin-top:2px;">
                        {{ gift.package_desc }}
                    </div>
                </div>
                <div style="text-align:right; flex-shrink:0;">
                    <span id="live-gift-sub" style="font-size:12px; font-weight:800; color:var(--text-dark); background:var(--sand); padding:6px 10px; border-radius:10px; border:1px solid rgba(18,18,18,0.08); display:inline-block;">
                        {{ gift.balance_display }}
                    </span>
                </div>
            </div>

            <div id="pending-activation-box" style="{{ '' if gift.status == 'pending' else 'display:none;' }} background:#FFFBEB; border:1.5px solid #F59E0B; border-radius:16px; padding:15px; margin-bottom:14px; text-align:center;">
                <div style="font-size:13.5px; font-weight:800; color:#92400E; margin-bottom:5px;">
                    ⏳ Gift Card en proceso de activación por el barista
                </div>
                <div style="font-size:12.5px; color:#78350F; line-height:1.45;">
                    Esta Gift Card (<strong>{{ gift.code }}</strong>) aún debe ser activada por el barista en <strong>{{ campaign.shopName }}</strong>. En cuanto quede activada, aparecerán aquí automáticamente tu código QR y el botón para guardarla en tu Wallet.
                </div>
            </div>

            <div id="active-wallet-actions" style="{{ 'display:none;' if gift.status == 'pending' else '' }}">
                <!-- Botón Único Inteligente de Billetera Digital (Auto-detecta iOS vs Android) -->
                <div style="margin-top:6px;">
                    <a id="smart-gift-wallet-btn"
                       href="{{ '/gift/' ~ gift.code ~ '/google-wallet' if is_android else '/gift/' ~ gift.code ~ '/download.pkpass' }}"
                       class="btn-primary-wallet">
                        <span id="smart-gift-wallet-icon">{{ '🤖' if is_android else '🍎' }}</span>
                        <span id="smart-gift-wallet-label">{{ 'Agregar a Google Wallet' if is_android else 'Agregar a Apple Wallet' }}</span>
                    </a>
                </div>

                <!-- Botones para Compartir por WhatsApp o Copiar Link desde la propia Gift Card abierta -->
                <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-top:8px;">
                    <a id="unbox-wa-share-btn"
                       href="{{ gift.whatsapp_share_url }}"
                       target="_blank"
                       rel="noopener"
                       style="padding:11px 12px; border-radius:13px; background:#25D366; color:#FFFFFF; font-size:13px; font-weight:800; text-decoration:none; display:flex; align-items:center; justify-content:center; gap:6px; box-shadow:0 4px 12px rgba(37,211,102,0.25);">
                        <span>📲</span>
                        <span>Enviar a WhatsApp</span>
                    </a>
                    <button type="button"
                            id="unbox-copy-link-btn"
                            onclick="copyUnboxGiftLink()"
                            style="padding:11px 12px; border-radius:13px; border:1px solid rgba(18,18,18,0.14); background:var(--surface-white); color:var(--text-dark); font-size:13px; font-weight:800; font-family:inherit; cursor:pointer; display:flex; align-items:center; justify-content:center; gap:6px;">
                        <span>📋</span>
                        <span id="unbox-copy-link-lbl">Copiar Link</span>
                    </button>
                </div>

                <!-- QR para escanear en Mostrador (sin mensaje de fecha de activación) -->
                <div style="text-align:center; margin-top:14px; padding-top:14px; border-top:1px solid rgba(18,18,18,0.08);">
                    <div style="font-size:12px; font-weight:700; color:var(--text-muted);">
                        📷 Muestra este QR al barista en {{ campaign.shopName }}
                    </div>
                    <div class="qr-box">
                        <img src="/gift/{{ gift.code }}/qr.png" alt="QR {{ gift.code }}">
                        <div class="code-pill">{{ gift.code }}</div>
                    </div>
                </div>
            </div>
        </div>

        <!-- BENEFICIO EXCLUSIVO PARA TI: MISMO FORMULARIO COMPLETO DE LA TARJETA DE LEALTAD -->
        <div class="hook-card" id="loyalty-conversion-card">
            {% if gift.converted_serial %}
                <div style="text-align:center;">
                    <div style="font-size:24px; margin-bottom:4px;">🎉☕</div>
                    <h3 style="margin:0 0 6px; font-size:17px; font-weight:800; color:var(--olive);">
                        ¡Ya tienes tu Tarjeta de Lealtad con +1 Sello Gratis!
                    </h3>
                    <p style="margin:0 0 12px; font-size:13px; color:var(--text-muted);">
                        Tu código de socio es <strong style="font-family:monospace; color:var(--text-dark);">{{ gift.converted_serial }}</strong>.
                    </p>
                    <a href="/?serial={{ gift.converted_serial }}" class="btn-primary-wallet" style="margin-bottom:0;">
                        <span>☕</span>
                        <span>Abrir mi Tarjeta de Lealtad en el Hub</span>
                    </a>
                </div>
            {% else %}
                <div style="display:flex; align-items:center; gap:10px; margin-bottom:8px;">
                    <div style="width:40px; height:40px; border-radius:50%; background:var(--sand); border:1px solid rgba(18,18,18,0.08); display:flex; align-items:center; justify-content:center; font-size:20px; flex-shrink:0;">
                        🎁
                    </div>
                    <div>
                        <div style="font-size:11px; font-weight:800; color:var(--olive); text-transform:uppercase; letter-spacing:0.5px;">
                            Beneficio Exclusivo para Ti
                        </div>
                        <h3 style="margin:2px 0 0; font-size:16px; font-weight:800; color:var(--text-dark);">
                            ¡Recibe +1 Sello Gratis en tu Tarjeta de Lealtad!
                        </h3>
                    </div>
                </div>
                <p style="margin:0 0 14px; font-size:12.5px; color:var(--text-muted); line-height:1.45;">
                    Por haber recibido esta Gift Card, regístrate gratis en el programa de lealtad de <strong>{{ campaign.shopName }}</strong> y empieza hoy mismo con <strong>1 sello de bienvenida</strong> rumbo a tu recompensa ({{ campaign.rewardText }}).
                </p>
                <form onsubmit="convertGiftToLoyalty(event)" id="form-convert-loyalty">
                    <div style="margin-bottom:10px;">
                        <label class="field-label">Tu nombre completo</label>
                        <input type="text" id="conv-name" class="input-gift" value="{{ gift.recipient_name }}" placeholder="Ej. Sofía Hernández" required>
                    </div>
                    <div style="margin-bottom:10px;">
                        <label class="field-label">Correo electrónico</label>
                        <input type="email" id="conv-email" class="input-gift" placeholder="tucorreo@ejemplo.com" required>
                    </div>
                    <div style="margin-bottom:10px;">
                        <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:5px;">
                            <label class="field-label" style="margin-bottom:0;">Número de celular</label>
                            <span style="font-size:11px; color:var(--olive); font-weight:700;">Para respaldar tus sellos</span>
                        </div>
                        <input type="tel" id="conv-phone" class="input-gift" value="{{ gift.recipient_phone }}" placeholder="Ej. 642 113 8069" required>
                    </div>

                    <div class="bday-card">
                        <label style="display:block; font-size:12px; font-weight:700; color:var(--text-dark);">
                            🎂 Déjanos tu cumpleaños si quieres recibir un regalo en tu día:
                        </label>
                        <div class="bday-grid">
                            <select id="conv-bday-month" class="input-gift" style="padding:10px 12px; font-size:13px;">
                                <option value="">Mes (Opcional)</option>
                                <option value="01">Enero</option>
                                <option value="02">Febrero</option>
                                <option value="03">Marzo</option>
                                <option value="04">Abril</option>
                                <option value="05">Mayo</option>
                                <option value="06">Junio</option>
                                <option value="07">Julio</option>
                                <option value="08">Agosto</option>
                                <option value="09">Septiembre</option>
                                <option value="10">Octubre</option>
                                <option value="11">Noviembre</option>
                                <option value="12">Diciembre</option>
                            </select>
                            <select id="conv-bday-day" class="input-gift" style="padding:10px 12px; font-size:13px;">
                                <option value="">Día (Opcional)</option>
                                {% for d in range(1, 32) %}
                                <option value="{{ '%02d' % d }}">{{ d }}</option>
                                {% endfor %}
                            </select>
                        </div>
                    </div>

                    <label style="display:flex; align-items:flex-start; gap:10px; padding:4px 2px 12px; cursor:pointer;">
                        <input type="checkbox" id="conv-offers-optin" checked style="margin-top:2px; width:16px; height:16px; accent-color:#121212;">
                        <span style="font-size:11.5px; color:var(--text-muted); line-height:1.35;">
                            Recibir promociones, avisos de sellos y regalos de cumpleaños de {{ campaign.shopName }}. Tus datos son privados y nunca se comparten.
                        </span>
                    </label>

                    <button type="submit" id="btn-convert-submit" class="btn-primary-wallet" style="margin-bottom:0;">
                        <span>✨</span>
                        <span>Obtener mi tarjeta de lealtad (+1 Sello Gratis)</span>
                    </button>
                </form>
            {% endif %}
        </div>

        <div style="text-align:center; margin-top:18px;">
            <a href="/" style="color:var(--text-dark); font-size:13px; font-weight:700; text-decoration:none;">
                ← Ir al Hub de {{ campaign.shopName }} (Menú, Wi-Fi y Recompensas)
            </a>
        </div>
    </div>

    <script>
        const GIFT_CODE = {{ gift.code | tojson }};
        const GIFT_SHARE_URL = {{ (gift.gift_url or '') | tojson }} || window.location.href;
        let lastUpdatedAt = {{ gift.updated_at | tojson }};

        async function copyUnboxGiftLink() {
            const lbl = document.getElementById('unbox-copy-link-lbl');
            const targetUrl = GIFT_SHARE_URL || window.location.href;
            try {
                await navigator.clipboard.writeText(targetUrl);
                if (lbl) lbl.textContent = '¡Link Copiado!';
            } catch (e) {
                const ta = document.createElement('textarea');
                ta.value = targetUrl;
                document.body.appendChild(ta);
                ta.select();
                document.execCommand('copy');
                document.body.removeChild(ta);
                if (lbl) lbl.textContent = '¡Link Copiado!';
            }
            setTimeout(() => {
                if (lbl) lbl.textContent = 'Copiar Link';
            }, 2200);
        }

        // Detección automática de sistema operativo (iOS vs Android) en el navegador
        (function applySmartGiftWalletBtn() {
            const btn = document.getElementById('smart-gift-wallet-btn');
            const icon = document.getElementById('smart-gift-wallet-icon');
            const label = document.getElementById('smart-gift-wallet-label');
            if (!btn || !icon || !label) return;
            const ua = (navigator.userAgent || navigator.vendor || window.opera || '').toLowerCase();
            const uaPlatform = (navigator.userAgentData && navigator.userAgentData.platform ? navigator.userAgentData.platform : '').toLowerCase();
            const isAndroid = /android/.test(ua) || /android/.test(uaPlatform);
            if (isAndroid) {
                btn.href = '/gift/' + encodeURIComponent(GIFT_CODE) + '/google-wallet';
                icon.textContent = '🤖';
                label.textContent = 'Agregar a Google Wallet';
            } else {
                btn.href = '/gift/' + encodeURIComponent(GIFT_CODE) + '/download.pkpass';
                icon.textContent = '🍎';
                label.textContent = 'Agregar a Apple Wallet';
            }
        })();

        async function convertGiftToLoyalty(e) {
            e.preventDefault();
            const btn = document.getElementById('btn-convert-submit');
            const name = (document.getElementById('conv-name').value || '').trim();
            const email = (document.getElementById('conv-email').value || '').trim();
            const phone = (document.getElementById('conv-phone').value || '').trim();
            const month = (document.getElementById('conv-bday-month').value || '').trim();
            const day = (document.getElementById('conv-bday-day').value || '').trim();
            const birthdate = (month && day) ? ('2000-' + month + '-' + day) : '';
            if (!name || !email || !phone) return;
            btn.disabled = true;
            btn.innerHTML = '<span>⏳ Creando tu tarjeta con +1 sello...</span>';
            try {
                const res = await fetch('/api/gift/' + encodeURIComponent(GIFT_CODE) + '/convert-to-loyalty', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name, email, phone, birthdate })
                });
                const data = await res.json();
                if (res.ok && data.ok) {
                    try {
                        localStorage.setItem('maishas_demo_hub_member_v1', JSON.stringify({
                            serial: data.serial,
                            name: data.name,
                            email: data.email || email,
                            phone: data.phone || phone,
                            stamps: data.stamps,
                            stamp_goal: data.stamp_goal || 10
                        }));
                    } catch (err) {}
                    window.location.href = '/?serial=' + encodeURIComponent(data.serial);
                } else {
                    alert(data.error || 'No se pudo crear la tarjeta');
                    btn.disabled = false;
                    btn.innerHTML = '<span>✨</span><span>Obtener mi tarjeta de lealtad (+1 Sello Gratis)</span>';
                }
            } catch (err) {
                btn.disabled = false;
                btn.innerHTML = '<span>✨</span><span>Obtener mi tarjeta de lealtad (+1 Sello Gratis)</span>';
            }
        }

        // Sincronización en vivo cada 3.5 segundos cuando el barista activa, descuenta saldo o canjea en mostrador
        setInterval(async () => {
            if (document.hidden) return;
            try {
                const res = await fetch('/api/gift/' + encodeURIComponent(GIFT_CODE) + '/state');
                if (!res.ok) return;
                const data = await res.json();
                if (data.updated_at && data.updated_at !== lastUpdatedAt) {
                    lastUpdatedAt = data.updated_at;
                    const stripEl = document.getElementById('live-gift-strip');
                    if (stripEl) stripEl.src = '/gift/' + encodeURIComponent(GIFT_CODE) + '/strip.png?v=' + encodeURIComponent(data.updated_at);
                    const hl = document.getElementById('live-gift-headline');
                    if (hl) hl.textContent = data.value_headline;
                    const sub = document.getElementById('live-gift-sub');
                    if (sub) sub.textContent = data.balance_display;
                    const badge = document.getElementById('live-status-badge');
                    if (badge) {
                        if (data.status === 'active') {
                            badge.className = 'status-badge status-active';
                            badge.textContent = '🟢 Activa en Mostrador';
                            const pend = document.getElementById('pending-activation-box');
                            if (pend) pend.style.display = 'none';
                            const actBox = document.getElementById('active-wallet-actions');
                            if (actBox) actBox.style.display = 'block';
                        } else if (data.status === 'redeemed') {
                            badge.className = 'status-badge status-redeemed';
                            badge.textContent = '🎉 Disfrutada al 100%';
                        }
                    }
                }
            } catch (e) {}
        }, 3500);
    </script>
</body>
</html>
"""


def _build_gift_whatsapp_urls(gift_dict):
    base_url = _public_base_url()
    code = gift_dict["code"]
    gift_url = f"{base_url}/gift/{code}"
    shop_name = (get_campaign_config().get("shopName") or "Demo Café").strip()
    val_label = gift_dict["package_title"] if gift_dict["gift_type"] == "combo" else f"${gift_dict['initial_amount']:,.2f} MXN"

    share_msg = (
        f"🎁 ¡Hola {gift_dict['recipient_name']}! {gift_dict['sender_name']} te envió un regalo de *{shop_name}* ({val_label}).\n"
        f"💌 \"{gift_dict['dedication']}\"\n\n"
        f"Ábrelo aquí y guárdalo en tu Wallet:\n{gift_url}"
    )
    activate_msg = (
        f"Hola {shop_name} ☕, solicito activar por transferencia mi Gift Card recién creada:\n"
        f"🔑 Identificador: *{code}*\n"
        f"🎁 Regalo: *{val_label}*\n"
        f"👤 Para: {gift_dict['recipient_name']}\n"
        f"🙌 De parte de: {gift_dict['sender_name']}"
    )
    return {
        "gift_url": gift_url,
        "whatsapp_share_url": f"https://wa.me/?text={urllib_quote(share_msg)}",
        "whatsapp_activate_url": f"https://wa.me/526421138069?text={urllib_quote(activate_msg)}",
    }


@app.route("/api/gift/create", methods=["POST"])
def api_gift_create():
    """
    Crea una Gift Card Digital desde el Hub del cliente.
    Siempre nace en estado 'pending' (⏳ En proceso de activación por el barista)
    hasta que el barista en caja o validando transferencia la active desde /barista.
    """
    data = request.get_json(force=True, silent=True) or {}
    gift_type = (data.get("gift_type") or request.form.get("gift_type") or "balance").strip().lower()
    if gift_type not in ("balance", "combo"):
        gift_type = "balance"

    sender_name = (data.get("sender_name") or request.form.get("sender_name") or "").strip()[:70] or "Alguien especial"
    sender_phone = (data.get("sender_phone") or request.form.get("sender_phone") or "").strip()[:25]
    recipient_name = (data.get("recipient_name") or request.form.get("recipient_name") or "").strip()[:70] or "Para ti"
    recipient_phone = (data.get("recipient_phone") or request.form.get("recipient_phone") or "").strip()[:25]
    dedication = (
        data.get("dedication")
        or data.get("message")
        or request.form.get("dedication")
        or request.form.get("message")
        or ""
    ).strip()[:240]
    if not dedication:
        dedication = f"¡Hola {recipient_name}! Te regalo este detalle especial para que disfrutes en Demo Café ☕✨"

    if gift_type == "combo":
        combos_map = get_gift_combos_map()
        combos_list = get_gift_combos_list()
        default_pkg = combos_list[0] if combos_list else DEFAULT_GIFT_COMBOS_LIST[0]
        raw_pkg_key = (
            data.get("package_key")
            or data.get("combo_key")
            or request.form.get("package_key")
            or request.form.get("combo_key")
            or default_pkg["key"]
        ).strip().lower()
        pkg = combos_map.get(raw_pkg_key) or default_pkg
        pkg_key = pkg["key"]
        pkg_title = pkg["title"]
        pkg_desc = pkg["desc"]
        init_amount = float(pkg["price"])
        curr_balance = float(pkg["price"])
        tot_uses = int(pkg.get("uses", 1))
        rem_uses = int(pkg.get("uses", 1))
    else:
        pkg_key = ""
        try:
            raw_amt = float(data.get("amount") or request.form.get("amount") or 200)
        except Exception:
            raw_amt = 200.0
        init_amount = max(50.0, min(5000.0, round(raw_amt, 2)))
        curr_balance = init_amount
        pkg_title = f"Saldo de Regalo ${init_amount:,.0f} MXN"
        pkg_desc = "Válido en cualquier bebida, frappé, panini o postre del menú"
        tot_uses = 1
        rem_uses = 1

    code = f"GIFT-{uuid.uuid4().hex[:6].upper()}"
    now_utc = get_utc_timestamp()
    now_son = get_sonora_datetime_str()
    status_val = "pending"
    act_son = ""
    detail_str = f"⏳ Creada desde el Hub ({pkg_title}) · En proceso de activación por el barista"

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """INSERT INTO gift_cards (
               code, gift_type, package_key, package_title, package_desc,
               initial_amount, current_balance, total_uses, remaining_uses,
               sender_name, sender_phone, recipient_name, recipient_phone,
               dedication, status, converted_serial, created_at_utc,
               created_at_sonora, activated_at_sonora, updated_at, last_action_detail
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?)""",
        (
            code, gift_type, pkg_key, pkg_title, pkg_desc,
            init_amount, curr_balance, tot_uses, rem_uses,
            sender_name, sender_phone, recipient_name, recipient_phone,
            dedication, status_val, now_utc, now_son, act_son, now_utc, detail_str
        )
    )
    c.execute(
        """INSERT INTO gift_transactions (code, action, amount_delta, balance_after, detail, created_at_utc, created_at_sonora)
           VALUES (?, 'create', ?, ?, ?, ?, ?)""",
        (code, init_amount, curr_balance, detail_str, now_utc, now_son)
    )
    conn.commit()
    conn.close()

    gift_obj = get_gift_card_by_code(code)
    urls = _build_gift_whatsapp_urls(gift_obj)
    return jsonify({
        "ok": True,
        "gift": gift_obj,
        "gift_url": urls["gift_url"],
        "whatsapp_share_url": urls["whatsapp_share_url"],
        "whatsapp_activate_url": urls["whatsapp_activate_url"],
    })


@app.route("/api/gift/<gift_code>/demo-activate", methods=["POST"])
def api_gift_demo_activate(gift_code):
    """Activa una Gift Card en 1 clic (para demostración o confirmación rápida)."""
    gift = get_gift_card_by_code(gift_code)
    if not gift:
        return jsonify({"ok": False, "error": "Gift Card no encontrada"}), 404

    now_utc = get_utc_timestamp()
    now_son = get_sonora_datetime_str()
    detail_str = f"🟢 Gift Card activada y lista para canjear ({now_son})"
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """UPDATE gift_cards
           SET status = 'active', activated_at_sonora = ?, updated_at = ?, last_action_detail = ?
           WHERE code = ?""",
        (now_son, now_utc, detail_str, gift["code"])
    )
    c.execute(
        """INSERT INTO gift_transactions (code, action, amount_delta, balance_after, detail, created_at_utc, created_at_sonora)
           VALUES (?, 'activate', 0, ?, ?, ?, ?)""",
        (gift["code"], gift["current_balance"], detail_str, now_utc, now_son)
    )
    conn.commit()
    conn.close()

    try:
        send_apns_push_for_serial(gift["code"], notification_body="🎁 ¡Tu Gift Card de Demo Café ya está activa y lista para usarse!")
    except Exception:
        pass

    return jsonify({"ok": True, "gift": get_gift_card_by_code(gift["code"])})


@app.route("/gift/<gift_code>")
def gift_unboxing_view(gift_code):
    gift = get_gift_card_by_code(gift_code)
    if not gift:
        return "Gift Card no encontrada", 404
    detected_plat = _detect_client_platform(request)
    return render_template_string(
        GIFT_UNBOXING_HTML,
        gift=gift,
        campaign=get_campaign_config(),
        hub=get_hub_config(),
        is_android=(detected_plat == "android")
    )


@app.route("/gift/<gift_code>/strip.png")
def gift_card_strip_png(gift_code):
    gift = get_gift_card_by_code(gift_code)
    if not gift:
        return "", 404
    img = _create_gift_strip_pil(gift, campaign=get_campaign_config()).resize((750, 288), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    resp = send_file(buf, mimetype="image/png", max_age=0)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/gift/<gift_code>/qr.png")
def gift_card_qr_png(gift_code):
    gift = get_gift_card_by_code(gift_code)
    code_clean = gift["code"] if gift else (gift_code or "").strip().upper()
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=1)
    qr.add_data(code_clean)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#12100E", back_color="#FFFFFF").convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", max_age=3600)


@app.route("/gift/<gift_code>/download.pkpass")
def download_gift_pkpass(gift_code):
    gift = get_gift_card_by_code(gift_code)
    if not gift:
        return "Gift Card no encontrada", 404
    ua = (request.headers.get("User-Agent") or "").lower()
    is_ios = ("iphone" in ua or "ipad" in ua or "ipod" in ua) and ("android" not in ua)
    pkpass_path = generate_signed_gift_pkpass(gift["code"], request.host_url, for_android=not is_ios)
    resp = send_file(
        pkpass_path,
        mimetype="application/vnd.apple.pkpass",
        as_attachment=True,
        download_name=f"GiftCard_{gift['code']}.pkpass"
    )
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    return resp


@app.route("/gift/<gift_code>/google-wallet")
def gift_google_wallet_save(gift_code):
    """Genera un pase en Google Wallet para la Gift Card (o descarga el .pkpass compatible en Android)."""
    gift = get_gift_card_by_code(gift_code)
    if not gift:
        return "Gift Card no encontrada", 404
    if google_wallet_enabled():
        try:
            sa = _load_google_sa()
            base_url = _public_base_url()
            ensure_google_class(base_url)
            obj = build_google_loyalty_object(gift["code"], base_url)
            if obj:
                import threading
                threading.Thread(target=ensure_google_object, args=(gift["code"], base_url), daemon=True).start()
                now = int(datetime.now(timezone.utc).timestamp())
                claims = {
                    "iss": sa["client_email"], "aud": "google", "typ": "savetowallet", "iat": now,
                    "origins": [base_url],
                    "payload": {"loyaltyObjects": [obj]},
                }
                return redirect(f"https://pay.google.com/gp/v/save/{_gw_sign_jwt(claims, sa)}")
        except Exception as e:
            _log_wallet_event(f"⚠️ Error Google Wallet Gift Card ({gift['code']}): {e}")
    return redirect(url_for("download_gift_pkpass", gift_code=gift["code"]))


@app.route("/api/gift/<gift_code>/state")
def api_gift_state(gift_code):
    gift = get_gift_card_by_code(gift_code)
    if not gift:
        return jsonify({"error": "Not found"}), 404
    urls = _build_gift_whatsapp_urls(gift)
    out = dict(gift)
    out.update(urls)
    return jsonify(out)


@app.route("/api/gift/<gift_code>/convert-to-loyalty", methods=["POST"])
def api_gift_convert_to_loyalty(gift_code):
    """
    El 'Truco Maestro': Convierte al destinatario de una Gift Card en un socio del programa de lealtad
    regalándole +1 Sello de Bienvenida (stamps = 1, total_visits = 1) con todos los datos de registro.
    """
    gift = get_gift_card_by_code(gift_code)
    if not gift:
        return jsonify({"ok": False, "error": "Gift Card no encontrada"}), 404

    if gift["converted_serial"]:
        return jsonify({
            "ok": True,
            "serial": gift["converted_serial"],
            "name": gift["recipient_name"],
            "stamps": 1,
            "stamp_goal": get_campaign_config()["stampGoal"],
            "already_converted": True
        })

    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or gift["recipient_name"] or "Socio").strip()[:80]
    phone = (data.get("phone") or gift["recipient_phone"] or "").strip()[:25]
    email = (data.get("email") or "").strip()[:100]
    birthdate = (data.get("birthdate") or "").strip()[:20]

    detected = _detect_client_platform(request) or "ios"
    android_active = 1 if detected == "android" else 0
    now_ts = get_utc_timestamp()
    sonora_now = get_sonora_datetime_str()
    cfg = get_campaign_config()

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Verificar si ya existe por teléfono o correo para sumarle el sello de regalo o crearle su tarjeta nueva con 1 sello
    existing_serial = None
    norm_phone = _normalize_phone_digits(phone)
    email_low = email.lower()
    for row_p in c.execute("SELECT serial, stamps, total_visits, phone, email, birthdate FROM clients WHERE COALESCE(status, 'active') = 'active'").fetchall():
        row_phone_norm = _normalize_phone_digits(row_p[3])
        row_email_low = (row_p[4] or "").strip().lower()
        if (len(norm_phone) == 10 and row_phone_norm == norm_phone) or (email_low and row_email_low == email_low):
            existing_serial = row_p[0]
            new_stamps = min(cfg["stampGoal"], (row_p[1] or 0) + 1)
            new_visits = (row_p[2] or 0) + 1
            upd_email = email if email else (row_p[4] or "")
            upd_bday = birthdate if birthdate else (row_p[5] or "")
            c.execute(
                "UPDATE clients SET stamps = ?, total_visits = ?, email = ?, birthdate = ?, updated_at = ?, message = ? WHERE serial = ?",
                (new_stamps, new_visits, upd_email, upd_bday, now_ts, f"🎁 ¡Recibiste +1 sello de bienvenida por tu Gift Card {gift['code']}!", existing_serial)
            )
            stamps_out = new_stamps
            break

    if not existing_serial:
        existing_serial = f"MAISHAS-{uuid.uuid4().hex[:6].upper()}"
        stamps_out = 1
        welcome_msg = f"🎁 ¡Bienvenido(a) {name}! Empezaste con +1 sello gratis por tu Gift Card."
        c.execute(
            """INSERT INTO clients (serial, name, email, phone, birthdate, stamps, total_visits, redeemed_count, message, updated_at, platform, android_active, created_at, last_action, status, deactivated_at, recovery_pin, recovery_pin_expires)
               VALUES (?, ?, ?, ?, ?, 1, 1, 0, ?, ?, ?, ?, ?, 'add', 'active', '', '', 0)""",
            (existing_serial, name, email, phone, birthdate, welcome_msg, now_ts, detected, android_active, sonora_now)
        )
        c.execute(
            """INSERT INTO stamp_transactions (serial, client_name, action, stamps_before, stamps_after, visits_after, redeemed_after, created_at_utc, created_at_sonora, detail)
               VALUES (?, ?, 'add', 0, 1, 1, 0, ?, ?, ?)""",
            (existing_serial, name, now_ts, sonora_now, f"🎁 +1 Sello de bienvenida al convertir Gift Card {gift['code']}")
        )

    c.execute(
        "UPDATE gift_cards SET converted_serial = ?, updated_at = ? WHERE code = ?",
        (existing_serial, now_ts, gift["code"])
    )
    conn.commit()
    conn.close()

    return jsonify({
        "ok": True,
        "serial": existing_serial,
        "name": name,
        "email": email,
        "phone": phone,
        "stamps": stamps_out,
        "stamp_goal": cfg["stampGoal"],
    })


def _perform_gift_card_action(code_str, action, amount=0.0):
    """
    Ejecuta operaciones de mostrador sobre una Gift Card:
    - 'activate': activa una Gift Card pendiente
    - 'deduct': descuenta un monto en MXN del saldo disponible (o canjea el combo si es de tipo 'combo')
    - 'redeem': liquida el 100% del paquete/combo o del saldo restante
    - 'reload': recarga saldo adicional a la misma Gift Card
    - 'cancel': cancela la Gift Card
    """
    gift = get_gift_card_by_code(code_str)
    if not gift:
        return None, f"⚠️ Gift Card '{code_str}' no encontrada"

    code = gift["code"]
    shop_name = get_campaign_config().get("shopName") or "Demo Café"
    now_utc = get_utc_timestamp()
    now_son = get_sonora_datetime_str()
    act = (action or "deduct").strip().lower()

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    if act == "activate":
        notif = f"🎁 ¡Tu Gift Card de {shop_name} ya está activa y lista para disfrutar!"
        detail = f"🟢 Gift Card {code} activada en mostrador ({now_son}) para {gift['recipient_name']}"
        c.execute(
            "UPDATE gift_cards SET status = 'active', activated_at_sonora = ?, updated_at = ?, last_action_detail = ? WHERE code = ?",
            (now_son, now_utc, notif, code)
        )
        c.execute(
            "INSERT INTO gift_transactions (code, action, amount_delta, balance_after, detail, created_at_utc, created_at_sonora) VALUES (?, 'activate', 0, ?, ?, ?, ?)",
            (code, gift["current_balance"], detail, now_utc, now_son)
        )
        conn.commit()
        conn.close()
        push_res = send_apns_push_for_serial(code, notification_body=notif)
        return code, f"{detail} · {push_res}"

    if act == "cancel":
        detail = f"🗑️ Gift Card {code} cancelada ({now_son})"
        c.execute(
            "UPDATE gift_cards SET status = 'cancelled', updated_at = ?, last_action_detail = ? WHERE code = ?",
            (now_utc, detail, code)
        )
        conn.commit()
        conn.close()
        send_apns_push_for_serial(code, notification_body=f"⚠️ Tu Gift Card {code} ha expirado.")
        return code, detail

    if gift["status"] == "pending":
        conn.close()
        return code, f"⚠️ La Gift Card {code} aún está PENDIENTE de activación. Toca '🟢 Activar Gift Card' primero."

    if gift["gift_type"] == "combo" or act == "redeem":
        if gift["status"] == "redeemed" or (gift["gift_type"] == "balance" and gift["current_balance"] <= 0):
            conn.close()
            return code, f"ℹ️ La Gift Card {code} de {gift['recipient_name']} ya fue canjeada por completo."
        new_bal = 0.0
        delta = -gift["current_balance"]
        if gift["gift_type"] == "combo":
            notif = f"🎉 ¡Canjeaste tu {gift['package_title']} en {shop_name}! Tu Gift Card ha finalizado."
            detail = f"🎉 Combo canjeado en mostrador: {gift['package_title']} para {gift['recipient_name']} ({code})"
        else:
            notif = f"☕️ Se descontaron ${gift['current_balance']:,.2f} MXN de tu Gift Card en {shop_name}. Saldo agotado ($0.00 MXN)."
            detail = f"🎉 Saldo total descontado (${gift['current_balance']:,.2f} MXN) para {gift['recipient_name']} ({code}) · Tarjeta expirada"
        c.execute(
            "UPDATE gift_cards SET current_balance = 0, remaining_uses = 0, status = 'redeemed', updated_at = ?, last_action_detail = ? WHERE code = ?",
            (now_utc, notif, code)
        )
        c.execute(
            "INSERT INTO gift_transactions (code, action, amount_delta, balance_after, detail, created_at_utc, created_at_sonora) VALUES (?, 'redeem', ?, 0, ?, ?, ?)",
            (code, delta, detail, now_utc, now_son)
        )
        conn.commit()
        conn.close()
        push_res = send_apns_push_for_serial(code, notification_body=notif)
        return code, f"{detail} · {push_res}"

    # Descuento parcial de saldo en MXN ('deduct')
    try:
        deduct_amt = round(float(amount or 0.0), 2)
    except Exception:
        deduct_amt = 0.0

    if deduct_amt <= 0:
        conn.close()
        return code, "⚠️ Ingresa un monto en pesos mayor a $0 para descontar de la Gift Card."

    curr_bal = float(gift["current_balance"] or 0.0)
    if curr_bal <= 0:
        conn.close()
        return code, f"⚠️ La Gift Card {code} ya no tiene saldo disponible ($0.00 MXN)."

    if deduct_amt > curr_bal:
        conn.close()
        return code, f"⚠️ El consumo (${deduct_amt:,.2f} MXN) supera el saldo disponible de la Gift Card (${curr_bal:,.2f} MXN)."

    new_bal = round(max(0.0, curr_bal - deduct_amt), 2)
    new_status = "redeemed" if new_bal <= 0 else "active"
    new_rem = 0 if new_bal <= 0 else 1

    if new_bal <= 0:
        notif = f"☕️ Se descontaron ${deduct_amt:,.2f} MXN de tu Gift Card en {shop_name}. Saldo agotado ($0.00 MXN) · Tarjeta finalizada."
        detail = f"✅ Se descontaron ${deduct_amt:,.2f} MXN a {gift['recipient_name']} ({code}) · Saldo agotado ($0.00 MXN · Expirada en Wallet)"
    else:
        notif = f"☕️ Se descontaron ${deduct_amt:,.2f} MXN de tu Gift Card en {shop_name}. Saldo disponible: ${new_bal:,.2f} MXN"
        detail = f"✅ Se descontaron ${deduct_amt:,.2f} MXN a {gift['recipient_name']} ({code}) · Nuevo saldo: ${new_bal:,.2f} MXN"

    c.execute(
        "UPDATE gift_cards SET current_balance = ?, remaining_uses = ?, status = ?, updated_at = ?, last_action_detail = ? WHERE code = ?",
        (new_bal, new_rem, new_status, now_utc, notif, code)
    )
    c.execute(
        "INSERT INTO gift_transactions (code, action, amount_delta, balance_after, detail, created_at_utc, created_at_sonora) VALUES (?, 'deduct', ?, ?, ?, ?, ?)",
        (code, -deduct_amt, new_bal, detail, now_utc, now_son)
    )
    conn.commit()
    conn.close()

    push_res = send_apns_push_for_serial(code, notification_body=notif)
    return code, f"{detail} · {push_res}"


@app.route("/api/barista/gift-action", methods=["POST"])
@requires_auth
def api_barista_gift_action():
    code = (request.form.get("code") or "").strip().upper()
    action = (request.form.get("action") or "deduct").strip().lower()
    amount_raw = request.form.get("amount") or "0"
    code_out, msg = _perform_gift_card_action(code, action, amount=amount_raw)
    return redirect(url_for("barista", ok=msg, gift_updated=code_out))


@app.route("/api/barista/gift-create", methods=["POST"])
@requires_auth
def api_barista_gift_create():
    """Emite una Gift Card ya pagada y activa directamente desde el Mostrador POS del barista."""
    gift_type = (request.form.get("gift_type") or "balance").strip().lower()
    recipient_name = (request.form.get("recipient_name") or "").strip()[:70] or "Cliente Invitado"
    sender_name = (request.form.get("sender_name") or "").strip()[:70] or "Demo Café"
    dedication = (request.form.get("dedication") or "").strip()[:240]
    if not dedication:
        dedication = f"¡Disfruta tu regalo especial en Demo Café, {recipient_name}! ☕✨"

    if gift_type == "combo":
        combos_map = get_gift_combos_map()
        combos_list = get_gift_combos_list()
        default_pkg = combos_list[0] if combos_list else DEFAULT_GIFT_COMBOS_LIST[0]
        pkg_key = (request.form.get("package_key") or default_pkg["key"]).strip().lower()
        pkg = combos_map.get(pkg_key) or default_pkg
        pkg_key = pkg["key"]
        pkg_title = pkg["title"]
        pkg_desc = pkg["desc"]
        init_amount = float(pkg["price"])
    else:
        pkg_key = ""
        try:
            init_amount = max(50.0, min(5000.0, round(float(request.form.get("amount") or 200), 2)))
        except Exception:
            init_amount = 200.0
        pkg_title = f"Saldo de Regalo ${init_amount:,.0f} MXN"
        pkg_desc = "Válido en cualquier bebida, frappé o postre del menú"

    code = f"GIFT-{uuid.uuid4().hex[:6].upper()}"
    now_utc = get_utc_timestamp()
    now_son = get_sonora_datetime_str()
    detail_str = f"🟢 Emitida y pagada en Mostrador POS ({pkg_title}) el {now_son}"

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """INSERT INTO gift_cards (
               code, gift_type, package_key, package_title, package_desc,
               initial_amount, current_balance, total_uses, remaining_uses,
               sender_name, sender_phone, recipient_name, recipient_phone,
               dedication, status, converted_serial, created_at_utc,
               created_at_sonora, activated_at_sonora, updated_at, last_action_detail
           ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, ?, '', ?, '', ?, 'active', '', ?, ?, ?, ?, ?)""",
        (code, gift_type, pkg_key, pkg_title, pkg_desc, init_amount, init_amount, sender_name, recipient_name, dedication, now_utc, now_son, now_son, now_utc, detail_str)
    )
    c.execute(
        """INSERT INTO gift_transactions (code, action, amount_delta, balance_after, detail, created_at_utc, created_at_sonora)
           VALUES (?, 'create', ?, ?, ?, ?, ?)""",
        (code, init_amount, init_amount, detail_str, now_utc, now_son)
    )
    conn.commit()
    conn.close()

    return redirect(url_for("barista", ok=f"🎁 Gift Card {code} ({pkg_title} para {recipient_name}) creada y activada · Lista para enviar por WhatsApp o copiar link", gift_updated=code, gift_scan=code))


@app.route("/api/barista/gift-combos/save", methods=["POST"])
@requires_auth
def api_barista_gift_combos_save():
    """Agrega o actualiza un paquete / experiencia del catálogo 'Selecciona el Paquete / Experiencia'."""
    combo_key_raw = (request.form.get("combo_key") or "").strip().lower()
    icon = (request.form.get("icon") or "🥐").strip()[:4] or "🥐"
    title = (request.form.get("title") or "").strip()[:80]
    desc = (request.form.get("desc") or "").strip()[:160]
    try:
        price = max(10.0, min(10000.0, round(float(request.form.get("price") or 150), 2)))
    except Exception:
        price = 150.0

    if not title:
        return redirect(url_for("barista", ok="⚠️ El nombre del paquete es obligatorio", view="combos"))

    combos = get_gift_combos_list()
    if combo_key_raw:
        key_clean = re.sub(r"[^a-z0-9_]+", "_", combo_key_raw).strip("_") or f"combo_{uuid.uuid4().hex[:6]}"
    else:
        slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:24]
        key_clean = f"{slug}_{uuid.uuid4().hex[:4]}" if slug else f"combo_{uuid.uuid4().hex[:6]}"

    updated_existing = False
    for item in combos:
        if item["key"] == key_clean:
            item["icon"] = icon
            item["title"] = title
            item["desc"] = desc or "Paquete especial"
            item["price"] = price
            updated_existing = True
            break

    if not updated_existing:
        combos.append({
            "key": key_clean,
            "icon": icon,
            "title": title,
            "desc": desc or "Paquete especial",
            "price": price,
            "uses": 1,
        })

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('gift_combos_json', ?)",
        (json.dumps(combos, ensure_ascii=False),)
    )
    conn.commit()
    conn.close()

    action_lbl = "actualizado" if updated_existing else "agregado"
    return redirect(url_for("barista", ok=f"✅ Paquete «{title}» (${price:,.0f} MXN) {action_lbl} en Selecciona el Paquete / Experiencia", view="combos"))


@app.route("/api/barista/gift-combos/delete", methods=["POST"])
@requires_auth
def api_barista_gift_combos_delete():
    """Elimina un paquete / experiencia del catálogo de Gift Cards."""
    combo_key = (request.form.get("combo_key") or "").strip().lower()
    combos = get_gift_combos_list()
    if len(combos) <= 1:
        return redirect(url_for("barista", ok="⚠️ Debe existir al menos 1 paquete activo en el catálogo.", view="combos"))

    removed_title = combo_key
    new_combos = []
    for item in combos:
        if item["key"] == combo_key:
            removed_title = item["title"]
        else:
            new_combos.append(item)

    if not new_combos:
        new_combos = [dict(DEFAULT_GIFT_COMBOS_LIST[0])]

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('gift_combos_json', ?)",
        (json.dumps(new_combos, ensure_ascii=False),)
    )
    conn.commit()
    conn.close()
    return redirect(url_for("barista", ok=f"🗑️ Paquete «{removed_title}» eliminado del catálogo", view="combos"))


# ---------------------------------------------------------
# RUTA PRINCIPAL, BARISTA, EXPORTACIÓN EXCEL Y TARJETA ANDROID
# ---------------------------------------------------------

@app.route("/")
def index():
    hub_tpl_path = os.path.join(BASE_DIR, "hub_template.html")
    if os.path.isfile(hub_tpl_path):
        with open(hub_tpl_path, "r", encoding="utf-8") as fp:
            tpl_str = fp.read()
        return render_template_string(
            tpl_str,
            campaign=get_campaign_config(),
            hub=get_hub_config(),
            gift_combos=get_gift_combos_list(),
        )
    return render_template_string(REGISTRATION_HTML, campaign=get_campaign_config())


@app.route("/registro-directo")
def registro_directo():
    return render_template_string(REGISTRATION_HTML, campaign=get_campaign_config())


@app.route("/api/hub/hero.jpg")
def hub_hero_jpg():
    hero_path = os.path.join(BASE_DIR, "static", "maishas_interior.jpg")
    if os.path.isfile(hero_path):
        return send_file(hero_path, mimetype="image/jpeg", max_age=3600)
    return campaign_logo_png()


@app.route("/api/hub/menu-oficial.png")
def hub_menu_oficial_png():
    menu_path = os.path.join(BASE_DIR, "static", "maishas_menu_oficial.png")
    if os.path.isfile(menu_path):
        return send_file(menu_path, mimetype="image/png", max_age=3600)
    return "", 404


@app.route("/api/hub/wifi-qr.png")
def hub_wifi_qr_png():
    hub_cfg = get_hub_config()
    wifi_payload = f"WIFI:S:{hub_cfg['wifi_ssid']};T:WPA;P:{hub_cfg['wifi_pass']};;"
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=1)
    qr.add_data(wifi_payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#121212", back_color="#FFFFFF").convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", max_age=300)


def _detect_client_platform(req=None):
    """Detecta de forma estricta si la petición proviene de un dispositivo iOS ('ios'), Android ('android') o escritorio (None)."""
    r = req or request
    ua = (r.headers.get("User-Agent") or "").lower()
    ch_plat = (r.headers.get("Sec-CH-UA-Platform") or "").lower()
    if "android" in ua or "android" in ch_plat:
        return "android"
    if "iphone" in ua or "ipad" in ua or "ipod" in ua or "ios" in ch_plat:
        return "ios"
    return None


@app.route("/api/hub/register", methods=["POST"])
def api_hub_register():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or request.form.get("name") or "").strip()
    email = (data.get("email") or request.form.get("email") or "").strip()
    phone = (data.get("phone") or request.form.get("phone") or "").strip()
    birthdate = (data.get("birthdate") or request.form.get("birthdate") or "").strip()

    detected = _detect_client_platform(request)
    platform = "android" if detected == "android" else "ios"
    android_active = 1 if platform == "android" else 0
    now_ts = get_utc_timestamp()
    created_sonora = get_sonora_datetime_str()

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    existing = None
    conflict_field = ""
    norm_phone = _normalize_phone_digits(phone)
    if len(norm_phone) == 10:
        all_with_phone = c.execute(
            "SELECT serial, stamps, total_visits, redeemed_count, message, phone, birthdate, email FROM clients WHERE COALESCE(status, 'active') != 'hidden' AND phone IS NOT NULL AND TRIM(phone) != '' ORDER BY rowid DESC"
        ).fetchall()
        for row_p in all_with_phone:
            if _normalize_phone_digits(row_p[5]) == norm_phone:
                existing = row_p
                conflict_field = "phone"
                break
    if not existing and email:
        existing = c.execute(
            "SELECT serial, stamps, total_visits, redeemed_count, message, phone, birthdate, email FROM clients WHERE COALESCE(status, 'active') != 'hidden' AND LOWER(TRIM(email)) = LOWER(?) ORDER BY rowid DESC LIMIT 1",
            (email,)
        ).fetchone()
        if existing:
            conflict_field = "email"

    if existing:
        existing_serial = existing[0]
        c.execute(
            "INSERT INTO wallet_logs (created_at, log_text) VALUES (?, ?)",
            (now_ts, f"🛡️ Alta bloqueada por seguridad ({conflict_field} ya registrado en {existing_serial})")
        )
        conn.commit()
        conn.close()
        conflict_msg = (
            "Este número de celular ya tiene una tarjeta registrada en Demo Café."
            if conflict_field == "phone"
            else "Este correo electrónico ya tiene una tarjeta registrada en Demo Café."
        )
        return jsonify({
            "ok": False,
            "code": "ALREADY_EXISTS",
            "conflict_field": conflict_field,
            "conflict_value": phone if conflict_field == "phone" else email,
            "error": f"{conflict_msg} Por seguridad y para proteger tus sellos, solicita al barista tu PIN temporal de 4 dígitos para recuperar tu tarjeta."
        }), 409

    serial_number = f"MAISHAS-{uuid.uuid4().hex[:6].upper()}"
    stamps_val = 0
    final_phone = phone
    final_bdate = birthdate
    final_email = email
    c.execute(
        """INSERT INTO clients (serial, name, email, phone, birthdate, stamps, total_visits, redeemed_count, message, updated_at, platform, android_active, created_at, last_action, status, deactivated_at, recovery_pin, recovery_pin_expires)
           VALUES (?, ?, ?, ?, ?, 0, 0, 0, ?, ?, ?, ?, ?, 'add', 'active', '', '', 0)""",
        (serial_number, name or "Cliente", final_email, final_phone, final_bdate, DEFAULT_MESSAGE, now_ts, platform, android_active, created_sonora)
    )

    conn.commit()
    conn.close()

    cfg = get_campaign_config()
    return jsonify({
        "ok": True,
        "serial": serial_number,
        "name": name or "Cliente",
        "email": final_email,
        "phone": final_phone,
        "birthdate": final_bdate,
        "stamps": stamps_val,
        "stamp_goal": cfg["stampGoal"],
        "pkpass_url": f"/card/{serial_number}/download.pkpass",
        "android_card_url": f"/card/{serial_number}?autowallet=1",
        "qr_url": f"/card/{serial_number}/qr.png",
    })


@app.route("/api/barista/recovery-pin", methods=["POST"])
@requires_auth
def api_barista_recovery_pin():
    import secrets
    data = request.get_json(force=True, silent=True) or {}
    serial = (data.get("serial") or request.form.get("serial") or "").strip().upper()
    if not serial:
        return jsonify({"ok": False, "error": "Código de cliente requerido."}), 400

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    row = c.execute(
        "SELECT serial, name, phone FROM clients WHERE UPPER(serial) = ? AND COALESCE(status, 'active') != 'hidden' LIMIT 1",
        (serial,)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "Cliente no encontrado."}), 404

    pin = f"{secrets.randbelow(9000) + 1000}"
    expires_epoch = int(time.time()) + 600  # 10 minutos
    c.execute(
        "UPDATE clients SET recovery_pin = ?, recovery_pin_expires = ? WHERE serial = ?",
        (pin, expires_epoch, row[0])
    )
    c.execute(
        "INSERT INTO wallet_logs (created_at, log_text) VALUES (?, ?)",
        (get_utc_timestamp(), f"🔑 PIN de recuperación generado para {row[1]} ({row[0]}): {pin} (válido 10 min)")
    )
    conn.commit()
    conn.close()

    return jsonify({
        "ok": True,
        "serial": row[0],
        "name": row[1] or "Cliente",
        "phone": row[2] or "",
        "pin": pin,
        "expires_in_seconds": 600
    })


@app.route("/api/hub/lookup", methods=["POST"])
def api_hub_lookup():
    data = request.get_json(force=True, silent=True) or {}
    query = (data.get("query") or "").strip()
    pin = (data.get("pin") or "").strip()
    demo_first = bool(data.get("demo_first"))

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    found = None

    if demo_first and not query:
        found = c.execute(
            "SELECT serial, name, email, phone, birthdate, stamps, recovery_pin, recovery_pin_expires FROM clients WHERE COALESCE(status, 'active') = 'active' ORDER BY rowid ASC LIMIT 1"
        ).fetchone()
        conn.close()
    elif query:
        if not pin or len(pin) != 4 or not pin.isdigit():
            conn.close()
            return jsonify({
                "ok": False,
                "code": "PIN_REQUIRED",
                "error": "Por seguridad, ingresa también el PIN temporal de 4 dígitos que te proporcione el barista en mostrador."
            }), 400

        q_up = query.upper()
        norm_p = _normalize_phone_digits(query)
        found = c.execute(
            "SELECT serial, name, email, phone, birthdate, stamps, recovery_pin, recovery_pin_expires FROM clients WHERE COALESCE(status, 'active') = 'active' AND (UPPER(serial) = ? OR LOWER(TRIM(email)) = LOWER(?)) ORDER BY rowid DESC LIMIT 1",
            (q_up if q_up.startswith("MAISHAS-") else f"MAISHAS-{q_up}", query)
        ).fetchone()
        if not found and len(norm_p) == 10:
            rows = c.execute(
                "SELECT serial, name, email, phone, birthdate, stamps, recovery_pin, recovery_pin_expires FROM clients WHERE COALESCE(status, 'active') = 'active' AND phone IS NOT NULL AND TRIM(phone) != '' ORDER BY rowid DESC"
            ).fetchall()
            for r in rows:
                if _normalize_phone_digits(r[3]) == norm_p:
                    found = r
                    break

        if not found:
            conn.close()
            return jsonify({"ok": False, "error": "No encontramos una tarjeta activa con ese celular o correo."}), 404

        db_pin = (found[6] or "").strip()
        db_exp = int(found[7] or 0)
        now_epoch = int(time.time())

        if not db_pin or db_pin != pin:
            c.execute(
                "INSERT INTO wallet_logs (created_at, log_text) VALUES (?, ?)",
                (get_utc_timestamp(), f"⚠️ Intento fallido de recuperación con PIN inválido para {found[0]}")
            )
            conn.commit()
            conn.close()
            return jsonify({
                "ok": False,
                "code": "PIN_INVALID",
                "error": "El PIN de 4 dígitos es incorrecto. Acércate con el barista para pedir tu código de recuperación vigente."
            }), 403

        if now_epoch > db_exp:
            conn.close()
            return jsonify({
                "ok": False,
                "code": "PIN_EXPIRED",
                "error": "El PIN temporal ya expiró (tiene vigencia de 10 minutos). Pide al barista que genere uno nuevo."
            }), 403

        # Consumir el PIN tras el uso exitoso (One-Time Password) y actualizar plataforma según el dispositivo actual
        detected = _detect_client_platform(request)
        if detected == "android":
            c.execute(
                "UPDATE clients SET recovery_pin = '', recovery_pin_expires = 0, platform = 'android', android_active = 1, last_sync_at = ? WHERE serial = ?",
                (get_short_time(), found[0])
            )
        elif detected == "ios":
            c.execute(
                "UPDATE clients SET recovery_pin = '', recovery_pin_expires = 0, platform = 'ios', android_active = 0, last_sync_at = ? WHERE serial = ?",
                (get_short_time(), found[0])
            )
        else:
            c.execute(
                "UPDATE clients SET recovery_pin = '', recovery_pin_expires = 0, last_sync_at = ? WHERE serial = ?",
                (get_short_time(), found[0])
            )
        c.execute(
            "INSERT INTO wallet_logs (created_at, log_text) VALUES (?, ?)",
            (get_utc_timestamp(), f"✅ Tarjeta recuperada con PIN verificado ({'Android' if detected == 'android' else 'iPhone'}): {found[1]} ({found[0]})")
        )
        conn.commit()
        conn.close()
    else:
        conn.close()

    if not found:
        return jsonify({"ok": False, "error": "No encontramos una tarjeta activa con ese dato."}), 404

    cfg = get_campaign_config()
    return jsonify({
        "ok": True,
        "serial": found[0],
        "name": found[1] or "Cliente",
        "email": found[2] or "",
        "phone": found[3] or "",
        "birthdate": found[4] or "",
        "stamps": found[5] or 0,
        "stamp_goal": cfg["stampGoal"],
        "pkpass_url": f"/card/{found[0]}/download.pkpass",
        "android_card_url": f"/card/{found[0]}?autowallet=1",
        "qr_url": f"/card/{found[0]}/qr.png",
    })


@app.route("/api/hub/staff-stamp", methods=["POST"])
def api_hub_staff_stamp():
    data = request.get_json(force=True, silent=True) or {}
    serial = (data.get("serial") or "").strip().upper()
    action = (data.get("action") or "add").strip().lower()
    if action not in ("add", "sub", "reset"):
        action = "add"
    serial_clean, status_msg = _perform_stamp_update(serial, action=action, force_duplicate=True)
    if not serial_clean:
        return jsonify({"ok": False, "error": status_msg}), 404
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT name, stamps, total_visits, redeemed_count FROM clients WHERE serial = ?", (serial_clean,)).fetchone()
    conn.close()
    cfg = get_campaign_config()
    return jsonify({
        "ok": True,
        "serial": serial_clean,
        "name": row[0] if row else "Cliente",
        "stamps": (row[1] or 0) if row else 0,
        "stamp_goal": cfg["stampGoal"],
        "message": status_msg,
    })


@app.route("/api/hub/feedback", methods=["POST"])
def api_hub_feedback():
    data = request.get_json(force=True, silent=True) or {}
    serial = (data.get("serial") or "").strip().upper()
    message = (data.get("message") or "").strip()[:1000]
    sentiment = (data.get("sentiment") or "constructive").strip()
    has_photo = 1 if data.get("has_photo") else 0
    if not message:
        return jsonify({"ok": False}), 400
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO hub_feedback (serial, message, sentiment, has_photo, created_at_sonora) VALUES (?, ?, ?, ?, ?)",
        (serial, message, sentiment, has_photo, get_sonora_datetime_str())
    )
    c.execute(
        "INSERT INTO wallet_logs (created_at, log_text) VALUES (?, ?)",
        (get_utc_timestamp(), f"💬 Buzón de Sugerencias ({sentiment}): {message[:100]}")
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "sentiment": sentiment})


@app.route("/api/hub/settings", methods=["POST"])
@requires_auth
def api_hub_settings():
    fields = {
        "hub_slogan": (request.form.get("slogan") or "").strip()[:120],
        "hub_wifi_ssid": (request.form.get("wifi_ssid") or "").strip()[:80],
        "hub_wifi_pass": (request.form.get("wifi_pass") or "").strip()[:80],
        "hub_google_review_url": (request.form.get("google_review_url") or "").strip()[:300],
        "hub_instagram_handle": (request.form.get("instagram_handle") or "").strip()[:60],
    }
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    for k, v in fields.items():
        if v:
            c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()
    return redirect(url_for("barista", ok="✅ Ajustes del Hub de Experiencia en Mesa actualizados"))


@app.route("/barista/login", methods=["GET", "POST"])
def barista_login():
    if request.method == "GET":
        if is_barista_authenticated():
            return redirect("/barista")
        return authenticate()
    ip = _client_ip()
    if _is_login_rate_limited(ip):
        return authenticate(
            error="⏳ Demasiados intentos fallidos. Por seguridad espera 1 minuto antes de volver a intentar.",
            status_code=429
        )
    username = request.form.get("username", "")
    password = request.form.get("password", "")
    if check_auth(username, password):
        _clear_login_failures(ip)
        resp = redirect("/barista")
        resp.set_cookie(
            BARISTA_COOKIE_NAME,
            _barista_expected_token(),
            max_age=31536000,
            httponly=True,
            samesite="Lax"
        )
        return resp
    _record_login_failure(ip)
    return authenticate(error="Usuario o contraseña incorrectos.")


@app.route("/barista/logout")
def barista_logout():
    resp = redirect("/barista")
    resp.delete_cookie(BARISTA_COOKIE_NAME)
    return resp


@app.route("/barista")
@requires_auth
def barista():
    # Ejecutar verificación automática de cumpleaños (si el check está activo y son >= 8:00 AM Hora Sonora)
    run_auto_birthday_dispatch(force_now=False)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT c.serial, c.name, c.email, c.stamps, c.push_token, c.message,
               c.phone, c.birthdate, c.last_push_status, c.last_sync_at,
               (SELECT COUNT(*) FROM registrations r WHERE r.serial = c.serial) as reg_count,
               c.total_visits, c.redeemed_count, c.android_active,
               (SELECT COUNT(*) FROM android_subscriptions a WHERE a.serial = c.serial) as and_sub_count,
               c.created_at,
               COALESCE(c.status, 'active') as status,
               COALESCE(c.deactivated_at, '') as deactivated_at,
               COALESCE(c.platform, 'ios') as platform
        FROM clients c
        ORDER BY c.rowid DESC
    """)
    rows = c.fetchall()

    # Contar notificaciones emergentes de Google Wallet en las últimas 24 horas por cliente
    cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S UTC")
    gw_push_counts = {}
    try:
        gw_rows = c.execute(
            "SELECT log_text FROM wallet_logs WHERE created_at >= ? AND log_text LIKE '🟢 Google Wallet: pase y notificación enviados a %'",
            (cutoff_24h,)
        ).fetchall()
        for (ltxt,) in gw_rows:
            m_ser = re.search(r"MAISHAS-[A-Z0-9]{6}", ltxt or "")
            if m_ser:
                s_key = m_ser.group(0)
                gw_push_counts[s_key] = gw_push_counts.get(s_key, 0) + 1
    except Exception:
        pass

    # Obtener timestamp UTC del último sello (+1) por cliente para la protección Anti-Doble Escaneo (< 60s)
    last_add_map = {}
    try:
        for ser_k, max_utc in c.execute(
            "SELECT serial, MAX(created_at_utc) FROM stamp_transactions WHERE action = 'add' GROUP BY serial"
        ).fetchall():
            if ser_k and max_utc:
                for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                    try:
                        dt_u = datetime.strptime(max_utc, fmt).replace(tzinfo=timezone.utc)
                        last_add_map[ser_k] = round(dt_u.timestamp(), 2)
                        break
                    except Exception:
                        pass
    except Exception:
        pass

    # Calcular Resumen de Corte de Turno del día de HOY en Hora Sonora (Punto B #7)
    now_son = datetime.now(SONORA_TZ)
    today_date_label = f"{now_son.day:02d}/{MESES_ES[now_son.month]}/{now_son.year}"
    stamps_today_count = 0
    sub_today_count = 0
    redeemed_today_count = 0
    try:
        today_tx_rows = c.execute("""
            SELECT action, COUNT(*)
            FROM stamp_transactions
            WHERE serial NOT IN (SELECT serial FROM clients WHERE COALESCE(status, 'active') = 'hidden')
              AND created_at_sonora LIKE ?
            GROUP BY action
        """, (f"{today_date_label}%",)).fetchall()
        tx_counts_map = dict(today_tx_rows)
        stamps_today_count = int(tx_counts_map.get("add", 0))
        sub_today_count = int(tx_counts_map.get("sub", 0))
        redeemed_today_count = int(tx_counts_map.get("reset", 0))
    except Exception:
        pass

    # Cargar últimos 50 movimientos de la bitácora de auditoría (excluyendo clientes ocultos definitivamente)
    recent_transactions = []
    try:
        tx_rows = c.execute("""
            SELECT serial, client_name, action, stamps_before, stamps_after, visits_after, redeemed_after, created_at_sonora, detail
            FROM stamp_transactions
            WHERE serial NOT IN (SELECT serial FROM clients WHERE COALESCE(status, 'active') = 'hidden')
              AND action != 'purge'
            ORDER BY id DESC
            LIMIT 50
        """).fetchall()
        for tx in tx_rows:
            recent_transactions.append({
                "serial": tx[0],
                "client_name": tx[1],
                "action": tx[2],
                "stamps_before": tx[3] or 0,
                "stamps_after": tx[4] or 0,
                "visits_after": tx[5] or 0,
                "redeemed_after": tx[6] or 0,
                "created_at_sonora": tx[7] or "",
                "detail": tx[8] or "",
            })
    except Exception:
        pass

    # Cargar mensajes recibidos en el Buzón de Sugerencias (tabla hub_feedback)
    feedback_list = []
    try:
        fb_rows = c.execute("""
            SELECT f.id, COALESCE(f.serial, ''), COALESCE(c.name, ''), f.message, COALESCE(f.sentiment, 'constructive'), COALESCE(f.created_at_sonora, '')
            FROM hub_feedback f
            LEFT JOIN clients c ON c.serial = f.serial
            ORDER BY f.id DESC
            LIMIT 100
        """).fetchall()
        for fbr in fb_rows:
            feedback_list.append({
                "id": fbr[0],
                "serial": fbr[1] or "",
                "client_name": fbr[2] or "",
                "message": fbr[3] or "",
                "sentiment": fbr[4] or "constructive",
                "created_at_sonora": fbr[5] or "",
            })
    except Exception:
        pass

    conn.close()

    campaign_cfg = get_campaign_config()
    stamp_goal = campaign_cfg["stampGoal"]
    bday_auto_cfg = get_birthday_auto_config()

    clients = []
    inactive_clients = []
    upcoming_birthdays = []
    installed_count = 0
    gold_count = 0
    silver_count = 0
    total_redeemed = 0
    ready_to_redeem_count = 0
    month_birthdays_count = 0
    new_clients_today_count = 0
    client_lookup_by_serial = {}

    for r in rows:
        status_val = (r[16] or "active").strip().lower()
        deactivated_at_val = format_sonora_time(r[17] or "")
        # Si fue eliminado definitivamente de la vista del barista ('hidden'), permanece en SQLite pero no se muestra en ningún lado del sistema
        if status_val == "hidden":
            continue

        reg_count = r[10] or 0
        and_sub_count = r[14] or 0
        platform_val = (r[18] or "ios").strip().lower()
        has_ios_passkit = bool(r[4]) or reg_count > 0
        android_installed = (bool(r[13]) and platform_val == "android") or (and_sub_count > 0)
        ios_installed = has_ios_passkit or (platform_val == "ios" and not android_installed)
        is_installed = ios_installed or android_installed
        stamps_val = r[3] or 0
        visits_val = r[11] if r[11] is not None else stamps_val
        redeemed_val = r[12] or 0
        tier = get_vip_tier(visits_val)

        full_name = (r[1] or "Cliente").strip()
        name_parts = [p for p in full_name.split() if p]
        if len(name_parts) >= 2:
            initials = (name_parts[0][0] + name_parts[1][0]).upper()
        elif name_parts:
            initials = name_parts[0][:2].upper()
        else:
            initials = "MC"
        first_name = name_parts[0] if name_parts else full_name

        bday_info = calculate_birthday_info(r[7] or "")
        created_at_clean = format_sonora_time(r[15] or "")
        if created_at_clean.startswith(today_date_label):
            new_clients_today_count += 1
        last_push_clean = humanize_push_status(r[8] or "")
        last_sync_clean = format_sonora_time(r[9] or "")

        client_obj = {
            "serial": r[0],
            "name": full_name,
            "first_name": first_name,
            "initials": initials,
            "email": r[2] or "",
            "stamps": stamps_val,
            "push_token": r[4],
            "message": (r[5] or "").replace("\u200b", ""),
            "phone": r[6] or "",
            "birthdate": r[7] or "",
            "bday": bday_info,
            "last_push_status": r[8] or "",
            "last_push_clean": last_push_clean,
            "last_sync_at": r[9] or "",
            "last_sync_clean": last_sync_clean,
            "created_at": created_at_clean,
            "ios_count": max(reg_count, 1 if r[4] else 0),
            "ios_installed": ios_installed,
            "android_installed": android_installed,
            "is_installed": is_installed,
            "total_visits": visits_val,
            "redeemed_count": redeemed_val,
            "tier": tier,
            "gw_pushes_24h": gw_push_counts.get(r[0], 0),
            "status": status_val,
            "deactivated_at": deactivated_at_val,
            "last_add_epoch": last_add_map.get(r[0], 0),
        }
        client_lookup_by_serial[r[0]] = client_obj

        if status_val == "inactive":
            inactive_clients.append(client_obj)
            continue

        if is_installed:
            installed_count += 1
        if stamps_val >= stamp_goal:
            ready_to_redeem_count += 1
        total_redeemed += redeemed_val
        if visits_val >= 25:
            gold_count += 1
        elif visits_val >= 10:
            silver_count += 1
        if bday_info["is_this_month"]:
            month_birthdays_count += 1

        clients.append(client_obj)
        if bday_info["is_upcoming_week"]:
            upcoming_birthdays.append(client_obj)

    upcoming_birthdays.sort(key=lambda x: x["bday"]["days_until"])

    shift_summary = {
        "date_label": today_date_label,
        "stamps_today": stamps_today_count,
        "sub_today": sub_today_count,
        "redeemed_today": redeemed_today_count,
        "new_clients_today": new_clients_today_count,
    }

    dup_confirm = None
    dup_serial_arg = (request.args.get("dup_serial") or "").strip().upper()
    dup_secs_arg = request.args.get("dup_secs")
    dup_src_arg = request.args.get("dup_src", "pos")
    if dup_serial_arg and dup_serial_arg in client_lookup_by_serial:
        dc_obj = client_lookup_by_serial[dup_serial_arg]
        try:
            secs_val = max(1, int(float(dup_secs_arg or 1)))
        except Exception:
            secs_val = 1
        dup_confirm = {
            "serial": dup_serial_arg,
            "name": dc_obj["name"],
            "stamps": dc_obj["stamps"],
            "secs_ago": secs_val,
            "form_id": "scan-form" if dup_src_arg == "scan" else f"form-add-{dup_serial_arg}",
        }

    gift_cards_list = list_gift_cards_for_barista()
    gift_scan_arg = (request.args.get("gift_scan") or "").strip().upper()
    gift_scan_obj = get_gift_card_by_code(gift_scan_arg) if gift_scan_arg else None

    html_out = render_template_string(
        BARISTA_HTML,
        clients=clients,
        inactive_clients=inactive_clients,
        upcoming_birthdays=upcoming_birthdays,
        month_birthdays_count=month_birthdays_count,
        ready_to_redeem_count=ready_to_redeem_count,
        installed_count=installed_count,
        gold_count=gold_count,
        silver_count=silver_count,
        total_redeemed=total_redeemed,
        shop_loc=get_shop_location(),
        campaign=campaign_cfg,
        hub=get_hub_config(),
        bday_auto=bday_auto_cfg,
        shift_summary=shift_summary,
        server_now_epoch=round(datetime.now(timezone.utc).timestamp(), 2),
        dup_confirm=dup_confirm,
        recent_transactions=recent_transactions,
        feedback_list=feedback_list,
        gift_cards=gift_cards_list,
        gift_combos=get_gift_combos_list(),
        gift_scan_obj=gift_scan_obj,
        gift_updated=request.args.get("gift_updated"),
        active_view=request.args.get("view", ""),
        ok=request.args.get("ok"),
        updated_serial=request.args.get("updated")
    )
    resp = Response(html_out, mimetype="text/html")
    resp.set_cookie(
        BARISTA_COOKIE_NAME,
        _barista_expected_token(),
        max_age=31536000,
        httponly=True,
        samesite="Lax"
    )
    return resp


@app.route("/api/delete-feedback", methods=["POST"])
@requires_auth
def api_delete_feedback():
    fb_id = (request.form.get("feedback_id") or "").strip()
    if fb_id.isdigit():
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM hub_feedback WHERE id = ?", (int(fb_id),))
        conn.commit()
        conn.close()
    return redirect(url_for("barista", ok="🗑️ Mensaje eliminado del Buzón de Sugerencias"))


@app.route("/api/export-clients.csv")
@requires_auth
def export_clients_csv():
    """Descarga el machote completo de clientes en formato CSV compatible con Microsoft Excel (UTF-8 con BOM)."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT c.serial, c.name, c.email, c.phone, c.birthdate, c.created_at,
               c.stamps, c.total_visits, c.redeemed_count, c.message,
               c.last_sync_at, c.last_push_status, c.push_token, c.android_active,
               (SELECT COUNT(*) FROM registrations r WHERE r.serial = c.serial) as reg_count,
               (SELECT COUNT(*) FROM android_subscriptions a WHERE a.serial = c.serial) as and_sub_count,
               COALESCE(c.status, 'active') as status,
               COALESCE(c.platform, 'ios') as platform
        FROM clients c
        WHERE COALESCE(c.status, 'active') != 'hidden'
        ORDER BY c.rowid DESC
    """)
    rows = c.fetchall()
    conn.close()

    output = io.StringIO()
    # Escribir BOM UTF-8 para que Microsoft Excel reconozca acentos y eñes automáticamente
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow([
        "Código Serial",
        "Nombre del Cliente",
        "Estado de Cuenta",
        "Número de Celular",
        "Correo Electrónico",
        "Fecha de Nacimiento",
        "Fecha de Alta (Sonora)",
        "Dispositivo Activo",
        "Sellos Actuales",
        "Visitas Históricas",
        "Cafés Gratis Canjeados",
        "Nivel VIP",
        "Último Aviso en Tarjeta",
        "Última Actividad (Sonora)"
    ])

    for r in rows:
        serial, name, email, phone, bdate, created_at, stamps, visits, redeemed, msg, sync_at, push_st, p_tok, and_act, reg_c, and_c, status_val, platform_val = r
        plat_clean = (platform_val or "ios").strip().lower()
        has_ios_pk = bool(p_tok) or ((reg_c or 0) > 0)
        and_on = (bool(and_act) and plat_clean == "android") or ((and_c or 0) > 0)
        ios_on = has_ios_pk or (plat_clean == "ios" and not and_on)
        if ios_on and and_on:
            dev_label = "iPhone + Android"
        elif ios_on:
            dev_label = "iPhone (Apple Wallet)"
        elif and_on:
            dev_label = "Android Wallet"
        else:
            dev_label = "Pendiente de instalar"

        stamps_v = stamps or 0
        visits_v = visits if visits is not None else stamps_v
        redeemed_v = redeemed or 0
        tier_name = get_vip_tier(visits_v)["name"]
        clean_msg = (msg or DEFAULT_MESSAGE).replace("\u200b", "")
        created_clean = format_sonora_time(created_at or "")
        act_clean = format_sonora_time(sync_at or "") or humanize_push_status(push_st or "")
        status_label = "Desactivado (Papelera)" if status_val == "inactive" else "Activo"

        writer.writerow([
            serial or "",
            name or "",
            status_label,
            phone or "",
            email or "",
            bdate or "",
            created_clean,
            dev_label,
            stamps_v,
            visits_v,
            redeemed_v,
            tier_name,
            clean_msg,
            act_clean
        ])

    csv_bytes = output.getvalue().encode("utf-8")
    now_str = datetime.now(SONORA_TZ).strftime("%Y-%m-%d")
    return Response(
        csv_bytes,
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="Clientes_DemoCafe_{now_str}.csv"'
        }
    )


@app.route("/api/register", methods=["POST"])
def register():
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    birthdate = (request.form.get("birthdate") or "").strip()
    platform_req = (request.form.get("platform") or "").strip().lower()

    ua = (request.headers.get("User-Agent") or "").lower()
    ch_plat = (request.headers.get("Sec-CH-UA-Platform") or "").lower()
    if platform_req == "android" or "android" in ua or "android" in ch_plat:
        platform = "android"
    else:
        platform = "ios"

    now_ts = get_utc_timestamp()
    created_sonora = get_sonora_datetime_str()
    android_active = 1 if platform == "android" else 0

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    existing = None
    norm_phone = _normalize_phone_digits(phone)
    if len(norm_phone) == 10:
        all_with_phone = c.execute(
            "SELECT serial, stamps, total_visits, redeemed_count, message, phone, birthdate, email FROM clients WHERE COALESCE(status, 'active') != 'hidden' AND phone IS NOT NULL AND TRIM(phone) != '' ORDER BY rowid DESC"
        ).fetchall()
        for row_p in all_with_phone:
            if _normalize_phone_digits(row_p[5]) == norm_phone:
                existing = row_p
                break
    if not existing and email:
        existing = c.execute(
            "SELECT serial, stamps, total_visits, redeemed_count, message, phone, birthdate, email FROM clients WHERE COALESCE(status, 'active') != 'hidden' AND LOWER(TRIM(email)) = LOWER(?) ORDER BY rowid DESC LIMIT 1",
            (email,)
        ).fetchone()

    if existing:
        conn.close()
        return redirect("/?recover=1")

    serial_number = f"MAISHAS-{uuid.uuid4().hex[:6].upper()}"
    stamps_val = 0
    visits_val = 0
    redeemed_val = 0
    msg_val = DEFAULT_MESSAGE
    final_phone = phone
    final_bdate = birthdate
    c.execute(
        """INSERT INTO clients (serial, name, email, phone, birthdate, stamps, total_visits, redeemed_count, message, updated_at, platform, android_active, created_at, last_action, status, deactivated_at, recovery_pin, recovery_pin_expires)
           VALUES (?, ?, ?, ?, ?, 0, 0, 0, ?, ?, ?, ?, ?, 'add', 'active', '', '', 0)""",
        (serial_number, name, email, final_phone, final_bdate, msg_val, now_ts, platform, android_active, created_sonora)
    )

    conn.commit()
    conn.close()

    # Si es Android, redirigir a su Tarjeta Digital y abrir automáticamente Google Wallet
    if platform == "android":
        return redirect(url_for("android_card_view", serial_number=serial_number, autowallet=1))

    host_url = request.host_url
    pkpass_path = generate_signed_pkpass(
        serial_number, name, stamps_val, host_url, msg_val,
        phone=final_phone, birthdate=final_bdate, total_visits=visits_val, redeemed_count=redeemed_val,
        last_action="add", for_android=False
    )
    resp = send_file(
        pkpass_path,
        mimetype="application/vnd.apple.pkpass",
        as_attachment=True,
        download_name=f"DemoCafe_{serial_number}.pkpass"
    )
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    return resp


# ---------------------------------------------------------
# ENDPOINTS DE TARJETA DIGITAL ANDROID (PWA, QR, STRIP, WEB PUSH & LIVE SYNC)
# ---------------------------------------------------------

@app.route("/card/<serial_number>")
def android_card_view(serial_number):
    serial_clean = (serial_number or "").strip().upper()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT serial, name, email, stamps, message, phone, birthdate, total_visits, redeemed_count, updated_at, COALESCE(status, 'active') FROM clients WHERE UPPER(serial) = ?",
        (serial_clean,)
    )
    row = c.fetchone()
    if not row or row[10] != "active":
        conn.close()
        return "Tarjeta no encontrada o desactivada", 404

    serial_id = row[0]
    detected = _detect_client_platform(request)
    if detected == "android":
        c.execute("UPDATE clients SET platform = 'android', android_active = 1, last_sync_at = ? WHERE serial = ?", (get_short_time(), serial_id))
        conn.commit()
    elif detected == "ios":
        c.execute("UPDATE clients SET platform = 'ios', android_active = 0, last_sync_at = ? WHERE serial = ?", (get_short_time(), serial_id))
        conn.commit()
    conn.close()

    cfg = get_campaign_config()
    goal = cfg["stampGoal"]
    stamps_val = row[3] or 0
    visits_val = row[7] if row[7] is not None else stamps_val
    redeemed_val = row[8] or 0
    tier = get_vip_tier(visits_val)
    reward_status = "¡RECOMPENSA LISTA! 🎉" if stamps_val >= goal else f"Faltan {goal - stamps_val} sellos"

    client_data = {
        "serial": serial_id,
        "name": row[1] or "Cliente",
        "email": row[2] or "",
        "stamps": stamps_val,
        "message": (row[4] or DEFAULT_MESSAGE).replace("\u200b", ""),
        "phone": row[5] or "",
        "birthdate": row[6] or "",
        "total_visits": visits_val,
        "redeemed_count": redeemed_val,
        "updated_at": row[9] or "",
        "tier": tier,
        "reward_status": reward_status,
    }
    gw_enabled = google_wallet_enabled()
    gw_save_url = f"/card/{serial_id}/google-wallet"
    if gw_enabled:
        try:
            direct_url, _ = google_wallet_save_url(serial_id, _public_base_url())
            if direct_url:
                gw_save_url = direct_url
        except Exception as gw_err:
            _log_wallet_event(f"⚠️ Google Wallet save_url error ({serial_id}): {str(gw_err)[:200]}")

    return render_template_string(
        ANDROID_CARD_HTML,
        client=client_data,
        shop_loc=get_shop_location(),
        campaign=cfg,
        vapid_public_key=VAPID_PUBLIC_KEY,
        gw_enabled=gw_enabled,
        gw_save_url=gw_save_url
    )


@app.route("/card/<serial_number>/strip.png")
def android_card_strip(serial_number):
    serial_clean = (serial_number or "").strip().upper()
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT stamps, message FROM clients WHERE UPPER(serial) = ?", (serial_clean,)).fetchone()
    conn.close()
    stamps_val = row[0] if row else 0
    msg_val = row[1] if row else DEFAULT_MESSAGE
    img = _create_dynamic_strip_pil(stamps_val, message=msg_val, campaign=get_campaign_config()).resize((750, 250), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    resp = send_file(buf, mimetype="image/png", max_age=0)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/card/<serial_number>/qr.png")
def android_card_qr(serial_number):
    serial_clean = (serial_number or "").strip().upper()
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=1)
    qr.add_data(serial_clean)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#12100E", back_color="#FFFFFF").convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", max_age=3600)


@app.route("/card/<serial_number>/download.pkpass")
def download_pkpass_direct(serial_number):
    serial_clean = (serial_number or "").strip().upper()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    row = c.execute(
        "SELECT serial, name, stamps, message, phone, birthdate, total_visits, redeemed_count, last_action FROM clients WHERE UPPER(serial) = ?",
        (serial_clean,)
    ).fetchone()
    if not row:
        conn.close()
        return "No encontrado", 404

    ua = (request.headers.get("User-Agent") or "").lower()
    is_ios = ("iphone" in ua or "ipad" in ua or "ipod" in ua) and ("android" not in ua)
    if is_ios:
        c.execute(
            "UPDATE clients SET platform = 'ios', android_active = 0, last_sync_at = ? WHERE serial = ?",
            (get_short_time(), row[0])
        )
        conn.commit()
    conn.close()

    pkpass_path = generate_signed_pkpass(
        row[0], row[1], row[2] or 0, request.host_url, row[3],
        phone=row[4], birthdate=row[5], total_visits=row[6], redeemed_count=row[7] or 0,
        last_action=row[8] or "add",
        for_android=not is_ios
    )
    resp = send_file(
        pkpass_path,
        mimetype="application/vnd.apple.pkpass",
        as_attachment=True,
        download_name=f"DemoCafe_{row[0]}.pkpass"
    )
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    return resp


@app.route("/api/campaign/logo.png")
def campaign_logo_png():
    logo_path = _active_logo_file_path()
    img = Image.open(logo_path).convert("RGBA").resize((320, 320), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    resp = send_file(buf, mimetype="image/png", max_age=3600)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@app.route("/api/campaign/strip-bg.png")
def campaign_strip_bg_png():
    if os.path.isfile(CUSTOM_STRIP_BG_PATH):
        return send_file(CUSTOM_STRIP_BG_PATH, mimetype="image/png", max_age=0)
    return "", 404


@app.route("/card/icon-192.png")
def card_icon_192():
    icon_path = _active_logo_file_path()
    img = Image.open(icon_path).convert("RGBA").resize((192, 192), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", max_age=3600)


@app.route("/barista/icon-512.png")
def barista_icon_512():
    icon_path = _active_logo_file_path()
    img = Image.open(icon_path).convert("RGBA").resize((512, 512), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", max_age=3600)


@app.route("/barista/manifest.webmanifest")
def barista_manifest():
    cfg = get_campaign_config()
    manifest_data = {
        "id": "/barista",
        "name": f"{cfg['shopName']} — Consola Barista POS (DEMO)",
        "short_name": "Barista DEMO",
        "description": f"Consola de punto de venta, escáner QR y sellos de {cfg['shopName']}",
        "start_url": "/barista",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#F4ECE6",
        "theme_color": "#F4ECE6",
        "icons": [
            {"src": "/card/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/barista/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}
        ]
    }
    return Response(json.dumps(manifest_data), mimetype="application/manifest+json")


@app.route("/card/<serial_number>/manifest.webmanifest")
def android_manifest(serial_number):
    serial_clean = (serial_number or "").strip().upper()
    cfg = get_campaign_config()
    manifest_data = {
        "name": f"{cfg['shopName']} — Tarjeta de Fidelidad",
        "short_name": cfg["shopName"],
        "start_url": f"/card/{serial_clean}",
        "display": "standalone",
        "background_color": cfg["backgroundColor"],
        "theme_color": cfg["backgroundColor"],
        "icons": [
            {"src": "/card/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/barista/icon-512.png", "sizes": "512x512", "type": "image/png"}
        ]
    }
    return Response(json.dumps(manifest_data), mimetype="application/manifest+json")


@app.route("/sw.js")
def service_worker_js():
    sw_code = """
self.addEventListener('install', event => { self.skipWaiting(); });
self.addEventListener('activate', event => { event.waitUntil(self.clients.claim()); });
self.addEventListener('fetch', event => {});

self.addEventListener('push', event => {
    let data = {
        title: 'Demo Café ☕️',
        body: 'Tu tarjeta de fidelidad se ha actualizado.',
        url: '/',
        pkpass_url: '',
        image: '',
        serial: ''
    };
    if (event.data) {
        try { data = Object.assign(data, event.data.json()); }
        catch (e) { data.body = event.data.text(); }
    }
    const options = {
        body: data.body,
        icon: '/card/icon-192.png',
        badge: '/card/icon-192.png',
        vibrate: [200, 100, 200],
        tag: data.tag || ('maishas-push-' + Date.now()),
        renotify: true,
        requireInteraction: true,
        data: {
            url: data.url || '/',
            pkpass_url: data.pkpass_url || '',
            serial: data.serial || ''
        },
        actions: [
            { action: 'open_card', title: '📲 Ver Tarjeta en Vivo' },
            { action: 'update_pkpass', title: '🔄 Actualizar Wallet' }
        ]
    };
    if (data.image) {
        options.image = data.image;
    }
    event.waitUntil(
        Promise.all([
            self.registration.showNotification(data.title, options),
            self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(windowClients => {
                for (let client of windowClients) {
                    client.postMessage({ type: 'MAISHAS_PUSH', data: data });
                }
            })
        ])
    );
});

self.addEventListener('notificationclick', event => {
    event.notification.close();
    const notifData = event.notification.data || {};
    let targetUrl = notifData.url || '/';
    if (event.action === 'update_pkpass' && notifData.serial) {
        targetUrl = '/card/' + notifData.serial + '?auto_pkpass=1&t=' + Date.now();
    }
    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then(windowClients => {
            if (event.action !== 'update_pkpass') {
                for (let client of windowClients) {
                    if (notifData.serial && client.url.includes('/card/' + notifData.serial) && 'focus' in client) {
                        client.postMessage({ type: 'MAISHAS_PUSH', data: notifData });
                        return client.focus();
                    }
                }
            }
            if (clients.openWindow) return clients.openWindow(targetUrl);
        })
    );
});
"""
    resp = Response(sw_code.strip(), mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/api/android/heartbeat/<serial_number>", methods=["POST"])
def android_heartbeat(serial_number):
    serial_clean = (serial_number or "").strip().upper()
    detected = _detect_client_platform(request)
    conn = sqlite3.connect(DB_PATH)
    if detected == "android":
        conn.execute("UPDATE clients SET platform = 'android', android_active = 1, last_sync_at = ? WHERE UPPER(serial) = ?", (get_short_time(), serial_clean))
    elif detected == "ios":
        conn.execute("UPDATE clients SET platform = 'ios', android_active = 0, last_sync_at = ? WHERE UPPER(serial) = ?", (get_short_time(), serial_clean))
    else:
        conn.execute("UPDATE clients SET last_sync_at = ? WHERE UPPER(serial) = ?", (get_short_time(), serial_clean))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/android/subscribe/<serial_number>", methods=["POST"])
def android_subscribe(serial_number):
    serial_clean = (serial_number or "").strip().upper()
    payload = request.get_json(force=True, silent=True) or {}
    sub = payload.get("subscription") or {}
    endpoint = sub.get("endpoint")
    if not endpoint:
        return jsonify({"ok": False, "error": "Missing endpoint"}), 400

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO android_subscriptions (serial, endpoint, sub_json, created_at) VALUES (?, ?, ?, ?)",
        (serial_clean, endpoint, json.dumps(sub), get_utc_timestamp())
    )
    c.execute(
        "UPDATE clients SET platform = 'android', android_active = 1, last_sync_at = ?, last_push_status = ? WHERE UPPER(serial) = ?",
        (get_short_time(), f"✅ Push Android activado ({get_short_time()})", serial_clean)
    )
    c.execute(
        "INSERT INTO wallet_logs (created_at, log_text) VALUES (?, ?)",
        (get_utc_timestamp(), f"🔔 Suscripción Web Push Android registrada para {serial_clean}")
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/android/state/<serial_number>")
def android_card_state(serial_number):
    serial_clean = (serial_number or "").strip().upper()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT serial, name, stamps, message, total_visits, redeemed_count, updated_at, COALESCE(status, 'active') FROM clients WHERE UPPER(serial) = ?",
        (serial_clean,)
    )
    row = c.fetchone()
    if not row or row[7] != "active":
        conn.close()
        return jsonify({"error": "Not found or inactive"}), 404

    # Distinguir si la sincronización proviene de un teléfono Android real o del Hub en un iPhone/escritorio
    detected = _detect_client_platform(request)
    if detected == "android":
        c.execute("UPDATE clients SET platform = 'android', android_active = 1, last_sync_at = ? WHERE serial = ?", (get_short_time(), row[0]))
    elif detected == "ios":
        c.execute("UPDATE clients SET platform = 'ios', android_active = 0, last_sync_at = ? WHERE serial = ?", (get_short_time(), row[0]))
    else:
        c.execute("UPDATE clients SET last_sync_at = ? WHERE serial = ?", (get_short_time(), row[0]))
    conn.commit()
    conn.close()

    cfg = get_campaign_config()
    goal = cfg["stampGoal"]
    stamps_val = row[2] or 0
    visits_val = row[4] if row[4] is not None else stamps_val
    redeemed_val = row[5] or 0
    tier = get_vip_tier(visits_val)
    reward_status = "¡RECOMPENSA LISTA! 🎉" if stamps_val >= goal else f"Faltan {goal - stamps_val} sellos"
    clean_msg = (row[3] or DEFAULT_MESSAGE).replace("\u200b", "")

    return jsonify({
        "serial": row[0],
        "name": row[1],
        "stamps": stamps_val,
        "stamp_goal": goal,
        "message": clean_msg,
        "tier_name": tier["name"],
        "reward_status": reward_status,
        "history_text": f"Nivel: {tier['name']} | Visitas históricas: {visits_val} | Canjes: {redeemed_val} ({tier['next_goal']})",
        "updated_at": row[6] or ""
    })


def _perform_stamp_update(serial, action="add", force_duplicate=False):
    """Función auxiliar compartida entre el botón de la tabla y el escáner QR."""
    raw_s = (serial or "").strip().upper()
    m_ser = re.search(r"MAISHAS-[A-Z0-9]{6}", raw_s)
    if m_ser:
        serial_clean = m_ser.group(0)
    elif re.fullmatch(r"[A-Z0-9]{6}", raw_s):
        serial_clean = f"MAISHAS-{raw_s}"
    else:
        serial_clean = raw_s
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT serial, name, stamps, total_visits, redeemed_count, birthdate, COALESCE(status, 'active') FROM clients WHERE UPPER(serial) = ?",
        (serial_clean,)
    )
    client = c.fetchone()

    if not client or client[6] == "hidden":
        conn.close()
        return None, f"⚠️ Código QR / Cliente '{serial_clean}' no encontrado"

    if client[6] == "inactive":
        conn.close()
        return None, f"📦 El cliente {client[1]} ({client[0]}) está desactivado en la Papelera. Reactívalo en '📊 Base de Datos' para sumarle sellos."

    cfg = get_campaign_config()
    goal = cfg["stampGoal"]
    shop_name = cfg["shopName"]
    reward_text = cfg["rewardText"]

    serial_id, client_name = client[0], client[1]
    current_stamps = client[2] or 0
    current_visits = client[3] if client[3] is not None else current_stamps
    current_redeemed = client[4] or 0
    bday_info = calculate_birthday_info(client[5] or "")
    bday_alert = ""
    if bday_info["is_today"]:
        bday_alert = " · 🎂 ¡HOY ES SU CUMPLEAÑOS!"
    elif bday_info["is_upcoming_week"]:
        bday_alert = f" · 🎂 Cumple {bday_info['badge_text']}"

    if action == "sub":
        new_stamps = max(0, current_stamps - 1)
        new_visits = max(0, current_visits - 1)
        new_redeemed = current_redeemed
        action_label = f"Ajuste aplicado a {client_name} ({serial_id}): saldo actual {new_stamps}/{goal}"
        notif_body = f"☕️ Tu tarjeta {shop_name} está al día. Sellos acumulados: {new_stamps} / {goal}"
    elif action == "reset":
        new_stamps = 0
        new_visits = current_visits
        new_redeemed = current_redeemed + 1
        action_label = f"🎁 Recompensa canjeada para {client_name} ({serial_id}): reiniciado a 0/{goal} (Total canjes: {new_redeemed}){bday_alert}"
        notif_body = f"🎁 ¡Disfruta tu recompensa ({reward_text})! Tu tarjeta se ha reiniciado a 0 / {goal}."
    else:
        action = "add"
        if current_stamps >= goal:
            conn.close()
            return serial_id, f"ℹ️ {client_name} ({serial_id}) ya tiene {goal}/{goal} sellos. Usa '🎁 Canjear' para entregar su recompensa.{bday_alert}"

        # Protección Anti-Doble Escaneo / Sello Duplicado (< 60 segundos) si no viene confirmado explícitamente
        if not force_duplicate:
            last_add_row = c.execute(
                "SELECT created_at_utc FROM stamp_transactions WHERE serial = ? AND action = 'add' ORDER BY id DESC LIMIT 1",
                (serial_id,)
            ).fetchone()
            if last_add_row and last_add_row[0]:
                for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                    try:
                        dt_prev = datetime.strptime(last_add_row[0], fmt).replace(tzinfo=timezone.utc)
                        diff_sec = (datetime.now(timezone.utc) - dt_prev).total_seconds()
                        if 0 <= diff_sec < 60:
                            secs_ago = max(1, int(round(diff_sec)))
                            conn.close()
                            return serial_id, f"DUPLICATE_COOLDOWN:{secs_ago}"
                        break
                    except Exception:
                        pass

        new_stamps = current_stamps + 1
        new_visits = current_visits + 1
        new_redeemed = current_redeemed
        old_tier = get_vip_tier(current_visits)["name"]
        new_tier = get_vip_tier(new_visits)["name"]
        tier_note = f" — 🏆 ¡Subió a {new_tier}!" if new_tier != old_tier else ""
        action_label = f"✅ Sello agregado a {client_name} ({serial_id}): ahora tiene {new_stamps}/{goal} ({new_visits} visitas){tier_note}{bday_alert}"
        if new_stamps >= goal:
            notif_body = f"🎉 ¡Felicidades! Completaste {goal}/{goal} sellos. ¡Tienes {reward_text} disponible en {shop_name}!"
        else:
            notif_body = f"☕️ ¡Has ganado 1 nuevo sello en {shop_name}! Total: {new_stamps} / {goal}"

    now_ts = get_utc_timestamp()
    c.execute(
        "UPDATE clients SET stamps = ?, total_visits = ?, redeemed_count = ?, updated_at = ?, last_action = ? WHERE serial = ?",
        (new_stamps, new_visits, new_redeemed, now_ts, action, serial_id)
    )
    try:
        c.execute(
            """INSERT INTO stamp_transactions (serial, client_name, action, stamps_before, stamps_after, visits_after, redeemed_after, created_at_utc, created_at_sonora, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (serial_id, client_name, action, current_stamps, new_stamps, new_visits, new_redeemed, now_ts, get_sonora_datetime_str(), action_label)
        )
    except Exception:
        pass
    conn.commit()
    conn.close()

    push_summary = send_apns_push_for_serial(serial_id, notification_body=notif_body)
    return serial_id, f"{action_label} · {push_summary}"


@app.route("/api/add-stamp", methods=["POST"])
@requires_auth
def add_stamp():
    serial = request.form.get("serial")
    action = request.form.get("action", "add")
    force_dup = str(request.form.get("force_duplicate", "0")).strip() == "1"
    serial_id, msg = _perform_stamp_update(serial, action, force_duplicate=force_dup)
    if isinstance(msg, str) and msg.startswith("DUPLICATE_COOLDOWN:"):
        secs_ago = msg.split(":", 1)[1]
        return redirect(url_for("barista", dup_serial=serial_id, dup_secs=secs_ago, dup_src="pos", updated=serial_id))
    return redirect(url_for("barista", ok=msg, updated=serial_id))


@app.route("/api/scan-action", methods=["POST"])
@requires_auth
def scan_action():
    serial = (request.form.get("serial") or "").strip().upper()
    m_gift = re.search(r"GIFT-[A-Z0-9]{6}", serial)
    if m_gift or serial.startswith("GIFT-"):
        g_code = m_gift.group(0) if m_gift else serial
        gift_obj = get_gift_card_by_code(g_code)
        if gift_obj:
            return redirect(url_for("barista", ok=f"🎁 Gift Card detectada: {gift_obj['code']} ({gift_obj['recipient_name']})", gift_scan=gift_obj["code"]))
        return redirect(url_for("barista", ok=f"⚠️ Gift Card '{g_code}' no encontrada"))

    action = request.form.get("action", "add")
    force_dup = str(request.form.get("force_duplicate", "0")).strip() == "1"
    serial_id, msg = _perform_stamp_update(serial, action, force_duplicate=force_dup)
    if isinstance(msg, str) and msg.startswith("DUPLICATE_COOLDOWN:"):
        secs_ago = msg.split(":", 1)[1]
        return redirect(url_for("barista", dup_serial=serial_id, dup_secs=secs_ago, dup_src="scan", updated=serial_id))
    return redirect(url_for("barista", ok=f"📷 QR Escaneado: {msg}", updated=serial_id))


@app.route("/api/birthday-auto-settings", methods=["GET", "POST"])
@requires_auth
def api_birthday_auto_settings():
    """Activa o desactiva la función automática de felicitación de cumpleaños a las 8:00 AM (Hora Sonora)."""
    if request.method == "GET":
        return jsonify({"ok": True, "bday_auto": get_birthday_auto_config()})

    payload = request.get_json(force=True, silent=True) or {}
    enabled = bool(payload.get("enabled", True))
    raw_tpl = (payload.get("template") or "").strip()
    template = raw_tpl[:190] if raw_tpl else DEFAULT_BDAY_AUTO_TEMPLATE

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('bday_auto_enabled', ?)", ("1" if enabled else "0",))
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('bday_auto_template', ?)", (template,))
    conn.commit()
    conn.close()

    dispatched = 0
    if enabled:
        dispatched = run_auto_birthday_dispatch(force_now=False)

    status_msg = (
        f"✅ Función automática de cumpleaños ACTIVADA (8:00 AM Hora Sonora){f' · {dispatched} felicitación(es) enviada(s) hoy' if dispatched else ''}."
        if enabled
        else "⏸️ Función automática de cumpleaños DESACTIVADA (solo envío manual)."
    )
    return jsonify({
        "ok": True,
        "enabled": enabled,
        "template": template,
        "dispatched_today": dispatched,
        "message": status_msg,
    })


def _decode_data_url_to_png(data_url, out_path, resize_max=None):
    """Decodifica un data:image/...;base64 y lo guarda como PNG optimizado."""
    if not data_url or not data_url.startswith("data:image/"):
        return False
    try:
        _, b64_part = data_url.split(",", 1)
        raw_bytes = base64.b64decode(b64_part)
        img = Image.open(io.BytesIO(raw_bytes)).convert("RGBA")
        if resize_max and (img.width > resize_max[0] or img.height > resize_max[1]):
            img.thumbnail(resize_max, Image.Resampling.LANCZOS)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        img.save(out_path, format="PNG", optimize=True)
        return True
    except Exception as e:
        print(f"⚠️ Error guardando imagen de campaña: {e}")
        return False


@app.route("/api/campaign-settings", methods=["GET", "POST"])
@requires_auth
def api_campaign_settings():
    """Obtiene o guarda la configuración completa de la plantilla de la campaña."""
    if request.method == "GET":
        return jsonify({"ok": True, "campaign": get_campaign_config()})

    payload = request.get_json(force=True, silent=True) or {}
    current = get_campaign_config()

    shop_name = (payload.get("shopName") or current["shopName"] or "Demo Café").strip()[:40]
    bg_hex = (payload.get("backgroundColor") or current["backgroundColor"] or "#12100E").strip()
    fg_hex = (payload.get("foregroundColor") or current["foregroundColor"] or "#F5F2EB").strip()
    lbl_hex = (payload.get("labelColor") or current["labelColor"] or "#D4AF37").strip()
    try:
        stamp_goal = max(4, min(12, int(payload.get("stampGoal") or current["stampGoal"] or 10)))
    except Exception:
        stamp_goal = 10
    reward_text = (payload.get("rewardText") or current["rewardText"]).strip()[:80]
    terms_text = (payload.get("termsText") or current["termsText"]).strip()[:500]
    menu_url = (payload.get("menuUrl") if "menuUrl" in payload else current["menuUrl"]).strip()[:200]
    use_custom_strip = bool(payload.get("useCustomStrip", current.get("useCustomStrip", False)))

    try:
        lat = float(payload.get("lat", current["lat"]))
        lng = float(payload.get("lng", current["lng"]))
    except Exception:
        lat, lng = current["lat"], current["lng"]
    relevant_text = (payload.get("relevantText") or current["relevantText"] or DEFAULT_GEO_TEXT).strip()[:160]

    logo_data_url = payload.get("logoDataUrl") or ""
    strip_data_url = payload.get("stripDataUrl") or ""
    if logo_data_url.startswith("data:image/"):
        _decode_data_url_to_png(logo_data_url, CUSTOM_LOGO_PATH, resize_max=(660, 660))
    if strip_data_url.startswith("data:image/"):
        _decode_data_url_to_png(strip_data_url, CUSTOM_STRIP_BG_PATH, resize_max=(1125, 432))
        use_custom_strip = True

    updated_cfg = {
        "shopName": shop_name,
        "backgroundColor": bg_hex if re.fullmatch(r"#[0-9A-Fa-f]{6}", bg_hex) else "#12100E",
        "foregroundColor": fg_hex if re.fullmatch(r"#[0-9A-Fa-f]{6}", fg_hex) else "#F5F2EB",
        "labelColor": lbl_hex if re.fullmatch(r"#[0-9A-Fa-f]{6}", lbl_hex) else "#D4AF37",
        "stampGoal": stamp_goal,
        "rewardText": reward_text,
        "termsText": terms_text,
        "menuUrl": menu_url,
        "lat": lat,
        "lng": lng,
        "relevantText": relevant_text,
        "useCustomStrip": use_custom_strip,
    }

    now_ts = get_utc_timestamp()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('campaign_config_json', ?)", (json.dumps(updated_cfg),))
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('shop_lat', ?)", (str(lat),))
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('shop_lng', ?)", (str(lng),))
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('shop_relevant_text', ?)", (relevant_text,))
    c.execute("UPDATE clients SET updated_at = ? WHERE COALESCE(status, 'active') = 'active'", (now_ts,))
    serials = [r[0] for r in c.execute("SELECT serial FROM clients WHERE COALESCE(status, 'active') = 'active'").fetchall()]
    conn.commit()
    conn.close()

    # Sincronizar clase en Google Wallet con los nuevos colores, logo y nombre
    if google_wallet_enabled():
        try:
            ensure_google_class(_public_base_url(), force_update=True)
        except Exception:
            pass

    pushed = 0
    for s in serials:
        res = send_apns_push_for_serial(s, notification_body=f"✨ Tarjeta {shop_name} actualizada")
        if "✅" in res:
            pushed += 1

    return jsonify({
        "ok": True,
        "message": f"✅ Plantilla '{shop_name}' guardada (Meta: {stamp_goal} sellos) y sincronizada con {pushed} dispositivo(s).",
        "campaign": get_campaign_config()
    })


@app.route("/api/update-location", methods=["POST"])
@requires_auth
def update_location():
    lat_raw = (request.form.get("lat") or "").strip()
    lng_raw = (request.form.get("lng") or "").strip()
    relevant_text = (request.form.get("relevant_text") or "").strip()[:160] or DEFAULT_GEO_TEXT
    try:
        lat = float(lat_raw)
        lng = float(lng_raw)
    except ValueError:
        return redirect(url_for("barista", ok="⚠️ Latitud y Longitud deben ser números válidos (ej. 27.0706, -109.4437)"))

    now_ts = get_utc_timestamp()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('shop_lat', ?)", (str(lat),))
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('shop_lng', ?)", (str(lng),))
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('shop_relevant_text', ?)", (relevant_text,))
    c.execute("UPDATE clients SET updated_at = ? WHERE COALESCE(status, 'active') = 'active'", (now_ts,))
    serials = [r[0] for r in c.execute("SELECT serial FROM clients WHERE COALESCE(status, 'active') = 'active'").fetchall()]
    conn.commit()
    conn.close()

    pushed = 0
    for s in serials:
        res = send_apns_push_for_serial(s, notification_body=relevant_text)
        if "✅" in res:
            pushed += 1

    return redirect(url_for(
        "barista",
        ok=f"📍 Ubicación GPS guardada ({lat:.5f}, {lng:.5f}) y sincronizada con {pushed} teléfono(s)"
    ))


@app.route("/api/update-client", methods=["POST"])
@requires_auth
def update_client():
    serial = (request.form.get("serial") or "").strip().upper()
    name = (request.form.get("name") or "").strip()[:80]
    phone = (request.form.get("phone") or "").strip()[:25]
    email = (request.form.get("email") or "").strip()[:100]
    birthdate = (request.form.get("birthdate") or "").strip()[:20]

    if not serial or not name:
        return redirect(url_for("barista", ok="⚠️ El nombre del cliente es obligatorio"))

    now_ts = get_utc_timestamp()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    row = c.execute("SELECT serial FROM clients WHERE UPPER(serial) = ? AND COALESCE(status, 'active') != 'hidden'", (serial,)).fetchone()
    if not row:
        conn.close()
        return redirect(url_for("barista", ok=f"⚠️ Cliente {serial} no encontrado"))

    serial_id = row[0]
    c.execute(
        "UPDATE clients SET name = ?, phone = ?, email = ?, birthdate = ?, updated_at = ? WHERE serial = ?",
        (name, phone, email, birthdate, now_ts, serial_id)
    )
    conn.commit()
    conn.close()

    send_apns_push_for_serial(serial_id, notification_body="✨ Tus datos de socio fueron actualizados en Demo Café")
    return redirect(url_for("barista", ok=f"✏️ Datos de {name} ({serial_id}) actualizados correctamente", updated=serial_id))


@app.route("/api/transactions")
@requires_auth
def api_transactions():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT id, serial, client_name, action, stamps_before, stamps_after, visits_after, redeemed_after, created_at_sonora, detail
        FROM stamp_transactions
        WHERE serial NOT IN (SELECT serial FROM clients WHERE COALESCE(status, 'active') = 'hidden')
          AND action != 'purge'
        ORDER BY id DESC
        LIMIT 100
    """).fetchall()
    conn.close()
    return jsonify([
        {
            "id": r[0],
            "serial": r[1],
            "client_name": r[2],
            "action": r[3],
            "stamps_before": r[4],
            "stamps_after": r[5],
            "visits_after": r[6],
            "redeemed_after": r[7],
            "created_at_sonora": r[8],
            "detail": r[9],
        }
        for r in rows
    ])


@app.route("/api/delete-client", methods=["POST"])
@requires_auth
def delete_client():
    """
    Soft-Delete de 2 niveles (NUNCA ejecuta DELETE FROM clients ni DELETE FROM stamp_transactions):
    - mode='deactivate' (por defecto): mueve al cliente a la Papelera (status='inactive'), ocultándolo del Mostrador POS y desactivando su pase en Wallet, pero permitiendo reactivarlo.
    - mode='purge' ('Eliminar Definitivamente' desde Papelera): cambia status='hidden' y desvincula dispositivos Push. El registro y su historial quedan resguardados permanentemente en SQLite como desactivados, pero ya no son visibles en ninguna parte del panel de barista.
    """
    serial = (request.form.get("serial") or "").strip().upper()
    mode = (request.form.get("mode") or "deactivate").strip().lower()
    now_ts = get_utc_timestamp()
    sonora_now = get_sonora_datetime_str()

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    row = c.execute(
        "SELECT serial, name, stamps, total_visits, redeemed_count, COALESCE(status, 'active'), COALESCE(deactivated_at, '') FROM clients WHERE UPPER(serial) = ?",
        (serial,)
    ).fetchone()
    if not row:
        conn.close()
        return redirect(url_for("barista", ok=f"⚠️ Cliente {serial} no encontrado"))

    serial_id, name, stamps, visits, redeemed, _, prev_deact = row
    stamps_val = stamps or 0
    visits_val = visits if visits is not None else stamps_val
    redeemed_val = redeemed or 0

    if mode == "purge":
        final_deact = prev_deact if prev_deact else sonora_now
        c.execute(
            """UPDATE clients
               SET status = 'hidden', deactivated_at = ?, push_token = NULL, android_active = 0, updated_at = ?
               WHERE serial = ?""",
            (final_deact, now_ts, serial_id)
        )
        # Sólo desvinculamos los tokens de envío Push para que jamás reciba alertas, pero NUNCA borramos clients ni stamp_transactions
        c.execute("DELETE FROM registrations WHERE UPPER(serial) = ?", (serial_id,))
        c.execute("DELETE FROM android_subscriptions WHERE UPPER(serial) = ?", (serial_id,))
        try:
            c.execute(
                """INSERT INTO stamp_transactions (serial, client_name, action, stamps_before, stamps_after, visits_after, redeemed_after, created_at_utc, created_at_sonora, detail)
                   VALUES (?, ?, 'purge', ?, ?, ?, ?, ?, ?, ?)""",
                (serial_id, name, stamps_val, stamps_val, visits_val, redeemed_val, now_ts, sonora_now, f"🗑️ Ocultado definitivamente del panel (resguardado en BD): {name} ({serial_id})")
            )
        except Exception:
            pass
        conn.commit()
        conn.close()

        if google_wallet_enabled():
            try:
                google_wallet_push_update(serial_id, _public_base_url(), notify=False)
            except Exception:
                pass

        return redirect(url_for("barista", ok=f"🗑️ Cliente {name} ({serial_id}) eliminado definitivamente del sistema"))

    # Nivel 1: Desactivar y enviar a la Papelera (status = 'inactive')
    c.execute(
        "UPDATE clients SET status = 'inactive', deactivated_at = ?, updated_at = ? WHERE serial = ?",
        (sonora_now, now_ts, serial_id)
    )
    try:
        c.execute(
            """INSERT INTO stamp_transactions (serial, client_name, action, stamps_before, stamps_after, visits_after, redeemed_after, created_at_utc, created_at_sonora, detail)
               VALUES (?, ?, 'deactivate', ?, ?, ?, ?, ?, ?, ?)""",
            (serial_id, name, stamps_val, stamps_val, visits_val, redeemed_val, now_ts, sonora_now, f"📦 Cliente enviado a Papelera: {name} ({serial_id})")
        )
    except Exception:
        pass
    conn.commit()
    conn.close()

    # Notificar silenciosamente a Apple/Google Wallet para que el pase quede marcado como inactivo
    try:
        send_apns_push_for_serial(serial_id, notification_body="Tu tarjeta ha sido pausada temporalmente")
    except Exception:
        pass

    return redirect(url_for(
        "barista",
        ok=f"📦 Cliente {name} ({serial_id}) desactivado y movido a la Papelera (conserva sus {stamps_val} sellos en Base de Datos)"
    ))


@app.route("/api/reactivate-client", methods=["POST"])
@requires_auth
def reactivate_client():
    """Restaura un cliente de la Papelera (status='inactive') de vuelta al Mostrador POS activo (status='active')."""
    serial = (request.form.get("serial") or "").strip().upper()
    now_ts = get_utc_timestamp()
    sonora_now = get_sonora_datetime_str()

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    row = c.execute(
        "SELECT serial, name, stamps, total_visits, redeemed_count, COALESCE(status, 'active') FROM clients WHERE UPPER(serial) = ?",
        (serial,)
    ).fetchone()
    if not row or row[5] == "hidden":
        conn.close()
        return redirect(url_for("barista", ok=f"⚠️ Cliente {serial} no encontrado"))

    serial_id, name, stamps, visits, redeemed, _ = row
    stamps_val = stamps or 0
    visits_val = visits if visits is not None else stamps_val
    redeemed_val = redeemed or 0

    c.execute(
        "UPDATE clients SET status = 'active', deactivated_at = '', updated_at = ? WHERE serial = ?",
        (now_ts, serial_id)
    )
    try:
        c.execute(
            """INSERT INTO stamp_transactions (serial, client_name, action, stamps_before, stamps_after, visits_after, redeemed_after, created_at_utc, created_at_sonora, detail)
               VALUES (?, ?, 'reactivate', ?, ?, ?, ?, ?, ?, ?)""",
            (serial_id, name, stamps_val, stamps_val, visits_val, redeemed_val, now_ts, sonora_now, f"♻️ Cliente reactivado desde Papelera: {name} ({serial_id})")
        )
    except Exception:
        pass
    conn.commit()
    conn.close()

    try:
        send_apns_push_for_serial(serial_id, notification_body="♻️ ¡Tu tarjeta de lealtad está activa de nuevo!")
    except Exception:
        pass

    return redirect(url_for(
        "barista",
        ok=f"♻️ Cliente {name} ({serial_id}) reactivado en el Mostrador con sus {stamps_val} sellos intactos",
        updated=serial_id
    ))


@app.route("/api/send-message", methods=["POST"])
@requires_auth
def send_message():
    raw_message = (request.form.get("message") or "").strip()[:200]
    target = request.form.get("target", "all")
    is_bday = (request.form.get("is_bday") or "") == "1"
    if not raw_message:
        return redirect(url_for("barista", ok="⚠️ Escribe un mensaje antes de enviar"))

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if target == "all":
        c.execute("SELECT serial, name, message FROM clients WHERE COALESCE(status, 'active') = 'active'")
    else:
        c.execute("SELECT serial, name, message FROM clients WHERE serial = ? AND COALESCE(status, 'active') = 'active'", (target,))
    rows = c.fetchall()

    now_ts = get_utc_timestamp()
    now_sonora_year = datetime.now(SONORA_TZ).year
    sonora_dt_str = get_sonora_datetime_str()
    for serial_id, _, prev_msg in rows:
        msg_to_save = raw_message
        if prev_msg == raw_message:
            msg_to_save = raw_message + "\u200b"
        c.execute(
            "UPDATE clients SET message = ?, updated_at = ? WHERE serial = ?",
            (msg_to_save, now_ts, serial_id)
        )
        if is_bday and target != "all":
            c.execute(
                "INSERT OR REPLACE INTO birthday_push_logs (serial, year, sent_at_sonora, mode) VALUES (?, ?, ?, ?)",
                (serial_id, now_sonora_year, sonora_dt_str, "manual")
            )
    conn.commit()
    conn.close()

    pushed_ok = 0
    last_res = ""
    for serial_id, _, _ in rows:
        last_res = send_apns_push_for_serial(serial_id, notification_body=f"📣 {raw_message}")
        if "✅" in last_res:
            pushed_ok += 1

    if target == "all":
        msg_status = f"📣 Aviso enviado a {pushed_ok} teléfono(s) activos ({get_short_time()})"
        return redirect(url_for("barista", ok=msg_status))
    else:
        target_name = rows[0][1] if rows else target
        return redirect(url_for("barista", ok=f"📣 Aviso enviado a {target_name} · {last_res}", updated=target))


# ---------------------------------------------------------
# APPLE WALLET WEB SERVICE PROTOCOL & ANDROID WEB PUSH DISPATCHER
# ---------------------------------------------------------

def is_authorized():
    auth = request.headers.get("Authorization", "").strip()
    return (AUTH_TOKEN in auth) or auth in (
        f"ApplePass {AUTH_TOKEN}",
        f"AndroidPass {AUTH_TOKEN}",
        f"AttidoPass {AUTH_TOKEN}",
    ) or auth.endswith(f" {AUTH_TOKEN}")


@app.route("/v1/devices/<device_id>/registrations/<pass_type_id>/<serial_number>", methods=["POST"])
def register_device(device_id, pass_type_id, serial_number):
    if not is_authorized():
        return "", 401
    payload = request.get_json(force=True, silent=True) or {}
    push_token = (payload.get("pushToken") or payload.get("push_token") or "").strip()
    push_service_url = (payload.get("pushServiceUrl") or payload.get("push_service_url") or "").strip()
    if not push_token:
        return "", 400

    serial_clean = (serial_number or "").strip().upper()
    auth_hdr = request.headers.get("Authorization", "")
    ua_hdr = (request.headers.get("User-Agent") or "").lower()
    is_android_wallet_app = (
        auth_hdr.startswith("AndroidPass")
        or auth_hdr.startswith("AttidoPass")
        or "android" in ua_hdr
        or "walletpasses" in ua_hdr
        or bool(push_service_url)
    )

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM registrations WHERE device_id = ? AND UPPER(serial) = ?", (device_id, serial_clean))
    exists = c.fetchone()
    c.execute(
        "INSERT OR REPLACE INTO registrations (device_id, push_token, serial, push_service_url) VALUES (?, ?, ?, ?)",
        (device_id, push_token, serial_clean, push_service_url)
    )
    if is_android_wallet_app:
        c.execute(
            "UPDATE clients SET push_token = ?, platform = 'android', android_active = 1, last_sync_at = ? WHERE UPPER(serial) = ?",
            (push_token, get_short_time(), serial_clean)
        )
    else:
        c.execute(
            "UPDATE clients SET push_token = ?, platform = 'ios', android_active = 0, last_sync_at = ? WHERE UPPER(serial) = ?",
            (push_token, get_short_time(), serial_clean)
        )
    c.execute(
        "INSERT INTO wallet_logs (created_at, log_text) VALUES (?, ?)",
        (get_utc_timestamp(), f"📲 Registro PassKit ({'Android' if is_android_wallet_app else 'iPhone'}): serial={serial_clean} device={device_id[:10]}")
    )
    conn.commit()
    conn.close()

    print(f"📲 Wallet registrado ({'Android' if is_android_wallet_app else 'iPhone'}): device={device_id} serial={serial_clean}")
    return "", (200 if exists else 201)


@app.route("/v1/devices/<device_id>/registrations/<pass_type_id>/<serial_number>", methods=["DELETE"])
def unregister_device(device_id, pass_type_id, serial_number):
    if not is_authorized():
        return "", 401
    serial_clean = (serial_number or "").strip().upper()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM registrations WHERE device_id = ? AND UPPER(serial) = ?", (device_id, serial_clean))
    conn.execute(
        "UPDATE clients SET push_token = NULL WHERE UPPER(serial) = ? AND UPPER(serial) NOT IN (SELECT UPPER(serial) FROM registrations)",
        (serial_clean,)
    )
    conn.commit()
    conn.close()
    return "", 200


@app.route("/v1/devices/<device_id>/registrations/<pass_type_id>", methods=["GET"])
def get_updated_passes(device_id, pass_type_id):
    since = request.args.get("passesUpdatedSince")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    query = """SELECT c.serial, c.updated_at FROM clients c
               JOIN registrations r ON UPPER(r.serial) = UPPER(c.serial)
               WHERE r.device_id = ?"""
    params = [device_id]
    if since:
        query += " AND c.updated_at > ?"
        params.append(since)
    c.execute(query, params)
    rows = list(c.fetchall())

    g_query = """SELECT g.code, g.updated_at FROM gift_cards g
                 JOIN registrations r ON UPPER(r.serial) = UPPER(g.code)
                 WHERE r.device_id = ?"""
    g_params = [device_id]
    if since:
        g_query += " AND g.updated_at > ?"
        g_params.append(since)
    c.execute(g_query, g_params)
    rows.extend(c.fetchall())
    conn.close()

    if not rows:
        return "", 204
    return jsonify({
        "serialNumbers": [r[0] for r in rows],
        "lastUpdated": max((r[1] for r in rows if r[1]), default=get_utc_timestamp())
    })


@app.route("/v1/passes/<pass_type_id>/<serial_number>", methods=["GET"])
def get_latest_pass(pass_type_id, serial_number):
    if not is_authorized():
        return "", 401
    serial_clean = (serial_number or "").strip().upper()

    if serial_clean.startswith("GIFT-"):
        gift = get_gift_card_by_code(serial_clean)
        if not gift:
            return "", 404
        updated_at_str = gift.get("updated_at") or ""
        try:
            dt_upd = datetime.strptime(updated_at_str, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
        except Exception:
            try:
                dt_upd = datetime.strptime(updated_at_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except Exception:
                dt_upd = datetime.now(timezone.utc)
        upd_epoch_sec = int(dt_upd.timestamp())
        auth_hdr = request.headers.get("Authorization", "")
        ua_hdr = (request.headers.get("User-Agent") or "").lower()
        is_android_wallet = auth_hdr.startswith("AndroidPass") or auth_hdr.startswith("AttidoPass") or "android" in ua_hdr or "walletpasses" in ua_hdr
        pkpass_path = generate_signed_gift_pkpass(gift["code"], request.host_url, for_android=is_android_wallet)
        response = send_file(pkpass_path, mimetype="application/vnd.apple.pkpass", conditional=False, max_age=0)
        response.headers["Last-Modified"] = formatdate(timeval=float(upd_epoch_sec), localtime=False, usegmt=True)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT serial, name, stamps, message, phone, birthdate, total_visits, redeemed_count, updated_at, last_action, COALESCE(status, 'active') FROM clients WHERE UPPER(serial) = ?",
        (serial_clean,)
    )
    client = c.fetchone()
    if not client:
        conn.close()
        return "", 404

    serial_id, name, stamps, message, phone, birthdate, total_visits, redeemed_count, updated_at_str, last_action, status_val = client

    try:
        dt_upd = datetime.strptime(updated_at_str, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
    except Exception:
        try:
            dt_upd = datetime.strptime(updated_at_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            dt_upd = datetime.now(timezone.utc)

    upd_epoch_sec = int(dt_upd.timestamp())
    ims_header = request.headers.get("If-Modified-Since")
    if ims_header:
        try:
            ims_dt = parsedate_to_datetime(ims_header)
            if int(ims_dt.timestamp()) > upd_epoch_sec:
                conn.close()
                return "", 304
        except Exception:
            pass

    auth_hdr = request.headers.get("Authorization", "")
    ua_hdr = (request.headers.get("User-Agent") or "").lower()
    is_android_wallet = auth_hdr.startswith("AndroidPass") or auth_hdr.startswith("AttidoPass") or "android" in ua_hdr or "walletpasses" in ua_hdr

    if is_android_wallet:
        c.execute("UPDATE clients SET platform = 'android', android_active = 1, last_sync_at = ? WHERE serial = ?", (get_short_time(), serial_id))
    else:
        c.execute("UPDATE clients SET platform = 'ios', android_active = 0, last_sync_at = ? WHERE serial = ?", (get_short_time(), serial_id))
    c.execute(
        "INSERT INTO wallet_logs (created_at, log_text) VALUES (?, ?)",
        (get_utc_timestamp(), f"🔄 Pase descargado/actualizado ({'Android' if is_android_wallet else 'iPhone'}): {serial_id} ({stamps}/10)")
    )
    conn.commit()
    conn.close()

    pkpass_path = generate_signed_pkpass(
        serial_id, name, stamps or 0, request.host_url, message,
        phone=phone, birthdate=birthdate, total_visits=total_visits, redeemed_count=redeemed_count or 0,
        last_action=last_action or "add",
        for_android=is_android_wallet,
        is_voided=(status_val != "active")
    )
    response = send_file(pkpass_path, mimetype="application/vnd.apple.pkpass", conditional=False, max_age=0)
    response.headers["Last-Modified"] = formatdate(timeval=float(upd_epoch_sec), localtime=False, usegmt=True)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@app.route("/v1/log", methods=["POST"])
def wallet_log():
    logs = (request.get_json(force=True, silent=True) or {}).get("logs", [])
    if logs:
        conn = sqlite3.connect(DB_PATH)
        for line in logs:
            print(f"📝 Wallet log: {line}")
            conn.execute("INSERT INTO wallet_logs (created_at, log_text) VALUES (?, ?)", (get_utc_timestamp(), str(line)))
        conn.commit()
        conn.close()
    return "", 200


def _get_vapid_signer():
    """Devuelve un objeto Vapid válido. pywebpush interpreta un str como ruta/base64, NO como PEM,
    por eso hay que pasar una instancia de Vapid cargada desde el PEM."""
    return Vapid02.from_pem(VAPID_PRIVATE_PEM.encode("utf-8"))


def send_android_webpush(sub_dict, serial, body_text, stamps=None):
    """Envía notificación Web Push RFC 8291 (VAPID) a un teléfono Android con imagen en vivo del pase."""
    try:
        ts_val = int(datetime.now(timezone.utc).timestamp())
        title_str = f"Demo Café ☕️ ({stamps}/10 sellos)" if stamps is not None else "Demo Café ☕️"
        payload = json.dumps({
            "title": title_str,
            "body": body_text,
            "url": f"/card/{serial}?from_push=1&t={ts_val}",
            "pkpass_url": f"/card/{serial}/download.pkpass?v={ts_val}",
            "image": f"/card/{serial}/strip.png?v={ts_val}",
            "tag": f"maishas-{serial}-{ts_val}",
            "serial": serial
        })
        webpush(
            subscription_info=sub_dict,
            data=payload,
            vapid_private_key=_get_vapid_signer(),
            vapid_claims={"sub": "mailto:contacto@democafe.com"},
            ttl=86400,
            headers={"Urgency": "high"},
            timeout=10
        )
        return True, 200, "OK"
    except WebPushException as ex:
        code = ex.response.status_code if ex.response is not None else 500
        detail = f"{ex}"
        if ex.response is not None:
            try:
                detail += f" | {ex.response.text[:200]}"
            except Exception:
                pass
        _log_wallet_event(f"⚠️ Web Push Android falló ({serial}): HTTP {code} {detail[:300]}")
        return False, code, detail
    except Exception as e:
        _log_wallet_event(f"⚠️ Web Push Android error interno ({serial}): {str(e)[:300]}")
        return False, 500, str(e)


def _log_wallet_event(text):
    """Guarda un evento de diagnóstico en wallet_logs (visible en /api/diagnostics)."""
    try:
        print(text)
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT INTO wallet_logs (created_at, log_text) VALUES (?, ?)", (get_utc_timestamp(), text))
        conn.commit()
        conn.close()
    except Exception:
        pass


def send_apns_push_for_serial(serial, notification_body="Tu tarjeta Demo Café se ha actualizado ☕️"):
    """Despacha notificaciones Push a iPhone y Android y devuelve un estado claro en hora local de Navojoa, Sonora."""
    serial_clean = (serial or "").strip().upper()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    reg_rows = c.execute(
        "SELECT push_token, COALESCE(push_service_url, '') FROM registrations WHERE UPPER(serial) = ? AND push_token IS NOT NULL",
        (serial_clean,)
    ).fetchall()
    client_row = c.execute(
        "SELECT push_token, android_active, stamps FROM clients WHERE UPPER(serial) = ?",
        (serial_clean,)
    ).fetchone()

    token_map = {}
    for tok, srv_url in reg_rows:
        if tok:
            token_map[tok] = srv_url
    if client_row and client_row[0] and client_row[0] not in token_map:
        token_map[client_row[0]] = ""

    android_active = bool(client_row[1]) if client_row else False
    current_stamps = (client_row[2] or 0) if client_row else 0

    android_subs = c.execute(
        "SELECT endpoint, sub_json FROM android_subscriptions WHERE UPPER(serial) = ?",
        (serial_clean,)
    ).fetchall()

    any_success = False
    any_error = False

    # 1. Enviar a iPhones registrados (Apple APNs HTTP/2) o Apps Android .pkpass (WalletPasses / Attido)
    for token, srv_url in token_map.items():
        ok, status_code, detail = send_apns_push(token, push_service_url=srv_url)
        if ok:
            any_success = True
        else:
            any_error = True
            if status_code == 410 or "Unregistered" in detail or "BadDeviceToken" in detail:
                c.execute("DELETE FROM registrations WHERE push_token = ? AND UPPER(serial) = ?", (token, serial_clean))

    # 2. Enviar a suscripciones Web Push de Android (Chrome / PWA) sólo si Google Wallet nativo NO está activo,
    #    para que las notificaciones en Android salgan con el ícono nativo de Google Wallet y no con el de Chrome.
    conn.commit()  # liberar el lock de SQLite
    android_ok = 0
    android_fail = 0
    stale_endpoints = []
    if not google_wallet_enabled():
        for endpoint, sub_json_str in android_subs:
            try:
                sub_dict = json.loads(sub_json_str)
                ok_and, code_and, _ = send_android_webpush(sub_dict, serial_clean, notification_body, stamps=current_stamps)
                if ok_and:
                    any_success = True
                    android_ok += 1
                else:
                    any_error = True
                    android_fail += 1
                    if code_and in (404, 410):
                        stale_endpoints.append(endpoint)
            except Exception:
                any_error = True
                android_fail += 1
        for endpoint in stale_endpoints:
            c.execute("DELETE FROM android_subscriptions WHERE endpoint = ?", (endpoint,))

    # 3. Google Wallet (Android): activar actualización y aviso nativo en segundo plano. No afecta a Apple.
    gw_should_push = google_wallet_enabled() and (android_active or android_subs or not token_map)
    if gw_should_push and (android_active or android_subs):
        any_success = True

    sonora_now = get_short_time()
    if any_success:
        status_msg = f"✅ Notificación enviada ({sonora_now})"
    elif android_fail and not token_map:
        status_msg = f"⚠️ Android no recibió el Push ({sonora_now}) · Pídele abrir su tarjeta y tocar «Activar Notificaciones»"
    elif android_active and not android_subs:
        status_msg = f"🔄 Tarjeta web al día ({sonora_now}) · Falta activar Push en su Android"
    elif any_error:
        status_msg = f"⚠️ No se pudo notificar ({sonora_now})"
    else:
        status_msg = "⏳ Aún no instala su tarjeta"

    c.execute("UPDATE clients SET last_push_status = ? WHERE UPPER(serial) = ?", (status_msg, serial_clean))
    conn.commit()
    conn.close()

    if gw_should_push:
        try:
            google_wallet_push_update(serial_clean, _public_base_url(), message=notification_body, notify=True)
        except Exception as gw_err:
            _log_wallet_event(f"⚠️ Google Wallet: error al actualizar {serial_clean}: {str(gw_err)[:200]}")

    return status_msg


def send_apns_push(push_token, push_service_url=None):
    try:
        # Detectar si el token pertenece a una app Android de .pkpass (ej. WalletPasses FCM token) vs iOS APNs hex token
        is_ios_hex = len(push_token) in (64, 128) and all(c in "0123456789abcdefABCDEF" for c in push_token)
        if not is_ios_hex:
            target_url = push_service_url if (push_service_url and push_service_url.startswith("http")) else "https://walletpasses.appspot.com/api/v1/push"
            with httpx.Client(timeout=8.0) as client:
                res = client.post(
                    target_url,
                    json={"passTypeIdentifier": PASS_TYPE_ID, "pushTokens": [push_token]}
                )
                if res.status_code in (200, 201, 202, 204):
                    return True, 200, "OK"
                return False, res.status_code, res.text

        url = f"https://api.push.apple.com/3/device/{push_token}"
        context = ssl.create_default_context()
        context.load_cert_chain(certfile=CERT_PEM, keyfile=KEY_PEM)
        with httpx.Client(http2=True, verify=context, timeout=10.0) as client:
            headers = {
                "apns-topic": PASS_TYPE_ID,
                "apns-priority": "10"
            }
            res = client.post(url, json={}, headers=headers)
            print(f"📡 Push APNs enviado a {push_token[:12]}... Status: {res.status_code} {res.text}")
            if res.status_code == 200:
                return True, 200, "OK"
            return False, res.status_code, res.text
    except Exception as e:
        print(f"⚠️ Error enviando Push APNs: {e}")
        return False, 500, str(e)


# ---------------------------------------------------------
# GOOGLE WALLET API (ANDROID) — pase nativo que se actualiza solo, equivalente a Apple Wallet
# Se activa únicamente si existen GOOGLE_WALLET_ISSUER_ID y GOOGLE_WALLET_SERVICE_ACCOUNT_JSON.
# ---------------------------------------------------------
GOOGLE_WALLET_ISSUER_ID = (os.environ.get("GOOGLE_WALLET_ISSUER_ID") or "").strip()
GOOGLE_WALLET_CLASS_SUFFIX = (os.environ.get("GOOGLE_WALLET_CLASS_SUFFIX") or "maishas_demo_replica_v1").strip()
GW_API_BASE = "https://walletobjects.googleapis.com/walletobjects/v1"
GW_SCOPE = "https://www.googleapis.com/auth/wallet_object.issuer"
_RAILWAY_DOMAIN = (os.environ.get("RAILWAY_PUBLIC_DOMAIN") or "").strip()
PUBLIC_BASE_URL = (
    os.environ.get("PUBLIC_BASE_URL")
    or (f"https://{_RAILWAY_DOMAIN}" if _RAILWAY_DOMAIN else "https://web-production-f7cd4.up.railway.app")
).rstrip("/")
_gw_token_cache = {"token": None, "exp": 0}
_gw_class_ready = {"ok": False}


def _load_google_sa():
    """Lee la cuenta de servicio de Google (JSON directo o en base64) desde la variable de entorno."""
    raw = (os.environ.get("GOOGLE_WALLET_SERVICE_ACCOUNT_JSON") or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        try:
            data = json.loads(base64.b64decode(raw).decode("utf-8"))
        except Exception:
            return None
    if not (data.get("client_email") and data.get("private_key")):
        return None
    data["private_key"] = data["private_key"].replace("\\n", "\n")
    return data


def google_wallet_enabled():
    return bool(GOOGLE_WALLET_ISSUER_ID and _load_google_sa())


def _public_base_url():
    explicit_env = (os.environ.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if explicit_env:
        return explicit_env.replace("http://", "https://", 1)
    try:
        if request:
            host = request.host_url.rstrip("/")
            if "localhost" not in host and "127.0.0.1" not in host:
                return host.replace("http://", "https://", 1)
    except Exception:
        pass
    return PUBLIC_BASE_URL


def _gw_b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _gw_sign_jwt(claims, sa):
    """JWT RS256 firmado con la llave privada de la cuenta de servicio."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    header = {"alg": "RS256", "typ": "JWT"}
    signing_input = (
        _gw_b64url(json.dumps(header, separators=(",", ":")).encode())
        + "." + _gw_b64url(json.dumps(claims, separators=(",", ":")).encode())
    )
    key = serialization.load_pem_private_key(sa["private_key"].encode("utf-8"), password=None)
    sig = key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return signing_input + "." + _gw_b64url(sig)


def _gw_http(method, url, headers=None, json_body=None, data=None):
    try:
        with httpx.Client(timeout=10.0) as client:
            res = client.request(method, url, headers=headers, json=json_body, data=data)
        try:
            body = res.json()
        except Exception:
            body = {"raw": res.text[:300]}
        return res.status_code, body
    except Exception as e:
        return 0, {"error": str(e)[:200]}


def _gw_access_token():
    now = int(datetime.now(timezone.utc).timestamp())
    if _gw_token_cache["token"] and _gw_token_cache["exp"] - 60 > now:
        return _gw_token_cache["token"]
    sa = _load_google_sa()
    if not sa:
        return None
    assertion = _gw_sign_jwt({
        "iss": sa["client_email"], "scope": GW_SCOPE,
        "aud": sa.get("token_uri") or "https://oauth2.googleapis.com/token",
        "iat": now, "exp": now + 3600
    }, sa)
    status, body = _gw_http(
        "POST", sa.get("token_uri") or "https://oauth2.googleapis.com/token",
        data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}
    )
    if status != 200 or "access_token" not in body:
        _log_wallet_event(f"⚠️ Google Wallet: no se pudo obtener token ({status}): {str(body)[:200]}")
        return None
    _gw_token_cache["token"] = body["access_token"]
    _gw_token_cache["exp"] = now + int(body.get("expires_in", 3600))
    return _gw_token_cache["token"]


def _gw_api(method, path, json_body=None):
    token = _gw_access_token()
    if not token:
        return 0, {"error": "sin token de Google (revisa credenciales)"}
    return _gw_http(method, f"{GW_API_BASE}/{path}", headers={"Authorization": f"Bearer {token}"}, json_body=json_body)


def _gw_class_id():
    return f"{GOOGLE_WALLET_ISSUER_ID}.{GOOGLE_WALLET_CLASS_SUFFIX}"


def _gw_object_id(serial):
    return f"{GOOGLE_WALLET_ISSUER_ID}.{(serial or '').strip().upper()}"


def _gw_text(value):
    return {"defaultValue": {"language": "es-MX", "value": value}}


def build_google_loyalty_class(base_url):
    cfg = get_campaign_config()
    shop_name = cfg.get("shopName") or "Demo Café"
    return {
        "id": _gw_class_id(),
        "issuerName": shop_name,
        "programName": f"{shop_name} · Sellos",
        "programLogo": {
            "sourceUri": {"uri": f"{base_url}/api/campaign/logo.png?v={cfg.get('logoVersion', '1')}"},
            "contentDescription": _gw_text(shop_name)
        },
        "hexBackgroundColor": cfg.get("backgroundColor") or "#12100E",
        "countryCode": "MX",
        "reviewStatus": "UNDER_REVIEW",
        "multipleDevicesAndHoldersAllowedStatus": "ONE_USER_ALL_DEVICES",
        "homepageUri": {"uri": base_url, "description": shop_name},
        "locations": [{"latitude": cfg.get("lat", DEFAULT_LAT), "longitude": cfg.get("lng", DEFAULT_LNG)}],
    }


def build_google_loyalty_object(serial, base_url):
    """Construye el pase de lealtad o Gift Card de Google con los datos actuales (sellos o saldo, aviso, strip y expiración)."""
    serial_clean = (serial or "").strip().upper()
    cfg = get_campaign_config()
    shop_name = cfg.get("shopName") or "Demo Café"

    if serial_clean.startswith("GIFT-"):
        gift = get_gift_card_by_code(serial_clean)
        if not gift:
            return None
        is_exhausted = (
            gift["status"] in ("redeemed", "cancelled")
            or (gift["gift_type"] == "balance" and float(gift["current_balance"] or 0) <= 0)
            or (gift["gift_type"] == "combo" and int(gift["remaining_uses"] or 0) <= 0)
        )
        version = urllib_quote(f"{gift['updated_at']}_{cfg.get('logoVersion', '1')}")
        return {
            "id": _gw_object_id(gift["code"]),
            "classId": _gw_class_id(),
            "state": "EXPIRED" if is_exhausted else "ACTIVE",
            "accountId": gift["code"],
            "accountName": f"GIFT CARD - {shop_name.upper()} · Para: {gift['recipient_name']}",
            "loyaltyPoints": {
                "label": "VALE DE REGALO" if gift["gift_type"] == "combo" else "SALDO DISPONIBLE",
                "balance": {"string": gift["value_headline"]}
            },
            "secondaryLoyaltyPoints": {
                "label": "DE PARTE DE",
                "balance": {"string": gift["sender_name"][:40]}
            },
            "barcode": {"type": "QR_CODE", "value": gift["code"], "alternateText": gift["code"]},
            "heroImage": {
                "sourceUri": {"uri": f"{base_url}/gift/{gift['code']}/strip.png?v={version}"},
                "contentDescription": _gw_text(f"GIFT CARD - {shop_name}")
            },
            "textModulesData": [
                {"id": "title", "header": "TARJETA DE REGALO", "body": f"GIFT CARD - {shop_name}"},
                {"id": "dedication", "header": f"DEDICATORIA DE {gift['sender_name'].upper()}", "body": gift["dedication"]},
                {"id": "benefit", "header": "DETALLE DEL REGALO", "body": f"{gift['package_title']} — {gift['package_desc']}"},
                {"id": "last_action", "header": "ÚLTIMO MOVIMIENTO", "body": gift["last_action_detail"] or "Gift Card activa"},
            ],
            "linksModuleData": {
                "uris": [{"id": "unbox", "uri": f"{base_url}/gift/{gift['code']}", "description": "Abrir Gift Card Digital"}]
            },
            "locations": [{"latitude": cfg["lat"], "longitude": cfg["lng"]}],
        }

    conn = sqlite3.connect(DB_PATH, timeout=20.0)
    row = conn.execute(
        "SELECT serial, name, stamps, message, total_visits, updated_at, COALESCE(status, 'active') FROM clients WHERE UPPER(serial) = ?",
        (serial_clean,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    goal = cfg.get("stampGoal") or 10
    reward_text = cfg.get("rewardText") or "¡1 Café Gratis!"
    serial_id, name, stamps, message, visits, updated_at, status_val = row
    stamps = max(0, min(goal, stamps or 0))
    tier = get_vip_tier(visits if visits is not None else stamps)
    reward = f"¡{reward_text.upper()} DISPONIBLE! 🎉" if stamps >= goal else f"Faltan {goal - stamps} sellos para: {reward_text}"
    clean_msg = (message or DEFAULT_MESSAGE).replace("\u200b", "")
    promo_balance = (message or DEFAULT_MESSAGE)[:90]
    version = urllib_quote(f"{updated_at}_{cfg.get('logoVersion', '1')}_{cfg.get('stripVersion', '1')}")
    uris = [
        {"id": "live", "uri": f"{base_url}/card/{serial_id}", "description": "Mi tarjeta en vivo"}
    ]
    if cfg.get("menuUrl"):
        uris.append({"id": "menu", "uri": cfg["menuUrl"], "description": "Menú Digital / WhatsApp"})
    return {
        "id": _gw_object_id(serial_id),
        "classId": _gw_class_id(),
        "state": "ACTIVE" if status_val == "active" else "INACTIVE",
        "accountId": serial_id,
        "accountName": name or "Cliente",
        "loyaltyPoints": {"label": "SELLOS", "balance": {"string": f"{stamps} / {goal}"}},
        "secondaryLoyaltyPoints": {"label": f"AVISO {shop_name.upper()[:14]}", "balance": {"string": promo_balance}},
        "barcode": {"type": "QR_CODE", "value": serial_id, "alternateText": serial_id},
        "heroImage": {
            "sourceUri": {"uri": f"{base_url}/card/{serial_id}/strip.png?v={version}"},
            "contentDescription": _gw_text(f"{stamps} de {goal} sellos")
        },
        "textModulesData": [
            {"id": "tier", "header": "NIVEL VIP", "body": tier["name"]},
            {"id": "reward", "header": "PREMIO", "body": reward},
            {"id": "promo", "header": f"AVISO {shop_name.upper()}", "body": clean_msg},
            {"id": "terms", "header": "TÉRMINOS Y CONDICIONES", "body": cfg["termsText"]},
        ],
        "linksModuleData": {"uris": uris},
        "locations": [{"latitude": cfg["lat"], "longitude": cfg["lng"]}],
    }


def urllib_quote(text):
    from urllib.parse import quote
    return quote(text, safe="")


def ensure_google_class(base_url, force_update=False):
    """Crea o actualiza la clase de lealtad (una sola para todos los clientes)."""
    body = build_google_loyalty_class(base_url)
    status, resp = _gw_api("GET", f"loyaltyClass/{_gw_class_id()}")
    if status == 404:
        status, resp = _gw_api("POST", "loyaltyClass", body)
    elif status == 200 and force_update:
        patch = {k: v for k, v in body.items() if k != "id"}
        status, resp = _gw_api("PATCH", f"loyaltyClass/{_gw_class_id()}", patch)
    ok = status in (200, 201)
    _gw_class_ready["ok"] = ok
    if not ok:
        _log_wallet_event(f"⚠️ Google Wallet: error con la clase ({status}): {str(resp)[:250]}")
    return ok, status, resp


def ensure_google_object(serial, base_url, notify_on_update=False):
    """Crea el pase del cliente en Google (si no existe) o lo actualiza con sus datos actuales."""
    obj = build_google_loyalty_object(serial, base_url)
    if not obj:
        return False, 404, {"error": "cliente no encontrado"}
    if not _gw_class_ready["ok"]:
        ok, st, resp = ensure_google_class(base_url)
        if not ok:
            return False, st, resp
    status, resp = _gw_api("GET", f"loyaltyObject/{obj['id']}")
    if status == 404:
        status, resp = _gw_api("POST", "loyaltyObject", obj)
    elif status == 200:
        patch = {k: v for k, v in obj.items() if k not in ("id", "classId")}
        if notify_on_update:
            patch["notifyPreference"] = "NOTIFY_ON_UPDATE"
        status, resp = _gw_api("PATCH", f"loyaltyObject/{obj['id']}", patch)
    ok = status in (200, 201)
    if not ok:
        _log_wallet_event(f"⚠️ Google Wallet: error con el pase de {serial} ({status}): {str(resp)[:250]}")
    return ok, status, resp


def google_wallet_save_url(serial, base_url):
    """Link 'Agregar a Google Wallet' firmado en 0ms sin bloquear la petición HTTP."""
    sa = _load_google_sa()
    if not sa:
        return None, "Cuenta de servicio no disponible"
    obj = build_google_loyalty_object(serial, base_url)
    if not obj:
        return None, "Cliente no encontrado"
    import threading
    threading.Thread(target=ensure_google_object, args=(serial, base_url), daemon=True).start()
    now = int(datetime.now(timezone.utc).timestamp())
    claims = {
        "iss": sa["client_email"], "aud": "google", "typ": "savetowallet", "iat": now,
        "origins": [base_url],
        "payload": {"loyaltyObjects": [obj]},
    }
    return f"https://pay.google.com/gp/v/save/{_gw_sign_jwt(claims, sa)}", None


def _google_wallet_push_worker(serial_clean, base_url, message=None, notify=True):
    """Hilo en segundo plano que actualiza el pase en Google Wallet y envía la notificación nativa de Wallet."""
    import time
    time.sleep(0.15)
    try:
        obj_id = _gw_object_id(serial_clean)
        ok, st, _ = ensure_google_object(serial_clean, base_url, notify_on_update=True)
        if message and notify:
            msg_id = f"msg_{int(datetime.now(timezone.utc).timestamp() * 1000)}"
            st2, resp2 = _gw_api("POST", f"loyaltyObject/{obj_id}/addMessage", {
                "message": {
                    "id": msg_id,
                    "header": "Demo Café ☕️",
                    "body": message[:180],
                    "messageType": "TEXT_AND_NOTIFY",
                }
            })
            if st2 in (200, 201):
                _log_wallet_event(f"🟢 Google Wallet: pase y notificación enviados a {serial_clean}")
            else:
                _log_wallet_event(f"⚠️ Google Wallet: aviso no enviado a {serial_clean} ({st2}): {str(resp2)[:200]}")
        elif ok:
            _log_wallet_event(f"🟢 Google Wallet: pase sincronizado para {serial_clean}")
    except Exception as e:
        _log_wallet_event(f"⚠️ Google Wallet: error en worker para {serial_clean}: {str(e)[:200]}")


def google_wallet_push_update(serial, base_url, message=None, notify=True):
    """Lanza la actualización y alerta nativa de Google Wallet en segundo plano sin bloquear el servidor."""
    if not google_wallet_enabled():
        return False
    serial_clean = (serial or "").strip().upper()
    import threading
    threading.Thread(
        target=_google_wallet_push_worker,
        args=(serial_clean, base_url, message, notify),
        daemon=True
    ).start()
    return True


@app.route("/card/<serial_number>/google-wallet")
def google_wallet_save(serial_number):
    """Redirige al botón oficial 'Agregar a Google Wallet' del cliente."""
    if not google_wallet_enabled():
        return "Google Wallet aún no está configurado en el servidor.", 503
    serial_clean = (serial_number or "").strip().upper()
    url, err = google_wallet_save_url(serial_clean, _public_base_url())
    if not url:
        return f"No se pudo generar el pase de Google Wallet. {err}", 502
    _log_wallet_event(f"🟢 Google Wallet: enlace de guardado generado para {serial_clean}")
    return redirect(url)


@app.route("/api/google-wallet/setup")
@requires_auth
def google_wallet_setup_check():
    """Verifica las credenciales de Google Wallet y crea/actualiza la clase de lealtad."""
    sa = _load_google_sa()
    info = {
        "issuer_id_configurado": bool(GOOGLE_WALLET_ISSUER_ID),
        "cuenta_servicio_valida": bool(sa),
        "cuenta_servicio_email": sa["client_email"] if sa else None,
        "habilitado": google_wallet_enabled(),
    }
    if info["habilitado"]:
        ok, status, resp = ensure_google_class(_public_base_url(), force_update=True)
        info["clase_ok"] = ok
        info["clase_http"] = status
        info["clase_detalle"] = resp if not ok else {"id": _gw_class_id()}
    return jsonify(info)


@app.route("/api/test-android-push/<serial_number>")
@requires_auth
def test_android_push(serial_number):
    """Envía un Web Push de prueba y devuelve el resultado real por cada suscripción (para diagnóstico)."""
    serial_clean = (serial_number or "").strip().upper()
    conn = sqlite3.connect(DB_PATH)
    subs = conn.execute(
        "SELECT endpoint, sub_json, created_at FROM android_subscriptions WHERE UPPER(serial) = ?",
        (serial_clean,)
    ).fetchall()
    row = conn.execute("SELECT stamps FROM clients WHERE UPPER(serial) = ?", (serial_clean,)).fetchone()
    all_subs = conn.execute("SELECT serial, endpoint, created_at FROM android_subscriptions").fetchall()
    conn.close()
    results = []
    for endpoint, sub_json_str, created_at in subs:
        ok, code, detail = send_android_webpush(json.loads(sub_json_str), serial_clean, "🔔 Prueba de notificación Demo Café", stamps=(row[0] if row else 0))
        results.append({"endpoint_host": endpoint.split("/")[2], "created_at": created_at, "ok": ok, "http_status": code, "detail": detail[:300]})
    return jsonify({
        "serial": serial_clean,
        "client_exists": bool(row),
        "subscriptions_for_serial": len(subs),
        "results": results,
        "all_subscriptions_in_db": [{"serial": s, "host": e.split("/")[2], "created_at": c} for s, e, c in all_subs],
    })


@app.route("/api/diagnostics")
@requires_auth
def diagnostics():
    report = {
        "certificates": {
            "cert_pem_exists": os.path.isfile(CERT_PEM),
            "key_pem_exists": os.path.isfile(KEY_PEM),
            "wwdr_pem_exists": os.path.isfile(WWDR_PEM),
        },
        "http2_support": False,
        "dynamic_strip_pillow": False,
        "android_wallet_support": {
            "vapid_configured": bool(VAPID_PUBLIC_KEY),
            "qr_generator": True,
            "pwa_service_worker": True,
        },
        "apns_connection": None,
        "pass_signing": False,
        "geolocation": get_shop_location(),
        "database": {},
        "recent_wallet_logs": []
    }
    try:
        import h2  # noqa: F401
        report["http2_support"] = True
    except ImportError as e:
        report["http2_error"] = str(e)

    try:
        test_path = generate_signed_pkpass("MAISHAS-DIAG01", "Prueba Diag", 4, request.host_url, "Diagnóstico OK", total_visits=12)
        report["pass_signing"] = os.path.isfile(test_path) and os.path.getsize(test_path) > 1000
        report["dynamic_strip_pillow"] = True
    except Exception as e:
        report["pass_signing_error"] = str(e)

    try:
        ok, code, detail = send_apns_push("0000000000000000000000000000000000000000000000000000000000000000")
        report["apns_connection"] = {
            "reachable_and_authenticated": code in (200, 400),
            "status_code": code,
            "response": detail
        }
    except Exception as e:
        report["apns_connection"] = {"reachable_and_authenticated": False, "error": str(e)}

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    report["database"]["total_clients"] = c.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
    report["database"]["total_ios_registrations"] = c.execute("SELECT COUNT(*) FROM registrations").fetchone()[0]
    report["database"]["total_android_subscriptions"] = c.execute("SELECT COUNT(*) FROM android_subscriptions").fetchone()[0]
    report["recent_wallet_logs"] = [
        {"time": r[0], "log": r[1]}
        for r in c.execute("SELECT created_at, log_text FROM wallet_logs ORDER BY id DESC LIMIT 20").fetchall()
    ]
    conn.close()
    return jsonify(report)


_bday_worker_started = False
_bday_worker_lock = threading.Lock()


@app.before_request
def _ensure_birthday_auto_worker():
    global _bday_worker_started
    if _bday_worker_started:
        return
    with _bday_worker_lock:
        if _bday_worker_started:
            return
        _bday_worker_started = True
        def _worker():
            time.sleep(5)
            while True:
                try:
                    run_auto_birthday_dispatch(force_now=False)
                except Exception as e:
                    print(f"⚠️ Error en worker de cumpleaños automático: {e}")
                time.sleep(60)
        t = threading.Thread(target=_worker, daemon=True, name="bday-auto-worker")
        t.start()


if __name__ == "__main__":
    print("🚀 Servidor con soporte iOS Apple Wallet + Android Wallet Digital iniciado en http://localhost:5001")
    port = int(os.environ.get('PORT', 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
