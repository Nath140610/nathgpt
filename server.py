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

from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from pathlib import Path
from datetime import datetime, timezone

import hashlib
import json
import os
import re
import secrets
import threading

from discord_bridge import DiscordBridge


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = Path(
    os.environ.get(
        "NATHGPT_DATA_DIR",
        str(BASE_DIR / "data")
    )
)

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

ALLOW_IP_AUTOLOGIN = True


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


# Création automatique du dossier data

DATA_DIR.mkdir(
    parents=True,
    exist_ok=True
)


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
    image_url=None
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


def start_runtime_services():

    if os.environ.get(
        "DISCORD_AUTOSTART",
        "true"
    ).lower() == "true":
        discord_bridge.start()


# Gunicorn importe ce module au démarrage : le bot se lance également
# en production, avec un seul worker configuré dans render.yaml.
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


        if ip in known_ips:

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


@app.route("/health")
def health():

    return {"status": "ok"}, 200


# ============================================================
# IMAGE TEST BRE.PNG
# ============================================================

@app.route("/BRE.png")
def bre_image():

    return send_from_directory(
        BASE_DIR,
        "BRE.png"
    )


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


    return render_template(

        "chat.html",

        username=username

    )


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

    try:
        reference_images = get_reference_images()
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

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
            reference_images
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

            if event.get("type") in {"image", "text", "error"}:
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
