from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    redirect,
    url_for,
    session,
    make_response,
    send_from_directory,
    flash,
    stream_with_context,
)

import codecs
import encodings.idna

from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from pathlib import Path
from datetime import datetime, timedelta, timezone

import hashlib
import json
import os
import re
import secrets
import threading
import unicodedata

from discord_bridge import DiscordBridge


# Certains environnements Python minimalistes ne chargent pas automatiquement
# le codec IDNA, pourtant utilisé par Werkzeug pour lire le nom de domaine.
# Son enregistrement explicite évite les erreurs 500 sur les URL Render.
def ensure_idna_codec():

    try:
        codecs.lookup("idna")
        return
    except LookupError:
        pass


    codecs.register(
        lambda name: (
            encodings.idna.getregentry()
            if name.replace("_", "-") == "idna"
            else None
        )
    )


ensure_idna_codec()


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

DEFAULT_DATA_DIR = BASE_DIR / "data"


def get_data_dir():

    configured_dir = Path(
        os.environ.get(
            "NATHGPT_DATA_DIR",
            str(DEFAULT_DATA_DIR)
        )
    )

    try:
        configured_dir.mkdir(
            parents=True,
            exist_ok=True
        )

        return configured_dir

    except PermissionError:
        # Render n'autorise /var/data que lorsqu'un disque persistant est
        # réellement monté. En plan Free, l'application reste fonctionnelle
        # avec un stockage temporaire dans le dossier du projet.
        DEFAULT_DATA_DIR.mkdir(
            parents=True,
            exist_ok=True
        )

        print(
            "Stockage persistant indisponible : "
            "utilisation du dossier data temporaire."
        )

        return DEFAULT_DATA_DIR


DATA_DIR = get_data_dir()

USERS_FILE = DATA_DIR / "users.json"

TOKENS_FILE = DATA_DIR / "tokens.json"

CONVERSATIONS_FILE = DATA_DIR / "conversations.json"

SECRET_FILE = DATA_DIR / "secret_key.txt"

discord_bridge = DiscordBridge(DATA_DIR)


# ============================================================
# RECONNEXION AUTOMATIQUE PAR IP
# ============================================================

# True :
# NathGPT peut reconnaître automatiquement un compte
# grâce à son IP si cette IP n'appartient qu'à un seul compte.
#
# False :
# uniquement le cookie de connexion automatique sera utilisé.

ALLOW_IP_AUTOLOGIN = os.environ.get(
    "ALLOW_IP_AUTOLOGIN",
    "false"
).lower() == "true"


# Le mot de passe du panneau staff doit rester dans les variables Render,
# jamais dans le code ni dans un fichier envoyÃ© sur GitHub.
STAFF_PASSWORD = os.environ.get("STAFF_PASSWORD", "")
STAFF_SESSION_SECONDS = 8 * 60 * 60
STAFF_ACTIVE_WINDOW_SECONDS = 10 * 60


# ============================================================
# COOKIE "SE SOUVENIR DE MOI"
# ============================================================

REMEMBER_COOKIE_NAME = "nathgpt_remember"

REMEMBER_DAYS = 90


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)

app.config["MAX_CONTENT_LENGTH"] = 36 * 1024 * 1024


@app.context_processor
def inject_asset_version():

    try:
        version = int(
            (BASE_DIR / "static" / "style.css").stat().st_mtime
        )
    except OSError:
        version = 1

    return {"asset_version": version}


# ============================================================
# VERROU POUR LES JSON
# ============================================================

_file_lock = threading.Lock()


# ============================================================
# DATE UTC
# ============================================================

def utc_now():

    return datetime.now(
        timezone.utc
    ).isoformat()


# ============================================================
# ECRITURE JSON ATOMIQUE
# ============================================================

def atomic_write_json(path: Path, data):

    tmp = path.with_suffix(
        path.suffix + ".tmp"
    )

    tmp.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    os.replace(
        tmp,
        path
    )


# ============================================================
# CHARGEMENT JSON
# ============================================================

def load_json(path: Path, default):

    with _file_lock:

        if not path.exists():

            atomic_write_json(
                path,
                default
            )

            return default

        try:

            return json.loads(
                path.read_text(
                    encoding="utf-8"
                )
            )

        except (
            json.JSONDecodeError,
            OSError
        ):

            return default


# ============================================================
# SAUVEGARDE JSON
# ============================================================

def save_json(path: Path, data):

    with _file_lock:

        atomic_write_json(
            path,
            data
        )


# ============================================================
# HISTORIQUE DES CONVERSATIONS
# ============================================================

def save_conversation_message(
    username,
    conversation_id,
    role,
    content,
    image_url=None,
    cricut_images=None,
):

    conversations = load_json(
        CONVERSATIONS_FILE,
        {}
    )

    user_conversations = conversations.setdefault(
        username,
        []
    )

    conversation = next(
        (
            item
            for item in user_conversations
            if item.get("id") == conversation_id
        ),
        None
    )

    now = utc_now()

    if not conversation:

        conversation = {
            "id": conversation_id,
            "title": "Nouvelle discussion",
            "created_at": now,
            "updated_at": now,
            "messages": [],
        }

        user_conversations.append(
            conversation
        )

    clean_content = str(content or "").strip()[:4000]

    if role == "user" and conversation["title"] == "Nouvelle discussion":
        conversation["title"] = clean_content[:80] or "Nouvelle discussion"

    message = {
        "role": role,
        "content": clean_content,
        "created_at": now,
    }

    if image_url:
        message["image_url"] = str(image_url)[:2000]

    if cricut_images is not None:
        message["cricut_images"] = [
            str(url)[:2000]
            for url in cricut_images
            if str(url).strip()
        ][:300]

    conversation["messages"].append(
        message
    )

    # Garde l'historique utile sans faire grossir le fichier indéfiniment.
    conversation["messages"] = conversation["messages"][-200:]
    conversation["updated_at"] = now

    save_json(
        CONVERSATIONS_FILE,
        conversations
    )


def get_conversation_summaries(username):

    conversations = load_json(
        CONVERSATIONS_FILE,
        {}
    )

    items = conversations.get(
        username,
        []
    )

    return [
        {
            "id": item.get("id"),
            "title": item.get("title", "Nouvelle discussion"),
            "updated_at": item.get("updated_at"),
        }
        for item in reversed(items)
    ]


def get_conversation(username, conversation_id):

    conversations = load_json(
        CONVERSATIONS_FILE,
        {}
    )

    for item in conversations.get(username, []):
        if item.get("id") == conversation_id:
            return item

    return None


def delete_conversation(username, conversation_id):
    conversations = load_json(CONVERSATIONS_FILE, {})
    owner_key = next(
        (key for key in conversations if key.casefold() == username.casefold()),
        None,
    )
    if owner_key:
        conversations[owner_key] = [
            item for item in conversations[owner_key]
            if item.get("id") != conversation_id
        ]
        if not conversations[owner_key]:
            conversations.pop(owner_key, None)
        save_json(CONVERSATIONS_FILE, conversations)


def is_image_generation_request(question, has_reference_images=False):
    """DÃ©termine cÃ´tÃ© serveur si le rÃ©sultat attendu est une image."""
    if has_reference_images:
        return True

    text = unicodedata.normalize("NFD", str(question or "").casefold())
    text = "".join(character for character in text if not unicodedata.combining(character))

    if text.startswith("modify:") or text == "png:":
        return True

    keywords = (
        "genere une image", "genere l'image", "genere image",
        "cree une image", "creer une image", "cree l'image", "creer l'image",
        "fais une image", "fait une image", "dessine", "genere une photo",
        "cree une photo", "creer une photo", "genere une illustration",
        "cree une illustration", "creer une illustration", "image de", "photo de",
        "illustration de", "generate an image", "create an image",
        "generate a picture", "create a picture", "draw me",
    )
    return any(keyword in text for keyword in keywords)


def get_reference_images():

    images = request.files.getlist("reference_images")

    if len(images) > 4:
        raise ValueError("Tu peux envoyer jusqu'à 4 images de référence.")

    result = []

    for image in images:
        if not image or not image.filename:
            continue

        filename = secure_filename(image.filename)
        extension = Path(filename).suffix.lower()

        if extension not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            raise ValueError("Les références doivent être des images PNG, JPG, WEBP ou GIF.")

        data = image.read(8 * 1024 * 1024 + 1)

        if len(data) > 8 * 1024 * 1024:
            raise ValueError("Chaque image de référence est limitée à 8 Mo.")

        result.append({
            "filename": filename,
            "data": data,
        })

    return result


def save_discord_result(username, conversation_id, event):

    if event.get("type") == "image":
        save_conversation_message(
            username,
            conversation_id,
            "assistant",
            "Image générée",
            event.get("url")
        )

    elif event.get("type") == "text":
        save_conversation_message(
            username,
            conversation_id,
            "assistant",
            event.get("message", "")
        )

    elif event.get("type") == "cricut_complete":
        images = event.get("images", [])
        save_conversation_message(
            username,
            conversation_id,
            "assistant",
            f"Stickers Cricut terminés · {len(images)} image(s) prête(s) à télécharger.",
            cricut_images=images,
        )


discord_bridge.set_result_handler(
    save_discord_result
)


# ============================================================
# CLE SECRETE FLASK
# ============================================================

def get_or_create_secret():

    configured_secret = os.environ.get(
        "NATHGPT_SECRET_KEY",
        ""
    ).strip()

    if configured_secret:
        return configured_secret

    if SECRET_FILE.exists():

        value = SECRET_FILE.read_text(
            encoding="utf-8"
        ).strip()

        if value:

            return value


    value = secrets.token_hex(32)


    SECRET_FILE.write_text(
        value,
        encoding="utf-8"
    )


    return value


app.secret_key = get_or_create_secret()


# ============================================================
# CONFIG SESSION
# ============================================================

app.config.update(

    SESSION_COOKIE_HTTPONLY=True,

    SESSION_COOKIE_SAMESITE="Lax",

    # Mets True plus tard si ton site utilise HTTPS
    SESSION_COOKIE_SECURE=(
        os.environ.get(
            "SESSION_COOKIE_SECURE",
            "false"
        ).lower() == "true"
    ),

)


_runtime_services_lock = threading.Lock()
_runtime_services_started = False


def start_runtime_services():
    """Start Discord only after Gunicorn has exposed the HTTP application."""
    global _runtime_services_started

    if _runtime_services_started:
        return

    with _runtime_services_lock:
        if _runtime_services_started:
            return

        _runtime_services_started = True
        if os.environ.get(
            "DISCORD_AUTOSTART",
            "true"
        ).lower() == "true":
            discord_bridge.start()


# Gunicorn importe ce module au démarrage : le bot se lance également
# en production, avec un seul worker configuré dans render.yaml.
@app.before_request
def boot_runtime_services_after_http_startup():
    """The first Render health check starts the bot without blocking Flask."""
    start_runtime_services()


# ============================================================
# RECUPERER IP UTILISATEUR
# ============================================================

def client_ip():

    forwarded = request.headers.get(
        "X-Forwarded-For",
        ""
    )


    if forwarded:

        return forwarded.split(",")[0].strip()


    return request.remote_addr or "unknown"


# ============================================================
# USERNAME
# ============================================================

def normalize_username(username: str):

    return username.strip()


def valid_username(username: str):

    return (
        re.fullmatch(
            r"[A-Za-z0-9_.-]{3,24}",
            username
        )
        is not None
    )


# ============================================================
# TOKEN
# ============================================================

def hash_token(raw_token: str):

    return hashlib.sha256(
        raw_token.encode("utf-8")
    ).hexdigest()


# ============================================================
# TROUVER USER SANS TENIR COMPTE DES MAJUSCULES
# ============================================================

def find_user_key(users, username):

    wanted = username.casefold()


    for key in users:

        if key.casefold() == wanted:

            return key


    return None


# ============================================================
# ENREGISTRER IP DU COMPTE
# ============================================================

def add_ip_to_user(
    users,
    username,
    ip
):

    user = users[username]


    known_ips = user.setdefault(
        "known_ips",
        []
    )


    if (
        ip
        and ip != "unknown"
        and ip not in known_ips
    ):

        known_ips.append(ip)


    user["last_ip"] = ip

    user["last_login_at"] = utc_now()


# ============================================================
# ACTIVITE DES COMPTES (sans identifier le materiel)
# ============================================================

def touch_user_activity(username):
    """Met Ã  jour l'activitÃ© rÃ©cente, au plus une fois par minute."""
    now_timestamp = int(datetime.now(timezone.utc).timestamp())
    last_write = int(session.get("_activity_written_at", 0) or 0)

    if now_timestamp - last_write < 60:
        return

    users = load_json(USERS_FILE, {})
    user_key = find_user_key(users, username)

    if not user_key:
        return

    users[user_key]["last_seen_at"] = utc_now()
    save_json(USERS_FILE, users)
    session["_activity_written_at"] = now_timestamp


def staff_account_summaries():
    """PrÃ©pare des donnÃ©es minimales pour le panneau staff."""
    users = load_json(USERS_FILE, {})
    now = datetime.now(timezone.utc)
    accounts = []

    for username, details in users.items():
        last_seen = details.get("last_seen_at") or details.get("last_login_at")
        active = False

        if last_seen:
            try:
                seen_at = datetime.fromisoformat(last_seen)
                active = (now - seen_at).total_seconds() <= STAFF_ACTIVE_WINDOW_SECONDS
            except (TypeError, ValueError):
                pass

        accounts.append({
            "username": username,
            "created_at": details.get("created_at", "Inconnue"),
            "last_seen_at": last_seen or "Jamais",
            "active": active,
            "banned_until": get_ban_expiration(details),
        })

    accounts.sort(key=lambda account: (not account["active"], account["username"].casefold()))
    return accounts


def get_ban_expiration(user):
    """Retourne la fin du bannissement si le compte est encore banni."""
    value = user.get("banned_until")
    if not value:
        return None

    try:
        expiration = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None

    return expiration if expiration > datetime.now(timezone.utc) else None


def is_user_banned(user):
    return get_ban_expiration(user) is not None


def revoke_user_tokens(username):
    """Invalide les cookies de connexion persistante d'un compte."""
    tokens = load_json(TOKENS_FILE, {})
    wanted = username.casefold()
    filtered = {
        token_hash: details
        for token_hash, details in tokens.items()
        if str(details.get("username", "")).casefold() != wanted
    }

    if len(filtered) != len(tokens):
        save_json(TOKENS_FILE, filtered)


def remove_user_account(username):
    """Supprime le compte et ses donnÃ©es locales aprÃ¨s effacement Discord."""
    users = load_json(USERS_FILE, {})
    user_key = find_user_key(users, username)
    if not user_key:
        return False

    # Cette Ã©tape peut lever une erreur : dans ce cas le compte local reste
    # intact, pour respecter la promesse de suppression Ã©galement sur Discord.
    discord_bridge.delete_user_conversations(user_key)

    del users[user_key]
    save_json(USERS_FILE, users)
    revoke_user_tokens(user_key)

    conversations = load_json(CONVERSATIONS_FILE, {})
    for conversation_owner in list(conversations):
        if conversation_owner.casefold() == user_key.casefold():
            del conversations[conversation_owner]
    save_json(CONVERSATIONS_FILE, conversations)
    return True


def remove_all_user_conversations(username):
    """Efface les discussions d'un compte, y compris leurs salons Discord."""
    # Comme pour la suppression du compte, ne pas supprimer la copie locale
    # tant que Discord n'a pas confirmé la suppression de ses salons.
    discord_bridge.delete_user_conversations(username)

    conversations = load_json(CONVERSATIONS_FILE, {})
    owner_key = next(
        (key for key in conversations if key.casefold() == username.casefold()),
        None,
    )
    count = len(conversations.get(owner_key, [])) if owner_key else 0
    if owner_key:
        del conversations[owner_key]
        save_json(CONVERSATIONS_FILE, conversations)
    return count


def staff_dashboard_stats(accounts):
    """Calcule des statistiques globales sans exposer de données sensibles."""
    conversations = load_json(CONVERSATIONS_FILE, {})
    conversation_count = 0
    message_count = 0
    generated_image_count = 0

    for items in conversations.values():
        if not isinstance(items, list):
            continue
        conversation_count += len(items)
        for conversation in items:
            messages = conversation.get("messages", []) if isinstance(conversation, dict) else []
            if not isinstance(messages, list):
                continue
            message_count += len(messages)
            for message in messages:
                if not isinstance(message, dict):
                    continue
                if message.get("image_url"):
                    generated_image_count += 1
                generated_image_count += len(message.get("cricut_images", []) or [])

    return {
        "accounts": len(accounts),
        "active": sum(account["active"] for account in accounts),
        "banned": sum(bool(account["banned_until"]) for account in accounts),
        "cricut_accounts": sum(
            1 for details in load_json(USERS_FILE, {}).values()
            if details.get("cricut_enabled")
        ),
        "conversations": conversation_count,
        "messages": message_count,
        "images": generated_image_count,
    }


def staff_is_authenticated():
    now_timestamp = int(datetime.now(timezone.utc).timestamp())
    return bool(STAFF_PASSWORD) and int(
        session.get("staff_access_until", 0) or 0
    ) > now_timestamp


def get_staff_csrf_token():
    token = session.get("staff_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["staff_csrf_token"] = token
    return token


def get_settings_csrf_token():
    token = session.get("settings_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["settings_csrf_token"] = token
    return token


def render_staff_dashboard(temporary_password=None, temporary_username=None):
    accounts = staff_account_summaries()
    return render_template(
        "staff.html",
        authenticated=True,
        accounts=accounts,
        stats=staff_dashboard_stats(accounts),
        active_window_minutes=STAFF_ACTIVE_WINDOW_SECONDS // 60,
        staff_csrf_token=get_staff_csrf_token(),
        temporary_password=temporary_password,
        temporary_username=temporary_username,
    )


# ============================================================
# CREER TOKEN DE CONNEXION
# ============================================================

def create_remember_token(
    username,
    ip
):

    raw_token = secrets.token_urlsafe(48)


    token_hash = hash_token(
        raw_token
    )


    tokens = load_json(
        TOKENS_FILE,
        {}
    )


    tokens[token_hash] = {

        "username":
            username,

        "created_at":
            utc_now(),

        "last_seen_at":
            utc_now(),

        "last_ip":
            ip,

    }


    save_json(
        TOKENS_FILE,
        tokens
    )


    return raw_token


# ============================================================
# REVOQUER TOKEN
# ============================================================

def revoke_token(raw_token):

    if not raw_token:

        return


    tokens = load_json(
        TOKENS_FILE,
        {}
    )


    token_hash = hash_token(
        raw_token
    )


    if token_hash in tokens:

        del tokens[token_hash]


        save_json(
            TOKENS_FILE,
            tokens
        )


# ============================================================
# SESSION CONNECTEE
# ============================================================

def set_login_session(username):

    session.clear()


    session["username"] = username


    session.permanent = True


# ============================================================
# COOKIE AUTOMATIQUE
# ============================================================

def response_with_remember_cookie(
    response,
    username,
    ip
):

    raw_token = create_remember_token(
        username,
        ip
    )


    response.set_cookie(

        REMEMBER_COOKIE_NAME,

        raw_token,

        max_age=
            REMEMBER_DAYS
            * 24
            * 60
            * 60,

        httponly=True,

        # Mets True quand ton site est en HTTPS
        secure=False,

        samesite="Lax",

    )


    return response


# ============================================================
# AUTO LOGIN COOKIE
# ============================================================

def try_cookie_autologin():

    raw_token = request.cookies.get(
        REMEMBER_COOKIE_NAME
    )


    if not raw_token:

        return None


    tokens = load_json(
        TOKENS_FILE,
        {}
    )


    token_hash = hash_token(
        raw_token
    )


    token_info = tokens.get(
        token_hash
    )


    if not token_info:

        return None


    users = load_json(
        USERS_FILE,
        {}
    )


    username = token_info.get(
        "username"
    )


    user_key = find_user_key(
        users,
        username or ""
    )


    if not user_key:

        return None

    if is_user_banned(users[user_key]):
        return None


    ip = client_ip()


    add_ip_to_user(
        users,
        user_key,
        ip
    )


    save_json(
        USERS_FILE,
        users
    )


    token_info["last_seen_at"] = utc_now()

    token_info["last_ip"] = ip


    tokens[token_hash] = token_info


    save_json(
        TOKENS_FILE,
        tokens
    )


    set_login_session(
        user_key
    )


    return user_key


# ============================================================
# AUTO LOGIN PAR IP
# ============================================================

def try_ip_autologin():

    if not ALLOW_IP_AUTOLOGIN:

        return None


    ip = client_ip()


    if (
        not ip
        or ip == "unknown"
    ):

        return None


    users = load_json(
        USERS_FILE,
        {}
    )


    matches = []


    for username, info in users.items():

        known_ips = info.get(
            "known_ips",
            []
        )


        if ip in known_ips and not is_user_banned(info):

            matches.append(
                username
            )


    # On reconnecte uniquement si cette IP
    # correspond à UN SEUL compte.

    if len(matches) != 1:

        return None


    username = matches[0]


    add_ip_to_user(
        users,
        username,
        ip
    )


    save_json(
        USERS_FILE,
        users
    )


    set_login_session(
        username
    )


    return username


# ============================================================
# AUTO LOGIN AVANT CHAQUE PAGE
# ============================================================

@app.before_request
def automatic_login():

    # Pas besoin pour les fichiers CSS etc.

    if request.endpoint == "static":

        return


    if session.get("username"):
        users = load_json(USERS_FILE, {})
        user_key = find_user_key(users, session["username"])

        if not user_key or is_user_banned(users[user_key]):
            session.clear()
            if request.path.startswith("/api/"):
                return jsonify({"error": "Ce compte est temporairement indisponible."}), 403
            flash("Ce compte est temporairement indisponible.", "error")
            return redirect(url_for("login"))

        touch_user_activity(session["username"])
        return


    # Ne pas reconnecter automatiquement
    # quand quelqu'un veut créer un compte
    # ou se déconnecter.

    if request.endpoint in {
        "register",
        "logout"
    }:

        return


    # Cookie en priorité

    if try_cookie_autologin():

        return


    # IP ensuite

    try_ip_autologin()


# ============================================================
# LOGO
# ============================================================

@app.route("/logo.png")
def logo():

    return send_from_directory(
        BASE_DIR,
        "logo.png"
    )


@app.route("/favicon.ico")
def favicon():

    return send_from_directory(
        BASE_DIR,
        "logo.png",
        mimetype="image/png"
    )


@app.route("/service-worker.js")
def service_worker():
    """Expose le worker à la racine afin qu'il contrôle toute la PWA."""
    response = send_from_directory(
        BASE_DIR / "static",
        "service-worker.js",
        mimetype="application/javascript",
    )
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/health")
def health():

    return {"status": "ok"}, 200


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():

    if session.get("username"):

        return redirect(
            url_for("chat")
        )


    return redirect(
        url_for("login")
    )


# ============================================================
# REGISTER
# ============================================================

@app.route(
    "/register",
    methods=[
        "GET",
        "POST"
    ]
)
def register():

    if session.get("username"):

        return redirect(
            url_for("chat")
        )


    if request.method == "POST":

        username = normalize_username(
            request.form.get(
                "username",
                ""
            )
        )


        password = request.form.get(
            "password",
            ""
        )


        password2 = request.form.get(
            "password2",
            ""
        )


        # Validation pseudo

        if not valid_username(
            username
        ):

            flash(
                (
                    "Le pseudo doit contenir entre "
                    "3 et 24 caractères : "
                    "lettres, chiffres, _, . ou -."
                ),
                "error"
            )


            return render_template(
                "register.html"
            )


        # Validation mot de passe

        if len(password) < 8:

            flash(
                (
                    "Le mot de passe doit contenir "
                    "au moins 8 caractères."
                ),
                "error"
            )


            return render_template(
                "register.html"
            )


        # Password confirmation

        if password != password2:

            flash(
                (
                    "Les deux mots de passe "
                    "ne correspondent pas."
                ),
                "error"
            )


            return render_template(
                "register.html"
            )


        users = load_json(
            USERS_FILE,
            {}
        )


        # Vérifie pseudo existant

        if find_user_key(
            users,
            username
        ):

            flash(
                "Ce pseudo existe déjà.",
                "error"
            )


            return render_template(
                "register.html"
            )


        ip = client_ip()


        # Création utilisateur

        users[username] = {

            "password_hash":
                generate_password_hash(
                    password
                ),

            "created_at":
                utc_now(),

            "last_login_at":
                utc_now(),

            "last_ip":
                ip,

            "known_ips":
                (
                    [ip]
                    if ip != "unknown"
                    else []
                ),

        }


        save_json(
            USERS_FILE,
            users
        )


        # Connexion immédiate

        set_login_session(
            username
        )


        response = make_response(

            redirect(
                url_for("chat")
            )

        )


        return response_with_remember_cookie(

            response,
            username,
            ip

        )


    return render_template(
        "register.html"
    )


# ============================================================
# LOGIN
# ============================================================

@app.route(
    "/login",
    methods=[
        "GET",
        "POST"
    ]
)
def login():

    if session.get("username"):

        return redirect(
            url_for("chat")
        )


    if request.method == "POST":

        username = normalize_username(
            request.form.get(
                "username",
                ""
            )
        )


        password = request.form.get(
            "password",
            ""
        )


        users = load_json(
            USERS_FILE,
            {}
        )


        user_key = find_user_key(
            users,
            username
        )


        if not user_key:

            flash(
                "Pseudo ou mot de passe incorrect.",
                "error"
            )


            return render_template(
                "login.html"
            )


        user = users[
            user_key
        ]


        if is_user_banned(user):
            flash(
                "Ce compte est temporairement indisponible.",
                "error"
            )
            return render_template("login.html")


        # Vérifie mot de passe

        if not check_password_hash(

            user.get(
                "password_hash",
                ""
            ),

            password

        ):

            flash(
                "Pseudo ou mot de passe incorrect.",
                "error"
            )


            return render_template(
                "login.html"
            )


        ip = client_ip()


        add_ip_to_user(
            users,
            user_key,
            ip
        )


        save_json(
            USERS_FILE,
            users
        )


        set_login_session(
            user_key
        )


        response = make_response(

            redirect(
                url_for("chat")
            )

        )


        return response_with_remember_cookie(

            response,
            user_key,
            ip

        )


    return render_template(
        "login.html"
    )


# ============================================================
# CHAT
# ============================================================

@app.route("/chat")
def chat():

    username = session.get(
        "username"
    )


    if not username:

        return redirect(
            url_for("login")
        )


    users = load_json(USERS_FILE, {})
    user_key = find_user_key(users, username)

    return render_template(

        "chat.html",

        username=username,
        cricut_enabled=bool(user_key and users[user_key].get("cricut_enabled")),

    )


# ============================================================
# PARAMETRES DU COMPTE
# ============================================================

@app.route("/settings")
def settings():
    username = session.get("username")
    if not username:
        return redirect(url_for("login"))

    users = load_json(USERS_FILE, {})
    user_key = find_user_key(users, username)
    if not user_key:
        session.clear()
        return redirect(url_for("login"))

    return render_template(
        "settings.html",
        username=user_key,
        account=users[user_key],
        settings_csrf_token=get_settings_csrf_token(),
    )


@app.route("/settings/delete-account", methods=["POST"])
def delete_own_account():
    username = session.get("username")
    if not username:
        return redirect(url_for("login"))

    csrf_token = request.form.get("csrf_token", "")
    if not secrets.compare_digest(csrf_token, session.get("settings_csrf_token", "")):
        return "RequÃªte de suppression invalide.", 400

    confirmation = request.form.get("confirmation", "").strip()
    password = request.form.get("password", "")
    users = load_json(USERS_FILE, {})
    user_key = find_user_key(users, username)

    if not user_key:
        session.clear()
        return redirect(url_for("login"))

    if confirmation != "SUPPRIMER MON COMPTE" or not check_password_hash(
        users[user_key].get("password_hash", ""), password
    ):
        flash("La confirmation ou le mot de passe est incorrect.", "error")
        return redirect(url_for("settings"))

    try:
        remove_user_account(user_key)
    except RuntimeError as error:
        flash(str(error), "error")
        return redirect(url_for("settings"))

    session.clear()
    response = make_response(redirect(url_for("login")))
    response.delete_cookie(REMEMBER_COOKIE_NAME, path="/")
    flash("Ton compte, tes conversations et tes salons Discord ont Ã©tÃ© supprimÃ©s.", "success")
    return response


# ============================================================
# PANNEAU STAFF
# ============================================================

@app.route("/staff", methods=["GET", "POST"])
def staff_panel():
    """Panneau staff protÃ©gÃ© par STAFF_PASSWORD (variable Render)."""
    if not STAFF_PASSWORD:
        return "Le panneau staff n'est pas configurÃ©.", 503

    now_timestamp = int(datetime.now(timezone.utc).timestamp())

    if request.method == "POST":
        lock_until = int(session.get("staff_lock_until", 0) or 0)
        supplied_password = request.form.get("password", "")

        if now_timestamp < lock_until:
            flash("Trop de tentatives. RÃ©essaie dans quelques minutes.", "error")
        elif secrets.compare_digest(supplied_password, STAFF_PASSWORD):
            session.pop("staff_attempts", None)
            session.pop("staff_lock_until", None)
            session["staff_access_until"] = now_timestamp + STAFF_SESSION_SECONDS
            return redirect(url_for("staff_panel"))
        else:
            attempts = int(session.get("staff_attempts", 0) or 0) + 1
            session["staff_attempts"] = attempts

            if attempts >= 5:
                session["staff_attempts"] = 0
                session["staff_lock_until"] = now_timestamp + 10 * 60
                flash("Trop de tentatives. RÃ©essaie dans 10 minutes.", "error")
            else:
                flash("Mot de passe incorrect.", "error")

    access_until = int(session.get("staff_access_until", 0) or 0)

    if access_until <= now_timestamp:
        session.pop("staff_access_until", None)
        return render_template("staff.html", authenticated=False)

    return render_staff_dashboard()


@app.route("/staff/accounts/<username>/<action>", methods=["POST"])
def staff_account_action(username, action):
    """Actions sensibles disponibles uniquement au staff authentifiÃ©."""
    if not staff_is_authenticated():
        return "AccÃ¨s staff requis.", 403

    csrf_token = request.form.get("csrf_token", "")
    if not secrets.compare_digest(csrf_token, session.get("staff_csrf_token", "")):
        return "RequÃªte staff invalide.", 400

    users = load_json(USERS_FILE, {})
    user_key = find_user_key(users, username)
    if not user_key:
        flash("Compte introuvable.", "error")
        return redirect(url_for("staff_panel"))

    if action == "reset-password":
        temporary_password = secrets.token_urlsafe(12)
        users[user_key]["password_hash"] = generate_password_hash(temporary_password)
        users[user_key]["password_reset_at"] = utc_now()
        save_json(USERS_FILE, users)
        revoke_user_tokens(user_key)
        return render_staff_dashboard(
            temporary_password=temporary_password,
            temporary_username=user_key,
        )

    if action == "ban":
        try:
            hours = int(request.form.get("ban_hours", "0"))
        except ValueError:
            hours = 0

        if hours not in {1, 24, 72, 168, 720}:
            flash("DurÃ©e de bannissement invalide.", "error")
            return redirect(url_for("staff_panel"))

        users[user_key]["banned_until"] = (
            datetime.now(timezone.utc) + timedelta(hours=hours)
        ).isoformat()
        save_json(USERS_FILE, users)
        revoke_user_tokens(user_key)
        flash(f"{user_key} est banni pendant {hours} h.", "success")
        return redirect(url_for("staff_panel"))

    if action == "unban":
        users[user_key].pop("banned_until", None)
        save_json(USERS_FILE, users)
        flash(f"Bannissement retirÃ© pour {user_key}.", "success")
        return redirect(url_for("staff_panel"))

    if action == "delete-conversations":
        confirmation = request.form.get("confirm_delete_conversations", "").strip()
        if confirmation != "SUPPRIMER":
            flash("Écris SUPPRIMER pour confirmer l'effacement des discussions.", "error")
            return redirect(url_for("staff_panel"))

        try:
            count = remove_all_user_conversations(user_key)
        except RuntimeError as error:
            flash(str(error), "error")
            return redirect(url_for("staff_panel"))

        flash(
            f"{count} discussion(s) et leurs salons Discord ont été supprimés pour {user_key}.",
            "success",
        )
        return redirect(url_for("staff_panel"))

    if action == "delete":
        confirmation = request.form.get("confirm_username", "").strip()
        if confirmation.casefold() != user_key.casefold():
            flash("Pour supprimer ce compte, Ã©cris exactement son pseudo.", "error")
            return redirect(url_for("staff_panel"))

        try:
            remove_user_account(user_key)
        except RuntimeError as error:
            flash(str(error), "error")
            return redirect(url_for("staff_panel"))
        flash(f"Le compte {user_key}, ses conversations et ses salons Discord ont Ã©tÃ© supprimÃ©s.", "success")
        return redirect(url_for("staff_panel"))

    return "Action staff inconnue.", 404


# ============================================================
# RELAIS DISCORD
# ============================================================

@app.route(
    "/api/discord/turn",
    methods=["POST"]
)
def discord_turn():

    username = session.get("username")

    if not username:
        return jsonify({"error": "Connexion requise."}), 401

    payload = request.get_json(silent=True) or request.form
    question = str(payload.get("question", "")).strip()
    conversation_id = str(payload.get("conversation_id", "")).strip()

    if not question:
        return jsonify({"error": "La question est vide."}), 400

    if len(question) > 2000:
        return jsonify({"error": "La question est limitée à 2 000 caractères."}), 400

    if not re.fullmatch(r"[a-zA-Z0-9_-]{8,80}", conversation_id):
        return jsonify({"error": "Identifiant de conversation invalide."}), 400

    # Cette commande ne doit jamais partir au bot : elle sert uniquement à
    # attribuer l'accès Cricut au compte actuellement connecté.
    if question == "CRICUT_ALLOW_PERMS_TRUE":
        users = load_json(USERS_FILE, {})
        user_key = find_user_key(users, username)
        if not user_key:
            return jsonify({"error": "Compte introuvable."}), 404

        users[user_key]["cricut_enabled"] = True
        users[user_key]["cricut_enabled_at"] = utc_now()
        save_json(USERS_FILE, users)

        # La commande d'activation reste confidentielle et sa discussion est
        # effacée, côté site comme côté Discord si un salon existe déjà.
        delete_conversation(user_key, conversation_id)
        discord_bridge.delete_conversation(user_key, conversation_id)
        return jsonify({
            "cricut_activated": True,
            "message": "Compte Cricut activé.",
        })

    # La commande Pro est traitée localement afin de ne jamais être envoyée
    # au bot Discord ni conservée dans la discussion.
    if question == "PRO_ACCOUNT=True":
        users = load_json(USERS_FILE, {})
        user_key = find_user_key(users, username)
        if not user_key:
            return jsonify({"error": "Compte introuvable."}), 404

        users[user_key]["pro_enabled"] = True
        users[user_key]["pro_enabled_at"] = utc_now()
        save_json(USERS_FILE, users)
        delete_conversation(user_key, conversation_id)
        discord_bridge.delete_conversation(user_key, conversation_id)
        return jsonify({
            "pro_activated": True,
            "message": "Compte Pro activé. Les générations d'images sont déjà illimitées pour tous les comptes.",
        })

    try:
        reference_images = get_reference_images()
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    expects_image = is_image_generation_request(
        question,
        bool(reference_images),
    )

    save_conversation_message(
        username,
        conversation_id,
        "user",
        question
    )

    try:
        job_id = discord_bridge.start_turn(
            username,
            conversation_id,
            question,
            reference_images,
            expects_image=expects_image,
        )
    except RuntimeError as error:
        save_conversation_message(
            username,
            conversation_id,
            "assistant",
            str(error)
        )
        return jsonify({"error": str(error)}), 503
    except Exception:
        app.logger.exception("Impossible d'envoyer la question à Discord")
        save_conversation_message(
            username,
            conversation_id,
            "assistant",
            "Impossible d'envoyer la question à Discord."
        )
        return jsonify({"error": "Impossible d'envoyer la question à Discord."}), 502

    return jsonify({"job_id": job_id})


@app.route("/api/cricut/start", methods=["POST"])
def start_cricut_job():
    """Démarre une décomposition d'image réservée aux comptes Cricut."""
    username = session.get("username")
    if not username:
        return jsonify({"error": "Connexion requise."}), 401

    users = load_json(USERS_FILE, {})
    user_key = find_user_key(users, username)
    if not user_key or not users[user_key].get("cricut_enabled"):
        return jsonify({"error": "Le mode Cricut n'est pas activé sur ce compte."}), 403

    try:
        reference_images = get_reference_images()
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    if len(reference_images) != 1:
        return jsonify({"error": "Ajoute exactement une image à adapter en stickers."}), 400

    try:
        job_id = discord_bridge.start_cricut_job(user_key, reference_images[0])
    except RuntimeError as error:
        return jsonify({"error": str(error)}), 503
    except Exception:
        app.logger.exception("Impossible de démarrer la décomposition Cricut")
        return jsonify({"error": "Impossible d'envoyer l'image au bot Discord."}), 502

    conversation_id = discord_bridge.job_conversation(job_id, user_key)
    save_conversation_message(
        user_key,
        conversation_id,
        "user",
        f"✦ Adaptation Cricut : {reference_images[0]['filename']}",
    )

    return jsonify({
        "job_id": job_id,
        "conversation_id": conversation_id,
    })


@app.route("/api/discord/jobs/<job_id>/events")
def discord_job_events(job_id):

    username = session.get("username")

    if not username:
        return jsonify({"error": "Connexion requise."}), 401

    conversation_id = discord_bridge.job_conversation(
        job_id,
        username
    )

    if not conversation_id:
        return jsonify({"error": "Cette génération n'existe plus."}), 404

    def event_stream():

        while True:
            event = discord_bridge.next_event(job_id, username)

            if event is None:
                yield ": keep-alive\n\n"
                continue

            yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"

            if event.get("type") in {"image", "text", "error", "cricut_complete"}:
                return

    response = Response(
        stream_with_context(event_stream()),
        mimetype="text/event-stream"
    )

    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"

    return response


@app.route("/api/conversations")
def conversations():

    username = session.get("username")

    if not username:
        return jsonify({"error": "Connexion requise."}), 401

    return jsonify({"conversations": get_conversation_summaries(username)})


@app.route("/api/conversations/<conversation_id>")
def conversation_detail(conversation_id):

    username = session.get("username")

    if not username:
        return jsonify({"error": "Connexion requise."}), 401

    conversation = get_conversation(username, conversation_id)

    if not conversation:
        return jsonify({"error": "Discussion introuvable."}), 404

    return jsonify({"conversation": conversation})


# ============================================================
# LOGOUT
# ============================================================

@app.route("/logout")
def logout():

    username = session.get(
        "username"
    )


    current_ip = client_ip()


    raw_token = request.cookies.get(
        REMEMBER_COOKIE_NAME
    )


    # Supprime le token du JSON

    revoke_token(
        raw_token
    )


    # En cas de déconnexion manuelle,
    # retire l'IP actuelle des IP reconnues.

    if username:

        users = load_json(
            USERS_FILE,
            {}
        )


        user_key = find_user_key(
            users,
            username
        )


        if user_key:

            known_ips = users[
                user_key
            ].get(
                "known_ips",
                []
            )


            users[user_key][
                "known_ips"
            ] = [

                ip

                for ip in known_ips

                if ip != current_ip

            ]


            save_json(
                USERS_FILE,
                users
            )


    # Supprime session

    session.clear()


    response = make_response(

        redirect(
            url_for("login")
        )

    )


    # Supprime cookie

    response.delete_cookie(
        REMEMBER_COOKIE_NAME
    )


    return response


# ============================================================
# ERREUR 404
# ============================================================

@app.errorhandler(404)
def page_not_found(error):

    if request.path.startswith("/static/"):
        return "", 404

    if session.get("username"):

        return redirect(
            url_for("chat")
        )


    return redirect(
        url_for("login")
    )


# ============================================================
# START SERVER
# ============================================================

if __name__ == "__main__":

    print()
    print("=" * 55)
    print("           NathGPT Server")
    print("=" * 55)
    print()
    print("Serveur lancé :")
    print()
    print("http://127.0.0.1:5000")
    print()
    print("CTRL + C pour arrêter.")
    print()
    print("=" * 55)
    print()


    app.run(

        host="0.0.0.0",

        port=int(
            os.environ.get(
                "PORT",
                "5000"
            )
        ),

        # Le reloader démarre le programme deux fois et provoque le
        # SystemExit affiché par VS Code. Il doit rester désactivé car
        # le client Discord est lui aussi démarré dans ce processus.
        debug=False,

        use_reloader=False

    )
