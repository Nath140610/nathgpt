import os
import sys
import time
import socket
import asyncio
import subprocess
import io
import base64
import re
import hashlib
import tempfile
import uuid
import shutil
import json
import threading
import mimetypes
import urllib.parse

import aiohttp
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import pyperclip
import discord
from PIL import Image

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains


# ============================================================
# NATHGPT FINAL ORACLE — Discord → ChatGPT (Selenium + Chromium/Linux)
# - Statuts Discord simples en texte (aucun embed de progression)
# - Génération image avec pourcentage estimé
# - Modification par réponse `modify: ...`
# - PNG par réponse `png:`
# - Détection d'image renforcée (ignore les canvases/animations de chargement)
# - Attend la vraie image finale <img> avant envoi Discord
# - Tous les salons de la catégorie Discord configurée sont acceptés
# - Mémoire persistante indépendante par salon Discord
# - Pièces jointes Discord envoyées à ChatGPT
# - Images d'embeds Discord (bots/webhooks/apps) récupérées et envoyées à ChatGPT
# - Annulation automatique + commande retry:
# - Statut Discord dynamique selon la file d'attente
# - Messages de bots / webhooks acceptés (sauf le bot lui-même)
# - Upload ChatGPT anti-doublon robuste
# ============================================================


# ============================================================
# CONFIGURATION DISCORD
# ============================================================

# ⚠️ Mets le token de TON bot Discord ici.
# Ne partage jamais ce token.
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "MET_TON_TOKEN_ICI")

# ID de la catégorie Discord écoutée par NathGPT.
# Tous les salons texte placés dans cette catégorie sont acceptés.
# La valeur par défaut correspond à la catégorie demandée.
try:
    DISCORD_CATEGORY_ID = int(
        os.getenv("DISCORD_CATEGORY_ID", "1539922989200576512")
    )
except ValueError:
    DISCORD_CATEGORY_ID = 1539922989200576512

# Ancienne variable conservée uniquement pour ne pas casser un ancien .env.
# Elle n'est plus utilisée pour filtrer les messages.
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID", "")

# Conservé pour compatibilité avec les anciennes versions. Les boutons ne sont plus utilisés.
ONLY_AUTHOR_CAN_EDIT = True

# V11 : accepte les messages envoyés par d'autres bots et par des webhooks.
# Le bot s'ignore TOUJOURS lui-même pour éviter une boucle infinie.
ACCEPTER_MESSAGES_BOTS = True
ACCEPTER_WEBHOOKS = True

# IDs de bots à ignorer si tu veux éviter qu'un bot précis déclenche NathGPT.
IGNORE_BOT_IDS = set()

# Nombre maximum de versions gardées dans l'historique d'une image.
HISTORY_MAX_VERSIONS = 25

# Pièces jointes Discord : limites de sécurité/mémoire côté VM.
MAX_ATTACHMENTS = int(os.getenv("NATHGPT_MAX_ATTACHMENTS", "5"))
MAX_ATTACHMENT_BYTES = int(os.getenv("NATHGPT_MAX_ATTACHMENT_BYTES", str(25 * 1024 * 1024)))
MAX_ATTACHMENTS_TOTAL_BYTES = int(os.getenv("NATHGPT_MAX_ATTACHMENTS_TOTAL_BYTES", str(50 * 1024 * 1024)))

# Mémoire persistante : un salon Discord = une conversation ChatGPT.
MEMORY_FILE = os.getenv(
    "NATHGPT_MEMORY_FILE",
    "/home/ubuntu/NathGPT_V11_Oracle/channel_memory.json",
)
MAX_MEMORY_CHANNELS = int(os.getenv("NATHGPT_MAX_MEMORY_CHANNELS", "250"))

# Annulation automatique. Le délai interne de ChatGPT reste également actif.
AUTO_CANCEL_TIMEOUTS = {
    "texte": int(os.getenv("NATHGPT_AUTOCANCEL_TEXT", "210")),
    "code": int(os.getenv("NATHGPT_AUTOCANCEL_CODE", "300")),
    "image": int(os.getenv("NATHGPT_AUTOCANCEL_IMAGE", "360")),
    "edition_image": int(os.getenv("NATHGPT_AUTOCANCEL_EDIT", "360")),
    "png_image": int(os.getenv("NATHGPT_AUTOCANCEL_PNG", "360")),
    "decomp_count": int(os.getenv("NATHGPT_AUTOCANCEL_DECOMP_COUNT", "600")),
    "decomp_image": int(os.getenv("NATHGPT_AUTOCANCEL_DECOMP_IMAGE", "480")),
}


# ============================================================
# CONFIGURATION CHATGPT / CHROMIUM (ORACLE CLOUD / LINUX)
# ============================================================

DEBUG_PORT = int(os.getenv("NATHGPT_DEBUG_PORT", "9222"))
CHATGPT_URL = "https://chatgpt.com/"

# Oracle/Ubuntu : Chromium tourne dans l'écran virtuel Xvfb :99.
DISPLAY_ENV = os.getenv("DISPLAY", ":99")

# Le snap Chromium est confiné : on garde profil + uploads dans son dossier autorisé.
HOME_DIR = os.path.expanduser("~")
SNAP_COMMON_DIR = os.path.join(HOME_DIR, "snap", "chromium", "common")
PROFILE_DIR = os.getenv(
    "NATHGPT_PROFILE_DIR",
    os.path.join(SNAP_COMMON_DIR, "nathgpt-profile"),
)
UPLOAD_DIR = os.getenv(
    "NATHGPT_UPLOAD_DIR",
    os.path.join(SNAP_COMMON_DIR, "nathgpt-uploads"),
)

# Laisse False pour Oracle + noVNC : le navigateur reste visible dans le bureau virtuel.
CHROMIUM_HEADLESS = os.getenv("NATHGPT_HEADLESS", "0").strip().lower() in {"1", "true", "yes", "oui"}
AFFICHER_CHROMIUM_PENDANT_GENERATION = not CHROMIUM_HEADLESS

# Temps maximum pour une réponse texte/code.
TEXT_TIMEOUT = 180

# Les images peuvent prendre plusieurs minutes.
IMAGE_TIMEOUT = 420

# Les demandes normales utilisent maintenant une mémoire indépendante par salon.
# Cette variable reste uniquement pour compatibilité avec les anciennes versions.
NEW_CHAT_EACH_QUESTION = False

# Prompt EXACT envoyé quand quelqu’un répond `png:` à une image.
PNG_REGEN_PROMPT = """Analyse l’image jointe. Si elle contient une planche de stickers (sinon generer en format png sans fond l'image), régénère directement une nouvelle version optimisée pour l’impression et la découpe avec une Cricut.
Contraintes :

- Format final : **A4 portrait (21 × 29,7 cm)**
- Fond : **totalement transparent**
- Conserve exactement le style, les couleurs, les textes et les illustrations des stickers d’origine.
- Chaque sticker doit être **entièrement séparé des autres**.
- Laisse au minimum **X cm d’espace entre chaque sticker**.
- Laisse **X cm de marge autour de la planche**.
- Ajoute/conserve un **contour blanc propre d’environ X mm** autour de chaque sticker.
- Aucun contour blanc ne doit toucher celui d’un autre sticker.
- Ne coupe aucun élément et ne superpose aucun sticker.
- Répartis les stickers harmonieusement pour utiliser au mieux toute la feuille A4.
- Génère l’image en **PNG haute résolution avec transparence**, prête à être importée dans **Cricut Design Space – Imprimer puis découper**.

Ne décris pas le résultat : **génère directement la nouvelle image**."""

DECOMP_CRICUT_COMMANDS = {":decomp_cricut", "decomp_cricut"}
DECOMP_CRICUT_COUNT_PROMPT = "combien de stikers il y a dans cette image? repond un nombre sans detailler"
DECOMP_CRICUT_MINUTES_PER_IMAGE = 2
DECOMP_CRICUT_MEMORY_KEY_SUFFIX = ":decomp_cricut"
DECOMP_CRICUT_ITEM_PROMPT_TEMPLATE = (
    "Analyse l'image jointe. "
    "Elle montre une planche de stickers. "
    "Genere le {index} stikers de cette planche separe des autres. "
    "Genere format png sans fond avec des bords tres fin. "
    "Ne decris pas le resultat : genere directement l'image."
)


# ============================================================
# ESTIMATION DE PROGRESSION
# ============================================================

# Ce ne sont PAS des pourcentages fournis par ChatGPT.
# Ce sont des estimations locales qui s'améliorent au fil des générations.
DUREES_MOYENNES = {
    "texte": 22.0,
    "code": 38.0,
    "image": 95.0,
    "edition_image": 90.0,
    "png_image": 95.0,
}


def enregistrer_duree(type_generation: str, duree: float):
    """Moyenne exponentielle pour améliorer l'ETA au fil du temps."""
    ancienne = DUREES_MOYENNES.get(type_generation, duree)
    DUREES_MOYENNES[type_generation] = ancienne * 0.70 + duree * 0.30


def barre_progression(pourcentage: int, taille: int = 12) -> str:
    rempli = round((pourcentage / 100) * taille)
    return "█" * rempli + "░" * (taille - rempli)


def format_temps(secondes: float) -> str:
    secondes = max(0, int(secondes))
    if secondes < 60:
        return f"{secondes}s"
    minutes, sec = divmod(secondes, 60)
    return f"{minutes}min {sec:02d}s"


def calculer_progression(type_generation: str, elapsed: float):
    moyenne = max(5.0, DUREES_MOYENNES.get(type_generation, 30.0))

    # Pourcentage purement local. On ne met 100 % qu'une fois le résultat
    # réellement récupéré. Pour les images on continue à avancer doucement
    # jusqu'à 98 % au lieu de rester figé à 95 %.
    ratio = elapsed / moyenne
    plafond = 98 if type_generation in ("image", "edition_image", "png_image") else 96
    pourcentage = int(min(plafond, max(2, ratio * 90)))

    restant = moyenne - elapsed
    if restant <= 0:
        eta = "quelques instants"
    else:
        eta = f"≈ {format_temps(restant)}"

    return pourcentage, eta


def tronquer_texte(texte: str, limite: int, suffixe: str = "…") -> str:
    texte = (texte or "").strip()
    if len(texte) <= limite:
        return texte
    return texte[: max(0, limite - len(suffixe))].rstrip() + suffixe



def texte_progression_simple(type_generation: str, pct: int) -> str:
    if type_generation in ("image", "edition_image", "png_image"):
        return f"generation image en cours image {pct}%"
    return "reflexion en cours"


def texte_file_simple(position: int | None = None, total: int | None = None) -> str:
    if position is None:
        return "demande en attente"
    if total is None:
        return f"demande en attente - position {position}"
    return f"demande en attente - position {position}/{total}"


async def boucle_progression(message_statut: discord.Message, type_generation: str, stop_event: asyncio.Event):
    debut = time.monotonic()
    dernier_texte = None

    while not stop_event.is_set():
        elapsed = time.monotonic() - debut
        pct, _eta = calculer_progression(type_generation, elapsed)
        texte = texte_progression_simple(type_generation, pct)

        if texte != dernier_texte:
            try:
                await message_statut.edit(
                    content=texte,
                    embed=None,
                    attachments=[],
                )
                dernier_texte = texte
            except Exception:
                pass

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass


# ============================================================
# OUTILS CHROMIUM / ORACLE CLOUD
# ============================================================


def trouver_chromium():
    candidats = [
        os.getenv("NATHGPT_CHROMIUM_BIN", ""),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        "/snap/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]
    for chemin in candidats:
        if chemin and os.path.exists(chemin):
            return chemin
    return None


def trouver_chromedriver():
    candidats = [
        os.getenv("NATHGPT_CHROMEDRIVER_BIN", ""),
        shutil.which("chromium.chromedriver"),
        shutil.which("chromedriver"),
        "/snap/bin/chromium.chromedriver",
        "/usr/bin/chromedriver",
        "/usr/lib/chromium-browser/chromedriver",
        "/usr/lib/chromium/chromedriver",
    ]
    for chemin in candidats:
        if chemin and os.path.exists(chemin):
            return chemin
    return None


def port_ouvert(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except Exception:
        return False


def lancer_chromium():
    chromium = trouver_chromium()
    if chromium is None:
        raise RuntimeError(
            "Chromium est introuvable. Sur Ubuntu Oracle, exécute : sudo snap install chromium"
        )

    os.makedirs(PROFILE_DIR, exist_ok=True)
    os.makedirs(UPLOAD_DIR, exist_ok=True)

    print(f"🌐 Lancement de Chromium sur DISPLAY={DISPLAY_ENV}...")
    print(f"📁 Profil persistant : {PROFILE_DIR}")

    commande = [
        chromium,
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={DEBUG_PORT}",
        f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-software-rasterizer",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--ozone-platform=x11",
        "--window-size=1920,1080",
    ]

    if CHROMIUM_HEADLESS:
        commande.append("--headless=new")

    commande.append(CHATGPT_URL)

    env = os.environ.copy()
    env["DISPLAY"] = DISPLAY_ENV

    subprocess.Popen(
        commande,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )

    print("⏳ Attente de Chromium...")
    for _ in range(60):
        if port_ouvert(DEBUG_PORT):
            print("✅ Chromium est prêt.")
            return
        time.sleep(1)

    raise RuntimeError(
        "Impossible de démarrer Chromium. Vérifie Xvfb (:99), le snap Chromium et les logs systemd."
    )


def connecter_chromium():
    if not port_ouvert(DEBUG_PORT):
        lancer_chromium()

    print("🔗 Connexion Selenium → Chromium...")

    options = Options()
    options.add_experimental_option("debuggerAddress", f"127.0.0.1:{DEBUG_PORT}")

    driver_path = trouver_chromedriver()
    try:
        if driver_path:
            print(f"🧩 ChromeDriver : {driver_path}")
            driver = webdriver.Chrome(service=Service(driver_path), options=options)
        else:
            print("⚠️ ChromeDriver local non trouvé : tentative Selenium Manager...")
            driver = webdriver.Chrome(options=options)
    except Exception as e:
        raise RuntimeError(
            "Impossible de connecter Selenium à Chromium. Vérifie que chromium.chromedriver "
            f"est installé et correspond à Chromium. Détail : {e}"
        ) from e

    driver.set_script_timeout(20)

    try:
        if "chatgpt.com" not in (driver.current_url or ""):
            driver.get(CHATGPT_URL)
            time.sleep(3)
    except Exception:
        driver.get(CHATGPT_URL)
        time.sleep(3)

    zone = trouver_zone_question(driver, timeout=6)
    if zone is None:
        print()
        print("⚠️ ChatGPT n'est probablement pas encore connecté.")
        print("➡️ Ouvre noVNC via le tunnel SSH et connecte-toi une fois dans Chromium.")
        print("➡️ Le profil est persistant : la connexion sera conservée après redémarrage.")
        print()

    print("✅ Selenium est connecté à Chromium.")
    return driver


def fermer_chromium_complet(driver=None):
    """
    Ferme la fenêtre ChatGPT + Chromium puis attend que le port de debug soit libéré.
    Le profil Chromium n'est PAS supprimé : la session ChatGPT reste conservée.
    """
    print("Nettoyage RAM : fermeture complete de Chromium...")

    if driver is not None:
        # Essaye d'abord de demander directement au navigateur de se fermer.
        try:
            driver.execute_cdp_cmd("Browser.close", {})
        except Exception:
            try:
                driver.quit()
            except Exception:
                pass

    # Laisse un peu de temps à Chromium pour s'arrêter proprement.
    for _ in range(20):
        if not port_ouvert(DEBUG_PORT):
            break
        time.sleep(0.25)

    # Si un processus Chromium du profil NathGPT reste en mémoire,
    # on le termine sans toucher aux autres profils éventuels.
    if port_ouvert(DEBUG_PORT):
        try:
            subprocess.run(
                ["pkill", "-f", f"--user-data-dir={PROFILE_DIR}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
                check=False,
            )
        except Exception as e:
            print("Nettoyage Chromium via pkill impossible :", repr(e))

    for _ in range(30):
        if not port_ouvert(DEBUG_PORT):
            break
        time.sleep(0.25)

    print("Nettoyage RAM : Chromium ferme.")


# ============================================================
# CHATGPT : DOM / DÉTECTION
# ============================================================


def trouver_zone_question(driver, timeout=15):
    fin = time.time() + timeout

    selecteurs = [
        (By.ID, "prompt-textarea"),
        (By.CSS_SELECTOR, "div[contenteditable='true']"),
        (By.CSS_SELECTOR, "textarea"),
    ]

    while time.time() < fin:
        for by, selector in selecteurs:
            try:
                elements = driver.find_elements(by, selector)
                for element in elements:
                    if element.is_displayed() and element.is_enabled():
                        return element
            except Exception:
                pass
        time.sleep(0.4)

    return None


def recuperer_messages_assistant(driver):
    selecteurs = [
        "[data-message-author-role='assistant']",
        "article[data-turn='assistant']",
    ]

    for selector in selecteurs:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
            if elements:
                return elements
        except Exception:
            pass

    return []


def chatgpt_ecrit_encore(driver):
    selecteurs_stop = [
        "button[data-testid='stop-button']",
        "button[aria-label*='Stop']",
        "button[aria-label*='Arrêter']",
        "button[aria-label*='stop']",
    ]

    for selector in selecteurs_stop:
        try:
            boutons = driver.find_elements(By.CSS_SELECTOR, selector)
            for bouton in boutons:
                if bouton.is_displayed():
                    return True
        except Exception:
            pass

    return False


def lire_texte_element(element):
    try:
        return element.text.strip()
    except Exception:
        return ""


def _infos_image(driver, img):
    """Retourne les infos utiles d'un élément visuel sans faire échouer la boucle."""
    try:
        infos = driver.execute_script(
            """
            const el = arguments[0];
            const r = el.getBoundingClientRect();
            const tag = (el.tagName || '').toLowerCase();
            return {
                tag: tag,
                src: (el.currentSrc || el.src || ''),
                nw: el.naturalWidth || 0,
                nh: el.naturalHeight || 0,
                cw: Math.round(r.width || el.clientWidth || 0),
                ch: Math.round(r.height || el.clientHeight || 0),
                complete: tag === 'img' ? !!el.complete : false,
                visible: !!(r.width > 1 && r.height > 1),
                top: r.top + window.scrollY,
                left: r.left + window.scrollX,
                alt: el.alt || ''
            };
            """,
            img,
        )
        return infos or {}
    except Exception:
        return {}


def _signature_image(driver, img):
    infos = _infos_image(driver, img)
    src = (infos.get("src") or "").strip()
    if src:
        return src

    # Rare fallback si ChatGPT affiche une image sans src exploitable.
    return (
        f"nosrc|{infos.get('nw', 0)}x{infos.get('nh', 0)}|"
        f"{infos.get('cw', 0)}x{infos.get('ch', 0)}|{infos.get('alt', '')}"
    )


def _src_image_est_exploitable(src: str) -> bool:
    """Écarte les ressources connues de chargement/placeholder de l'interface."""
    src = (src or "").strip().lower()
    if not src:
        return False
    if src.startswith("data:image/svg"):
        return False
    mots_interdits = ("placeholder", "spinner", "loading", "skeleton", "shimmer")
    return not any(mot in src for mot in mots_interdits)


def images_valides_page(driver):
    """
    Cherche uniquement les vraies images finales affichées par ChatGPT.

    IMPORTANT : on ignore volontairement les <canvas>. Pendant la génération,
    ChatGPT affiche une animation de points dans un grand canvas. L'ancienne
    version prenait parfois une capture de ce canvas et l'envoyait à Discord
    avant que l'image finale soit prête.
    """
    try:
        candidats = driver.find_elements(By.CSS_SELECTOR, "img")
    except Exception:
        return []

    resultat = []

    for img in candidats:
        infos = _infos_image(driver, img)
        if not infos or infos.get("tag") != "img":
            continue
        if not infos.get("visible") or not infos.get("complete"):
            continue

        src = (infos.get("src") or "").strip()
        if not _src_image_est_exploitable(src):
            continue

        nw = int(infos.get("nw") or 0)
        nh = int(infos.get("nh") or 0)
        cw = int(infos.get("cw") or 0)
        ch = int(infos.get("ch") or 0)

        # Une image générée est à la fois grande dans son fichier ET grande à
        # l'écran. Cela exclut avatars, miniatures de la sidebar et pièces jointes.
        if nw >= 256 and nh >= 256 and cw >= 220 and ch >= 220:
            resultat.append(img)

    return resultat


def images_valides_dans_message(driver, message_element):
    """Compatibilité avec l'ancienne détection limitée à un message."""
    images = []

    try:
        candidats = message_element.find_elements(By.CSS_SELECTOR, "img")
    except Exception:
        return []

    for img in candidats:
        infos = _infos_image(driver, img)
        if not infos or infos.get("tag") != "img":
            continue
        if not infos.get("visible") or not infos.get("complete"):
            continue
        if not _src_image_est_exploitable(infos.get("src") or ""):
            continue

        nw = int(infos.get("nw") or 0)
        nh = int(infos.get("nh") or 0)
        cw = int(infos.get("cw") or 0)
        ch = int(infos.get("ch") or 0)

        if nw >= 256 and nh >= 256 and cw >= 220 and ch >= 220:
            images.append(img)

    return images


def compter_boutons_modifier_image(driver):
    """Compte les boutons Modifier/Edit visibles associés aux images ChatGPT."""
    compteur = 0
    try:
        boutons = driver.find_elements(By.TAG_NAME, "button")
    except Exception:
        return 0

    for bouton in boutons:
        try:
            if not bouton.is_displayed():
                continue
            texte = (bouton.text or "").strip().lower()
            aria = (bouton.get_attribute("aria-label") or "").strip().lower()
            cible = f"{texte} {aria}"
            if "modifier" in cible or "edit image" in cible or cible.strip() == "edit":
                compteur += 1
        except Exception:
            pass

    return compteur


def _empreinte_contenu_image(contenu: bytes) -> str:
    """Empreinte basée sur les pixels pour reconnaître la même image, même si son URL change."""
    if not contenu:
        return ""

    try:
        with Image.open(io.BytesIO(contenu)) as im:
            rgba = im.convert("RGBA")
            h = hashlib.sha256()
            h.update(f"{rgba.width}x{rgba.height}|RGBA|".encode("ascii"))
            h.update(rgba.tobytes())
            return h.hexdigest()
    except Exception:
        return hashlib.sha256(contenu).hexdigest()


def convertir_bytes_en_png(contenu: bytes) -> bytes:
    """Convertit réellement le fichier récupéré en PNG, en conservant l'alpha s'il existe."""
    if not contenu:
        return contenu

    try:
        with Image.open(io.BytesIO(contenu)) as im:
            # RGBA conserve une éventuelle transparence générée par ChatGPT.
            rgba = im.convert("RGBA")
            sortie = io.BytesIO()
            rgba.save(sortie, format="PNG", optimize=True)
            return sortie.getvalue()
    except Exception as e:
        print(f"⚠️ Conversion PNG impossible, fichier original conservé : {e}")
        return contenu


def _creer_fichier_temporaire_image(image_bytes: bytes) -> str:
    """Crée un PNG temporaire avec un nom unique pour éviter les doublons UI."""
    contenu = convertir_bytes_en_png(image_bytes)
    # Le snap Chromium voit de façon fiable ce dossier (contrairement à /tmp,
    # qui peut être isolé par le confinement snap).
    dossier = UPLOAD_DIR
    os.makedirs(dossier, exist_ok=True)
    chemin = os.path.join(dossier, f"nathgpt_{uuid.uuid4().hex}.png")
    with open(chemin, "wb") as f:
        f.write(contenu)
    return os.path.abspath(chemin)


def _inputs_fichier_chatgpt(driver):
    try:
        return driver.find_elements(By.CSS_SELECTOR, "input[type='file']")
    except Exception:
        return []


def _ouvrir_menu_piece_jointe_chatgpt(driver):
    selecteurs = [
        "button[aria-label*='Ajouter']", "button[aria-label*='ajouter']",
        "button[aria-label*='Joindre']", "button[aria-label*='joindre']",
        "button[aria-label*='Attach']", "button[aria-label*='attach']",
        "button[aria-label*='Upload']", "button[aria-label*='upload']",
        "button[data-testid*='attach']", "button[data-testid*='upload']",
    ]

    for selector in selecteurs:
        try:
            for bouton in driver.find_elements(By.CSS_SELECTOR, selector):
                if not bouton.is_displayed():
                    continue
                try:
                    bouton.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", bouton)
                time.sleep(0.5)
                return True
        except Exception:
            pass

    try:
        zone = trouver_zone_question(driver, timeout=2)
        if zone is not None:
            bouton = driver.execute_script(
                """
                const zone = arguments[0];
                const root = zone.closest('form') || zone.parentElement?.parentElement?.parentElement;
                if (!root) return null;
                return Array.from(root.querySelectorAll('button')).find(b => {
                    const txt = (b.innerText || '').trim();
                    const aria = (b.getAttribute('aria-label') || '').toLowerCase();
                    return txt === '+' || aria.includes('add') || aria.includes('ajout') ||
                           aria.includes('attach') || aria.includes('joindre');
                }) || null;
                """,
                zone,
            )
            if bouton is not None:
                try:
                    bouton.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", bouton)
                time.sleep(0.5)
                return True
    except Exception:
        pass

    return False


def _dialogue_fichier_duplique(driver):
    """Retourne le dialogue 'fichier déjà chargé' s'il est visible."""
    try:
        dialogues = driver.find_elements(By.CSS_SELECTOR, "[role='dialog']")
    except Exception:
        dialogues = []

    mots = (
        "déjà chargé", "deja charge", "already uploaded", "already loaded",
        "essayez de charger un nouveau fichier", "try uploading a new file",
    )
    for dialog in reversed(dialogues):
        try:
            if not dialog.is_displayed():
                continue
            texte = normaliser(dialog.text or "")
            if any(normaliser(m) in texte for m in mots):
                return dialog
        except Exception:
            continue
    return None


def fermer_dialogue_fichier_duplique(driver) -> bool:
    dialog = _dialogue_fichier_duplique(driver)
    if dialog is None:
        return False

    print("⚠️ Dialogue ChatGPT 'fichier déjà chargé' détecté.")
    try:
        boutons = dialog.find_elements(By.TAG_NAME, "button")
    except Exception:
        boutons = []

    # Cherche d'abord OK / Fermer, puis clique le premier bouton utilisable.
    for prioritaire in (True, False):
        for bouton in boutons:
            try:
                if not bouton.is_displayed() or not bouton.is_enabled():
                    continue
                txt = normaliser((bouton.text or "") + " " + (bouton.get_attribute("aria-label") or ""))
                est_ok = txt in ("ok", "fermer", "close") or "ok" == txt.strip()
                if prioritaire != est_ok:
                    continue
                try:
                    bouton.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", bouton)
                time.sleep(0.5)
                print("✅ Dialogue de doublon fermé.")
                return True
            except Exception:
                continue

    try:
        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
        time.sleep(0.3)
        return True
    except Exception:
        return False


def _piece_jointe_presente_chatgpt(driver) -> bool:
    """Détecte une vignette/pièce jointe dans le compositeur ChatGPT."""
    zone = trouver_zone_question(driver, timeout=2)
    if zone is None:
        return False

    try:
        return bool(driver.execute_script(
            """
            const zone = arguments[0];
            const root = zone.closest('form') || zone.parentElement?.parentElement?.parentElement;
            if (!root) return false;

            const buttons = Array.from(root.querySelectorAll('button'));
            const hasRemove = buttons.some(b => {
                const t = ((b.getAttribute('aria-label') || '') + ' ' +
                           (b.getAttribute('title') || '') + ' ' +
                           (b.innerText || '')).toLowerCase();
                return t.includes('remove') || t.includes('supprimer') || t.includes('retirer');
            });

            const imgs = Array.from(root.querySelectorAll('img')).filter(img => {
                const r = img.getBoundingClientRect();
                return r.width >= 32 && r.height >= 32;
            });

            const attachmentNodes = root.querySelectorAll(
                '[data-testid*="attachment"], [data-testid*="file"], [class*="attachment"]'
            );

            return hasRemove || imgs.length > 0 || attachmentNodes.length > 0;
            """,
            zone,
        ))
    except Exception:
        return False


def _supprimer_pieces_jointes_compositeur(driver):
    """Nettoie les pièces jointes restées dans un nouveau chat après un échec."""
    zone = trouver_zone_question(driver, timeout=2)
    if zone is None:
        return

    try:
        boutons = driver.execute_script(
            """
            const zone = arguments[0];
            const root = zone.closest('form') || zone.parentElement?.parentElement?.parentElement;
            if (!root) return [];
            return Array.from(root.querySelectorAll('button')).filter(b => {
                const t = ((b.getAttribute('aria-label') || '') + ' ' +
                           (b.getAttribute('title') || '')).toLowerCase();
                return t.includes('remove') || t.includes('supprimer') || t.includes('retirer');
            });
            """,
            zone,
        ) or []
    except Exception:
        boutons = []

    for bouton in reversed(boutons):
        try:
            if bouton.is_displayed():
                driver.execute_script("arguments[0].click();", bouton)
                time.sleep(0.25)
        except Exception:
            pass

    # Remet aussi à zéro les inputs fichier qui peuvent garder une ancienne FileList.
    try:
        driver.execute_script(
            "document.querySelectorAll('input[type=file]').forEach(i => { try { i.value=''; } catch(e){} });"
        )
    except Exception:
        pass


def televerser_image_chatgpt(driver, image_bytes: bytes) -> str:
    """
    V11 : téléverse UNE SEULE FOIS l'image source.

    Ancien bug : React pouvait vider input.files après un upload réussi. Le script
    pensait alors que l'upload avait échoué et envoyait le même fichier dans un
    deuxième input, ce qui déclenchait « Vous avez déjà chargé ce fichier ».
    """
    if not image_bytes:
        raise RuntimeError("Impossible de modifier l'image : l'image source est vide.")

    chemin = _creer_fichier_temporaire_image(image_bytes)
    print(f"📎 Image source préparée : {chemin}")

    try:
        fermer_dialogue_fichier_duplique(driver)

        zone = trouver_zone_question(driver, timeout=20)
        if zone is None:
            raise RuntimeError("Zone de saisie ChatGPT introuvable avant l'envoi de l'image.")

        afficher_chromium(driver)

        # Si un précédent essai a laissé une pièce jointe dans le compositeur,
        # on la retire avant de commencer ce nouvel upload.
        if _piece_jointe_presente_chatgpt(driver):
            print("🧹 Ancienne pièce jointe détectée dans le compositeur : nettoyage...")
            _supprimer_pieces_jointes_compositeur(driver)
            time.sleep(0.7)

        inputs = _inputs_fichier_chatgpt(driver)
        if not inputs:
            _ouvrir_menu_piece_jointe_chatgpt(driver)
            time.sleep(0.8)
            inputs = _inputs_fichier_chatgpt(driver)

        if not inputs:
            raise RuntimeError("Aucun input de téléversement ChatGPT n'a été trouvé.")

        # Classe les inputs : image/* d'abord, génériques ensuite.
        compatibles = []
        for input_file in inputs:
            try:
                accept = (input_file.get_attribute("accept") or "").lower()
            except Exception:
                accept = ""
            score = 0
            if "image" in accept:
                score += 10
            if "*" in accept or not accept:
                score += 2
            compatibles.append((score, input_file))
        compatibles.sort(key=lambda x: x[0], reverse=True)

        erreur_send = None
        upload_declenche = False

        for _score, input_file in compatibles:
            try:
                try:
                    driver.execute_script(
                        """
                        arguments[0].style.display='block';
                        arguments[0].style.visibility='visible';
                        arguments[0].style.opacity='1';
                        arguments[0].removeAttribute('hidden');
                        """,
                        input_file,
                    )
                except Exception:
                    pass

                # IMPORTANT : dès qu'un send_keys() réussit, on ne touche PLUS
                # aucun autre input fichier de la page.
                input_file.send_keys(chemin)
                upload_declenche = True
                print("📤 Upload envoyé une seule fois à ChatGPT.")
                break
            except Exception as e:
                erreur_send = e
                continue

        if not upload_declenche:
            raise RuntimeError(f"Impossible d'envoyer le fichier à ChatGPT : {erreur_send}")

        # Attend la vignette. Si ChatGPT ouvre malgré tout le dialogue doublon,
        # on le ferme et on CONSERVE la première pièce jointe déjà présente.
        fin = time.time() + 18
        while time.time() < fin:
            duplicate = _dialogue_fichier_duplique(driver) is not None
            if duplicate:
                fermer_dialogue_fichier_duplique(driver)
                time.sleep(0.4)

            if _piece_jointe_presente_chatgpt(driver):
                print("✅ Image source présente dans le compositeur ChatGPT.")
                time.sleep(0.8)
                return chemin

            time.sleep(0.35)

        raise RuntimeError(
            "L'upload a été déclenché, mais ChatGPT n'affiche aucune pièce jointe dans le compositeur."
        )

    except Exception:
        try:
            os.remove(chemin)
        except Exception:
            pass
        raise



def _nom_fichier_sur(nom: str) -> str:
    nom = os.path.basename(nom or "fichier")
    nom = re.sub(r"[^A-Za-z0-9._ -]+", "_", nom).strip(" .")
    if not nom:
        nom = "fichier"
    return nom[:120]


def _creer_fichier_temporaire_piece_jointe(contenu: bytes, nom: str) -> str:
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    nom_sur = _nom_fichier_sur(nom)
    chemin = os.path.join(UPLOAD_DIR, f"discord_{uuid.uuid4().hex}_{nom_sur}")
    with open(chemin, "wb") as f:
        f.write(contenu)
    return os.path.abspath(chemin)


def _input_fichier_score(input_file, pieces) -> int:
    try:
        accept = (input_file.get_attribute("accept") or "").lower()
    except Exception:
        accept = ""

    if not accept:
        return 50
    if "*/*" in accept or "*" == accept.strip():
        return 45

    score = 0
    for piece in pieces:
        ctype = (piece.get("content_type") or "").lower()
        nom = (piece.get("filename") or "").lower()
        ext = os.path.splitext(nom)[1].lstrip(".")
        if ctype and ctype in accept:
            score += 10
        elif ctype.startswith("image/") and "image" in accept:
            score += 8
        elif ext and ext in accept:
            score += 4
    return score


def televerser_pieces_jointes_chatgpt(driver, pieces, cancel_event=None):
    """Téléverse les pièces jointes Discord dans le compositeur ChatGPT."""
    if not pieces:
        return []

    verifier_annulation(cancel_event)
    chemins = []
    for piece in pieces:
        verifier_annulation(cancel_event)
        contenu = piece.get("bytes") or b""
        if not contenu:
            continue
        chemins.append(
            _creer_fichier_temporaire_piece_jointe(
                contenu,
                piece.get("filename") or "fichier",
            )
        )

    if not chemins:
        return []

    try:
        fermer_dialogue_fichier_duplique(driver)
        zone = trouver_zone_question(driver, timeout=20)
        if zone is None:
            raise RuntimeError("Zone de saisie ChatGPT introuvable avant l'envoi des pieces jointes.")

        afficher_chromium(driver)
        if _piece_jointe_presente_chatgpt(driver):
            _supprimer_pieces_jointes_compositeur(driver)
            time.sleep(0.6)

        inputs = _inputs_fichier_chatgpt(driver)
        if not inputs:
            _ouvrir_menu_piece_jointe_chatgpt(driver)
            time.sleep(0.8)
            inputs = _inputs_fichier_chatgpt(driver)
        if not inputs:
            raise RuntimeError("Aucun input de televersement ChatGPT n'a ete trouve.")

        classes = sorted(
            ((_input_fichier_score(inp, pieces), inp) for inp in inputs),
            key=lambda x: x[0],
            reverse=True,
        )
        input_file = classes[0][1]
        try:
            driver.execute_script(
                "arguments[0].style.display='block'; arguments[0].style.visibility='visible'; arguments[0].removeAttribute('hidden');",
                input_file,
            )
        except Exception:
            pass

        # Selenium accepte plusieurs chemins séparés par un saut de ligne sur les input multiple.
        try:
            input_file.send_keys("\n".join(chemins))
        except Exception:
            # Fallback : envoi un par un en retrouvant l'input à chaque fois.
            for chemin in chemins:
                verifier_annulation(cancel_event)
                inputs2 = _inputs_fichier_chatgpt(driver)
                if not inputs2:
                    _ouvrir_menu_piece_jointe_chatgpt(driver)
                    time.sleep(0.5)
                    inputs2 = _inputs_fichier_chatgpt(driver)
                if not inputs2:
                    raise RuntimeError("Input de piece jointe perdu pendant l'upload.")
                classes2 = sorted(
                    ((_input_fichier_score(inp, pieces), inp) for inp in inputs2),
                    key=lambda x: x[0],
                    reverse=True,
                )
                classes2[0][1].send_keys(chemin)
                time.sleep(0.8)

        fin = time.time() + 30
        while time.time() < fin:
            verifier_annulation(cancel_event)
            if _dialogue_fichier_duplique(driver) is not None:
                fermer_dialogue_fichier_duplique(driver)
            if _piece_jointe_presente_chatgpt(driver):
                print(f"Pieces jointes Discord chargees : {len(chemins)}")
                time.sleep(1.5)
                return chemins
            time.sleep(0.4)

        raise RuntimeError("ChatGPT n'affiche pas les pieces jointes dans le compositeur.")
    except Exception:
        for chemin in chemins:
            _supprimer_fichier_temporaire(chemin)
        raise


def supprimer_fichiers_temporaires(chemins):
    for chemin in chemins or []:
        _supprimer_fichier_temporaire(chemin)

def _supprimer_fichier_temporaire(chemin: str | None):
    if not chemin:
        return
    try:
        if os.path.isfile(chemin):
            os.remove(chemin)
    except Exception as e:
        print(f"⚠️ Impossible de supprimer le fichier temporaire {chemin!r} : {e}")

def prendre_etat_images_page(driver):
    """
    Snapshot AVANT une génération/édition.

    V6 : en plus des URL/compteurs, on mémorise l'empreinte PIXEL de toutes
    les images déjà présentes. Ainsi une modification ne peut plus renvoyer
    l'image précédente simplement parce qu'elle est encore visible dans le DOM.
    """
    comptes = {}
    empreintes = set()

    for img in images_valides_page(driver):
        sig = _signature_image(driver, img)
        comptes[sig] = comptes.get(sig, 0) + 1

        try:
            contenu = extraire_image_bytes(driver, img)
            if contenu and len(contenu) > 10_000:
                emp = _empreinte_contenu_image(contenu)
                if emp:
                    empreintes.add(emp)
        except Exception:
            pass

    return {
        "comptes": comptes,
        "modifier_count": compter_boutons_modifier_image(driver),
        "empreintes": empreintes,
        "total_images": sum(comptes.values()),
    }


def _extraire_nouvelle_image(driver, img, etat_avant):
    """Retourne les bytes uniquement si l'image n'existait PAS avant le prompt."""
    try:
        contenu = extraire_image_bytes(driver, img)
    except Exception:
        return None

    if not contenu or len(contenu) <= 10_000:
        return None

    empreinte = _empreinte_contenu_image(contenu)
    anciennes = set((etat_avant or {}).get("empreintes", set()) or set())

    if empreinte and empreinte in anciennes:
        # C'est exactement une image déjà affichée avant l'édition.
        return None

    return contenu

def _nouvelles_images_depuis_etat(driver, etat_avant):
    avant = (etat_avant or {}).get("comptes", {})
    vus = {}
    nouvelles = []

    for img in images_valides_page(driver):
        sig = _signature_image(driver, img)
        vus[sig] = vus.get(sig, 0) + 1
        if vus[sig] > avant.get(sig, 0):
            nouvelles.append(img)

    return nouvelles


def _image_pres_bouton_modifier(driver):
    """
    Essaie de récupérer l'image du dernier bloc possédant un bouton
    « Modifier ». C'est un excellent signal que ChatGPT a fini l'image.
    """
    try:
        boutons = driver.find_elements(By.TAG_NAME, "button")
    except Exception:
        return None

    for bouton in reversed(boutons):
        try:
            if not bouton.is_displayed():
                continue

            texte = (bouton.text or "").strip().lower()
            aria = (bouton.get_attribute("aria-label") or "").strip().lower()
            cible = f"{texte} {aria}"

            if not ("modifier" in cible or "edit image" in cible or cible.strip() == "edit"):
                continue

            img = driver.execute_script(
                """
                let node = arguments[0];
                for (let i = 0; i < 9 && node; i++, node = node.parentElement) {
                    const imgs = Array.from(node.querySelectorAll('img')).filter(img => {
                        const r = img.getBoundingClientRect();
                        const w = img.naturalWidth || img.clientWidth || 0;
                        const h = img.naturalHeight || img.clientHeight || 0;
                        return r.width > 1 && r.height > 1 && w >= 256 && h >= 256;
                    });
                    if (imgs.length) {
                        imgs.sort((a, b) => {
                            const aa = (a.naturalWidth || a.clientWidth || 0) * (a.naturalHeight || a.clientHeight || 0);
                            const bb = (b.naturalWidth || b.clientWidth || 0) * (b.naturalHeight || b.clientHeight || 0);
                            return bb - aa;
                        });
                        return imgs[0];
                    }
                }
                return null;
                """,
                bouton,
            )

            if img is not None:
                return img
        except Exception:
            continue

    return None

def _bytes_sont_image_exploitable(contenu: bytes) -> bool:
    if not contenu or len(contenu) <= 10_000:
        return False
    try:
        with Image.open(io.BytesIO(contenu)) as im:
            largeur, hauteur = im.size
            im.verify()
        return largeur >= 256 and hauteur >= 256
    except Exception:
        return False


def extraire_image_bytes(driver, image_element):
    """
    Récupère uniquement une vraie balise <img> finale.

    Les canvases sont refusés : ChatGPT les utilise notamment pour son animation
    de génération. Cela empêche d'envoyer à Discord l'écran blanc avec les points.
    """
    infos = _infos_image(driver, image_element)
    if infos.get("tag") != "img":
        raise RuntimeError("Élément ignoré : ce n'est pas l'image finale ChatGPT.")
    if not infos.get("complete"):
        raise RuntimeError("Image ChatGPT encore en cours de chargement.")

    src = (infos.get("src") or "").strip()
    if not _src_image_est_exploitable(src):
        raise RuntimeError("Image ChatGPT temporaire/placeholder ignorée.")

    try:
        script = r"""
            const url = arguments[0];
            const done = arguments[arguments.length - 1];

            fetch(url, {credentials: 'include'})
                .then(r => {
                    if (!r.ok) throw new Error('HTTP ' + r.status);
                    return r.blob();
                })
                .then(blob => {
                    const reader = new FileReader();
                    reader.onloadend = () => done(reader.result);
                    reader.onerror = () => done(null);
                    reader.readAsDataURL(blob);
                })
                .catch(() => done(null));
        """

        data_url = driver.execute_async_script(script, src)
        if isinstance(data_url, str) and data_url.startswith("data:"):
            _, b64 = data_url.split(",", 1)
            contenu = base64.b64decode(b64)
            if _bytes_sont_image_exploitable(contenu):
                return contenu
    except Exception:
        pass

    # Fallback : seulement maintenant que l'élément est confirmé comme un vrai
    # <img> final et chargé. On ne capture donc plus le canvas de progression.
    try:
        contenu = image_element.screenshot_as_png
        if _bytes_sont_image_exploitable(contenu):
            return contenu
    except Exception:
        pass

    raise RuntimeError("Impossible de récupérer le fichier de l'image finale ChatGPT.")


def afficher_chromium(driver):
    """Affiche la fenêtre Chromium contrôlé par Selenium pour faciliter le diagnostic."""
    if not AFFICHER_CHROMIUM_PENDANT_GENERATION:
        return

    try:
        driver.switch_to.window(driver.current_window_handle)
    except Exception:
        pass

    try:
        driver.maximize_window()
    except Exception:
        pass

    try:
        driver.execute_script("window.focus();")
    except Exception:
        pass


def _attendre_page_chatgpt(driver, timeout=25):
    fin = time.time() + timeout
    while time.time() < fin:
        try:
            if driver.execute_script("return document.readyState") in ("interactive", "complete"):
                zone = trouver_zone_question(driver, timeout=1.5)
                if zone is not None:
                    return zone
        except Exception:
            pass
        time.sleep(0.35)
    return None


def preparer_chatgpt(driver, nouveau_chat=False):
    """Prépare un onglet ChatGPT et nettoie un nouveau compositeur si demandé."""
    print("🌐 Préparation de l'onglet ChatGPT...")

    cible = None
    try:
        for handle in driver.window_handles:
            try:
                driver.switch_to.window(handle)
                if "chatgpt.com" in (driver.current_url or ""):
                    cible = handle
                    break
            except Exception:
                continue
    except Exception:
        pass

    if cible is None:
        print("➕ Aucun onglet ChatGPT trouvé : ouverture d'un nouvel onglet...")
        try:
            driver.switch_to.new_window("tab")
        except Exception:
            pass
        driver.get(CHATGPT_URL)
    else:
        driver.switch_to.window(cible)

    afficher_chromium(driver)

    try:
        url_actuelle = driver.current_url or ""
    except Exception:
        url_actuelle = ""

    if nouveau_chat or "chatgpt.com" not in url_actuelle:
        print("🆕 Ouverture d'une nouvelle conversation ChatGPT...")
        # Un petit query param aléatoire force une navigation fraîche sans changer
        # le fonctionnement de la page et limite les états de compositeur réutilisés.
        driver.get(CHATGPT_URL + f"?nathgpt={uuid.uuid4().hex[:10]}")

    zone = _attendre_page_chatgpt(driver, timeout=30)
    if zone is None:
        raise RuntimeError(
            "ChatGPT est ouvert mais la zone de saisie est introuvable. "
            "Vérifie que tu es connecté dans le profil Chromium persistant Oracle."
        )

    # Un échec précédent peut laisser le popup de doublon ouvert.
    fermer_dialogue_fichier_duplique(driver)

    if nouveau_chat:
        # Nettoyage défensif : texte/attachment résiduel d'un essai précédent.
        try:
            zone = trouver_zone_question(driver, timeout=3)
            if zone is not None:
                zone.click()
                zone.send_keys(Keys.CONTROL, "a")
                zone.send_keys(Keys.BACKSPACE)
        except Exception:
            pass
        _supprimer_pieces_jointes_compositeur(driver)
        time.sleep(0.4)

    print(f"✅ ChatGPT prêt : {driver.current_url}")
    return trouver_zone_question(driver, timeout=5)

def recuperer_messages_utilisateur(driver):
    selecteurs = [
        "[data-message-author-role='user']",
        "article[data-turn='user']",
    ]

    for selector in selecteurs:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
            if elements:
                return elements
        except Exception:
            pass

    return []


def _lire_contenu_zone(driver, zone):
    """Lit le contenu d'une zone de saisie, même si ChatGPT utilise contenteditable."""
    try:
        valeur = driver.execute_script(
            """
            const el = arguments[0];
            if (!el) return '';
            if (typeof el.value === 'string' && el.value.trim()) return el.value.trim();
            if (el.innerText && el.innerText.trim()) return el.innerText.trim();
            if (el.textContent && el.textContent.trim()) return el.textContent.trim();
            const editable = el.querySelector && el.querySelector('[contenteditable="true"]');
            if (editable) {
                return (editable.innerText || editable.textContent || '').trim();
            }
            return '';
            """,
            zone,
        )
        return valeur or ""
    except Exception:
        return ""


def _relire_zone_fraiche(driver):
    """Retrouve la zone après un éventuel re-render React de ChatGPT."""
    try:
        zone = trouver_zone_question(driver, timeout=3)
        if zone is None:
            return None, ""
        return zone, _lire_contenu_zone(driver, zone)
    except Exception:
        return None, ""


def _trouver_bouton_envoyer(driver):
    selecteurs = [
        "button[data-testid='send-button']",
        "button[aria-label*='Send']",
        "button[aria-label*='Envoyer']",
        "button[aria-label*='send']",
        "button[aria-label*='envoyer']",
    ]

    for selector in selecteurs:
        try:
            boutons = driver.find_elements(By.CSS_SELECTOR, selector)
            for bouton in boutons:
                if bouton.is_displayed() and bouton.is_enabled():
                    return bouton
        except Exception:
            pass

    return None


def _cliquer_bouton_envoyer(driver):
    bouton = _trouver_bouton_envoyer(driver)
    if bouton is None:
        return False

    try:
        bouton.click()
    except Exception:
        try:
            driver.execute_script("arguments[0].click();", bouton)
        except Exception:
            return False

    return True


def _ecrire_avec_presse_papiers(driver, prompt):
    zone = trouver_zone_question(driver, timeout=5)
    if zone is None:
        return False

    ancien = ""
    try:
        ancien = pyperclip.paste()
    except Exception:
        pass

    try:
        zone.click()
        try:
            zone.send_keys(Keys.CONTROL, "a")
            zone.send_keys(Keys.BACKSPACE)
        except Exception:
            pass

        pyperclip.copy(prompt)
        zone.send_keys(Keys.CONTROL, "v")
        time.sleep(0.7)
        return True
    except Exception as e:
        print(f"⚠️ Collage presse-papiers impossible : {e}")
        return False
    finally:
        try:
            pyperclip.copy(ancien)
        except Exception:
            pass


def _texte_normalise_comparaison(texte):
    """Normalise les espaces pour comparer le texte du compositeur avec le prompt attendu."""
    if not texte:
        return ""
    return " ".join(str(texte).replace("\r", "\n").split())


def _ecrire_multiligne_sans_envoyer(driver, prompt):
    """
    Fallback sans presse-papiers.
    IMPORTANT : on n'envoie JAMAIS un '\\n' brut à send_keys(), car sur certains
    contenteditable il peut être interprété comme Entrée et envoyer le message.
    Les retours à la ligne sont donc faits avec Shift+Entrée.
    """
    zone = trouver_zone_question(driver, timeout=5)
    if zone is None:
        return False

    try:
        zone.click()
        zone.send_keys(Keys.CONTROL, "a")
        zone.send_keys(Keys.BACKSPACE)
        time.sleep(0.2)

        lignes = prompt.replace("\r\n", "\n").replace("\r", "\n").split("\n")

        for index, ligne in enumerate(lignes):
            if ligne:
                zone.send_keys(ligne)

            if index < len(lignes) - 1:
                ActionChains(driver) \
                    .key_down(Keys.SHIFT) \
                    .send_keys(Keys.ENTER) \
                    .key_up(Keys.SHIFT) \
                    .perform()

        return True
    except Exception as e:
        print(f"⚠️ Fallback Shift+Entrée impossible : {e}")
        return False


def _ecrire_avec_javascript(driver, prompt):
    """Dernier fallback : remplit le contenteditable sans simuler la touche Entrée."""
    zone = trouver_zone_question(driver, timeout=5)
    if zone is None:
        return False

    try:
        return bool(driver.execute_script(
            """
            const el = arguments[0];
            const text = arguments[1];
            const target = el.matches && el.matches('[contenteditable="true"]')
                ? el
                : (el.querySelector && el.querySelector('[contenteditable="true"]')) || el;

            target.focus();

            if ('value' in target && target.tagName === 'TEXTAREA') {
                const setter = Object.getOwnPropertyDescriptor(
                    HTMLTextAreaElement.prototype, 'value'
                ).set;
                setter.call(target, text);
                target.dispatchEvent(new Event('input', {bubbles:true}));
                target.dispatchEvent(new Event('change', {bubbles:true}));
                return true;
            }

            if (target.isContentEditable) {
                const sel = window.getSelection();
                const range = document.createRange();
                range.selectNodeContents(target);
                sel.removeAllRanges();
                sel.addRange(range);
                document.execCommand('delete', false, null);
                document.execCommand('insertText', false, text);
                target.dispatchEvent(new InputEvent('input', {
                    bubbles:true,
                    inputType:'insertText',
                    data:text
                }));
                return true;
            }
            return false;
            """,
            zone,
            prompt,
        ))
    except Exception as e:
        print(f"⚠️ Injection JavaScript impossible : {e}")
        return False


def _prompt_est_complet(prompt, contenu):
    attendu = _texte_normalise_comparaison(prompt)
    recu = _texte_normalise_comparaison(contenu)
    if not attendu or not recu:
        return False
    if recu == attendu:
        return True
    # Vérification robuste : début + fin présents, utile quand l'éditeur ajoute
    # ses propres espaces/retours à la ligne.
    debut = attendu[: min(70, len(attendu))]
    fin = attendu[-min(90, len(attendu)):]
    return debut in recu and fin in recu


def envoyer_prompt(driver, prompt):
    print("📝 Recherche de la zone de saisie...")

    prompt = str(prompt).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not prompt:
        raise RuntimeError("Le prompt à envoyer est vide.")

    # V11 : le popup 'fichier déjà chargé' bloque physiquement le compositeur.
    # On le ferme AVANT chaque tentative d'écriture.
    fermer_dialogue_fichier_duplique(driver)

    zone = trouver_zone_question(driver, timeout=20)
    if zone is None:
        raise RuntimeError(
            "Impossible de trouver la zone de saisie ChatGPT. "
            "Vérifie que tu es connecté dans le profil Chromium persistant Oracle."
        )

    nombre_avant = len(recuperer_messages_assistant(driver))
    utilisateurs_avant = len(recuperer_messages_utilisateur(driver))
    afficher_chromium(driver)

    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block:'center', inline:'nearest'});", zone
        )
    except Exception:
        pass

    print(f"⌨️ Écriture du prompt ({len(prompt)} caractères)...")

    methodes = [
        ("presse-papiers", _ecrire_avec_presse_papiers),
        ("Shift+Entrée", _ecrire_multiligne_sans_envoyer),
        ("JavaScript", _ecrire_avec_javascript),
    ]

    texte_complet = False
    contenu = ""

    for nom, methode in methodes:
        fermer_dialogue_fichier_duplique(driver)
        try:
            ok = methode(driver, prompt)
        except Exception as e:
            ok = False
            print(f"⚠️ Méthode {nom} : {e}")

        if not ok:
            continue

        time.sleep(0.7)
        fermer_dialogue_fichier_duplique(driver)
        _zone_fraiche, contenu = _relire_zone_fraiche(driver)
        texte_complet = _prompt_est_complet(prompt, contenu)

        if texte_complet:
            print(f"✅ Prompt complet détecté avec la méthode {nom}.")
            break

        print(f"⚠️ Vérification du prompt échouée après {nom}, tentative suivante...")

    if not texte_complet:
        _, contenu = _relire_zone_fraiche(driver)
        raise RuntimeError(
            "Selenium n'a pas réussi à écrire le prompt complet dans ChatGPT. "
            f"Texte détecté : {_texte_normalise_comparaison(contenu)[:180]!r}"
        )

    fermer_dialogue_fichier_duplique(driver)

    bouton = _trouver_bouton_envoyer(driver)
    if bouton is None:
        # Une pièce jointe peut encore terminer son traitement : attend quelques secondes.
        fin_bouton = time.time() + 12
        while time.time() < fin_bouton and bouton is None:
            fermer_dialogue_fichier_duplique(driver)
            bouton = _trouver_bouton_envoyer(driver)
            time.sleep(0.35)

    if bouton is None:
        raise RuntimeError(
            "Le prompt est écrit, mais le bouton Envoyer n'est pas disponible. "
            "La pièce jointe est peut-être encore en cours de chargement."
        )

    if not _cliquer_bouton_envoyer(driver):
        raise RuntimeError("Impossible de cliquer sur le bouton Envoyer de ChatGPT.")

    print("🚀 Prompt envoyé à ChatGPT.")

    attendu = _texte_normalise_comparaison(prompt)
    fin_verification = time.time() + 18
    while time.time() < fin_verification:
        try:
            utilisateurs = recuperer_messages_utilisateur(driver)
            if len(utilisateurs) > utilisateurs_avant:
                dernier = lire_texte_element(utilisateurs[-1])
                dernier_norm = _texte_normalise_comparaison(dernier)
                if not dernier_norm or attendu[-60:] in dernier_norm or dernier_norm == attendu:
                    print("✅ Prompt réellement envoyé à ChatGPT.")
                    return nombre_avant
        except Exception:
            pass

        if chatgpt_ecrit_encore(driver):
            print("✅ ChatGPT a commencé à travailler.")
            return nombre_avant

        time.sleep(0.4)

    # Si le compositeur s'est vidé, le clic a normalement été accepté même si
    # le DOM des messages utilisateur a changé.
    _, restant = _relire_zone_fraiche(driver)
    if not _texte_normalise_comparaison(restant):
        print("✅ Compositeur vidé : prompt considéré comme envoyé.")
        return nombre_avant

    raise RuntimeError(
        "Le bouton Envoyer a été cliqué, mais ChatGPT ne semble pas avoir démarré."
    )

class GenerationCancelledError(RuntimeError):
    pass


class AutoCancelError(TimeoutError):
    pass


def verifier_annulation(cancel_event=None):
    if cancel_event is not None and cancel_event.is_set():
        raise GenerationCancelledError("generation annulee")


def attendre_reponse_texte(driver, nombre_avant, timeout=TEXT_TIMEOUT, cancel_event=None):
    debut = time.time()
    dernier_texte = ""
    derniere_modification = time.time()
    nouvelle_reponse = False

    while True:
        verifier_annulation(cancel_event)
        messages = recuperer_messages_assistant(driver)

        if len(messages) > nombre_avant:
            nouvelle_reponse = True
            texte = lire_texte_element(messages[-1])

            if texte != dernier_texte:
                dernier_texte = texte
                derniere_modification = time.time()
                print(f"\r✍️ Réception texte : {len(texte)} caractères", end="", flush=True)

            if (
                nouvelle_reponse
                and dernier_texte
                and not chatgpt_ecrit_encore(driver)
                and time.time() - derniere_modification >= 2.5
            ):
                print()
                return dernier_texte

        if time.time() - debut > timeout:
            print()
            if dernier_texte:
                return dernier_texte
            raise TimeoutError("ChatGPT n'a pas répondu dans le délai prévu.")

        time.sleep(0.5)


def _cliquer_reessayer_image(driver):
    """Clique une fois sur le bouton Réessayer/Retry si ChatGPT affiche une erreur temporaire."""
    try:
        boutons = driver.find_elements(By.TAG_NAME, "button")
    except Exception:
        return False

    for bouton in boutons:
        try:
            if not bouton.is_displayed() or not bouton.is_enabled():
                continue

            texte = (bouton.text or "").strip().lower()
            aria = (bouton.get_attribute("aria-label") or "").strip().lower()
            cible = f"{texte} {aria}"

            if "réessayer" in cible or "reessayer" in cible or "retry" in cible:
                try:
                    bouton.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", bouton)
                print("🔁 ChatGPT a affiché une erreur temporaire : clic sur Réessayer.")
                return True
        except Exception:
            continue

    return False


def attendre_image(driver, nombre_avant, etat_images_avant=None, timeout=IMAGE_TIMEOUT, minimum_total_images=None, minimum_modifier_count=None, cancel_event=None):
    """
    Attend une NOUVELLE image générée par ChatGPT.

    V7 :
    - compare le DOM avec l'état précédent ;
    - compare aussi l'empreinte PIXEL du fichier ;
    - refuse catégoriquement une image qui existait avant le prompt ;
    - détecte également le cas où ChatGPT remplace une image dans le même
      élément DOM au lieu d'ajouter un nouvel <img> ;
    - peut attendre qu'une NOUVELLE image soit ajoutée dans la conversation
      (ex. passer de 1 image à 2 images, puis renvoyer la plus récente) ;
    - pour une régénération/modification, peut attendre un NOUVEAU bouton
      « Modifier », ce qui force la prise du bloc image le plus récent.
    """
    debut = time.time()
    dernier_texte = ""
    tentatives_retry = 0
    signature_candidate = None
    candidate_stable_depuis = None
    log_image_detectee = False

    if etat_images_avant is None:
        etat_images_avant = {
            "comptes": {},
            "modifier_count": 0,
            "empreintes": set(),
        }

    modifier_avant = int(etat_images_avant.get("modifier_count", 0) or 0)
    total_images_avant = int(etat_images_avant.get("total_images", 0) or 0)
    if minimum_total_images is None:
        minimum_total_images = 0
    if minimum_modifier_count is None:
        minimum_modifier_count = 0
    log_attente_nouvelle_image = False
    log_attente_nouveau_bloc = False

    while True:
        verifier_annulation(cancel_event)
        # V6 : IMPORTANT — on ne clique PLUS sur "Réessayer" avant d'avoir
        # cherché une image. ChatGPT peut afficher "Un problème est survenu"
        # tout en ayant quand même terminé et affiché l'image. Dans ce cas,
        # cliquer immédiatement sur Réessayer relançait inutilement la génération
        # et empêchait parfois le bot de récupérer l'image déjà terminée.

        messages = recuperer_messages_assistant(driver)
        if len(messages) > nombre_avant:
            try:
                dernier_texte = lire_texte_element(messages[-1]) or dernier_texte
            except Exception:
                pass

        nouvelles = _nouvelles_images_depuis_etat(driver, etat_images_avant)
        modifier_maintenant = compter_boutons_modifier_image(driver)
        nouveau_bouton_modifier = modifier_maintenant > modifier_avant
        assez_de_blocs_image = modifier_maintenant >= int(minimum_modifier_count or 0)
        image_candidate = None

        # 1) Priorité au dernier bloc qui vient d'obtenir un bouton Modifier.
        if assez_de_blocs_image and nouveau_bouton_modifier:
            pres_modifier = _image_pres_bouton_modifier(driver)
            if pres_modifier is not None:
                image_candidate = pres_modifier

        # 2) Sinon, dernière nouvelle balise image du DOM.
        if image_candidate is None and nouvelles and assez_de_blocs_image:
            image_candidate = nouvelles[-1]

        total_images_maintenant = len(images_valides_page(driver))
        assez_d_images = total_images_maintenant >= int(minimum_total_images or 0)
        if minimum_total_images and not assez_d_images:
            if not log_attente_nouvelle_image:
                print(
                    "⏳ Régénération détectée : attente d'une image supplémentaire dans la conversation "
                    f"({total_images_maintenant}/{minimum_total_images})"
                )
                log_attente_nouvelle_image = True
            image_candidate = None

        if minimum_modifier_count and not assez_de_blocs_image:
            if not log_attente_nouveau_bloc:
                print(
                    "⏳ Attente d'un nouveau bloc image terminé dans ChatGPT "
                    f"({modifier_maintenant}/{minimum_modifier_count} boutons Modifier)"
                )
                log_attente_nouveau_bloc = True
            image_candidate = None

        if image_candidate is not None:
            infos = _infos_image(driver, image_candidate)
            sig = _signature_image(driver, image_candidate)

            if sig != signature_candidate:
                signature_candidate = sig
                candidate_stable_depuis = time.time()
                if not log_image_detectee:
                    print("🖼️ Une image candidate a été détectée...")
                    log_image_detectee = True

            stable_depuis = candidate_stable_depuis or time.time()
            stable = time.time() - stable_depuis >= 1.2
            nw = int(infos.get("nw") or 0)
            nh = int(infos.get("nh") or 0)
            cw = int(infos.get("cw") or 0)
            ch = int(infos.get("ch") or 0)
            # Certaines versions de l'interface ChatGPT affichent déjà l'image
            # à sa taille finale alors que naturalWidth/naturalHeight ou
            # img.complete ne sont pas encore fiables. La taille réellement
            # affichée suffit alors pour tenter l'extraction/screenshot.
            chargee = (
                infos.get("tag") == "img"
                and bool(infos.get("complete"))
                and nw >= 256
                and nh >= 256
                and cw >= 220
                and ch >= 220
                and _src_image_est_exploitable(infos.get("src") or "")
            )
            fini_ecriture = not chatgpt_ecrit_encore(driver)

            # Fallback anti-blocage : l'interface peut laisser le bouton Stop
            # visible trop longtemps. Si une NOUVELLE image est chargée et reste
            # stable plusieurs secondes, on la récupère même si ce signal UI est faux.
            stable_longue = time.time() - stable_depuis >= 6.0
            if chargee and (
                nouveau_bouton_modifier
                or (stable and fini_ecriture)
                or stable_longue
            ):
                try:
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center', inline:'center'});",
                        image_candidate,
                    )
                    time.sleep(0.25)
                except Exception:
                    pass

                contenu = _extraire_nouvelle_image(
                    driver, image_candidate, etat_images_avant
                )

                if contenu:
                    print(f"✅ NOUVELLE image récupérée : {len(contenu):,} octets")
                    return dernier_texte, contenu
                else:
                    # Important : on a détecté une image, mais elle est identique
                    # à une image d'avant. On NE la renvoie surtout pas à Discord.
                    print("⏭️ Image candidate ignorée : c'est une ancienne image.")

        # 3) Très important pour l'édition : ChatGPT peut REMPLACER le contenu
        # d'une image existante au lieu de créer une nouvelle balise/URL.
        # Une fois la génération terminée, on compare donc TOUTES les grandes
        # images actuelles avec les empreintes prises avant le prompt.
        fini_ecriture = not chatgpt_ecrit_encore(driver)
        generation_a_commence = (
            len(messages) > nombre_avant
            or nouveau_bouton_modifier
            or (time.time() - debut) > 4.0
        )

        # Deuxième fallback : après 15 secondes, on compare aussi les pixels
        # même si ChatGPT laisse encore par erreur son état "en cours" visible.
        interface_probablement_bloquee = (time.time() - debut) >= 15.0
        if (fini_ecriture or interface_probablement_bloquee) and generation_a_commence and (not minimum_total_images or len(images_valides_page(driver)) >= int(minimum_total_images)) and (not minimum_modifier_count or compter_boutons_modifier_image(driver) >= int(minimum_modifier_count)):
            images_page = images_valides_page(driver)
            for img in reversed(images_page):
                infos = _infos_image(driver, img)
                nw = int(infos.get("nw") or 0)
                nh = int(infos.get("nh") or 0)
                cw = int(infos.get("cw") or 0)
                ch = int(infos.get("ch") or 0)
                if not (
                    infos.get("tag") == "img"
                    and bool(infos.get("complete"))
                    and nw >= 256
                    and nh >= 256
                    and cw >= 220
                    and ch >= 220
                    and _src_image_est_exploitable(infos.get("src") or "")
                ):
                    continue

                contenu = _extraire_nouvelle_image(driver, img, etat_images_avant)
                if contenu:
                    print(
                        "✅ Nouvelle image détectée par comparaison de pixels "
                        f": {len(contenu):,} octets"
                    )
                    return dernier_texte, contenu

        # V6 : si une erreur temporaire est visible mais qu'AUCUNE nouvelle
        # image exploitable n'a été trouvée ci-dessus, on peut alors seulement
        # tenter "Réessayer". On laisse quelques secondes à l'image pour
        # apparaître avant de relancer.
        if (
            fini_ecriture
            and tentatives_retry < 2
            and (time.time() - debut) >= 5.0
        ):
            if _cliquer_reessayer_image(driver):
                tentatives_retry += 1
                signature_candidate = None
                candidate_stable_depuis = None
                log_image_detectee = False
                time.sleep(2.0)
                continue

        # Erreur/quota éventuel.
        if dernier_texte and fini_ecriture:
            texte_min = dernier_texte.lower()
            mots_limite = [
                "limit", "limite", "try again", "réessayer", "reessayer",
                "unable", "can't", "cannot", "impossible", "quota"
            ]
            if any(m in texte_min for m in mots_limite):
                # Le bouton Réessayer éventuel a déjà été traité juste au-dessus,
                # APRÈS la recherche d'une image. Si on arrive ici sans image,
                # on renvoie proprement l'erreur au lieu de cliquer encore.
                return dernier_texte, None

        if time.time() - debut > timeout:
            # Dernier scan strict : jamais d'ancienne image.
            if minimum_total_images and len(images_valides_page(driver)) < int(minimum_total_images):
                if dernier_texte:
                    return dernier_texte, None
                raise TimeoutError(
                    "La régénération a expiré avant qu'une nouvelle image soit ajoutée à la conversation."
                )
            if minimum_modifier_count and compter_boutons_modifier_image(driver) < int(minimum_modifier_count):
                if dernier_texte:
                    return dernier_texte, None
                raise TimeoutError(
                    "La régénération a expiré avant qu'un nouveau bloc image terminé apparaisse dans la conversation."
                )
            for img in reversed(images_valides_page(driver)):
                contenu = _extraire_nouvelle_image(driver, img, etat_images_avant)
                if contenu:
                    print(
                        "⚠️ Timeout logique, mais une nouvelle image différente "
                        f"a été récupérée : {len(contenu):,} octets"
                    )
                    return dernier_texte, contenu

            if dernier_texte:
                return dernier_texte, None
            raise TimeoutError(
                "ChatGPT a terminé, mais aucune NOUVELLE image différente de l'ancienne n'a été détectée."
            )

        time.sleep(0.6)


# ============================================================
# DÉTECTION DU TYPE DE DEMANDE
# ============================================================


def normaliser(texte: str) -> str:
    table = str.maketrans({
        "é": "e", "è": "e", "ê": "e", "ë": "e",
        "à": "a", "â": "a", "ä": "a",
        "î": "i", "ï": "i",
        "ô": "o", "ö": "o",
        "ù": "u", "û": "u", "ü": "u",
        "ç": "c",
    })

    t = texte.lower().translate(table)
    # Uniformise les apostrophes et espaces sans casser les phrases du type
    # "génère l'image" / "génère l’image".
    t = t.replace("’", "'").replace("`", "'")
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def est_demande_image(texte: str) -> bool:
    """Détecte une demande d'image même avec des formulations naturelles/typos.

    V6 corrige notamment :
      - "genere l'image d'une poule"
      - "génère l’image d'un chat"
      - "fais moi une photo de ..."
      - "crée ce dessin"
    """
    t = normaliser(texte)

    if t.startswith("/image ") or t == "/image":
        return True

    expressions = [
        "genere une image",
        "generer une image",
        "genere l'image",
        "generer l'image",
        "cree une image",
        "creer une image",
        "cree l'image",
        "creer l'image",
        "fais une image",
        "fait une image",
        "fais l'image",
        "fait l'image",
        "dessine ",
        "genere moi une image",
        "cree moi une image",
        "fais moi une image",
        "genere cette image",
        "regenere cette image",
        "recree cette image",
        "cree cette image",
        "genere une photo",
        "cree une photo",
        "fais une photo",
        "genere l'illustration",
        "cree l'illustration",
    ]

    if any(expr in t for expr in expressions):
        return True

    # Fallback volontairement large : s'il y a un verbe de création ET un
    # mot visuel, on traite la demande comme une génération d'image.
    # Cela évite que Discord affiche "Réponse en cours" alors que ChatGPT
    # est réellement en train de générer une image.
    mots_visuels = (
        "image", "photo", "illustration", "dessin", "logo",
        "affiche", "sticker", "stickers", "icone", "avatar",
    )
    verbes_creation = (
        "genere", "generer", "regenere", "regenerer",
        "cree", "creer", "recree", "recreer",
        "fais", "fait", "dessine", "fabrique", "produis",
    )

    return (
        any(mot in t for mot in mots_visuels)
        and any(verbe in t for verbe in verbes_creation)
    )



def est_demande_modification_image(texte: str) -> bool:
    """
    Détecte les demandes où l'utilisateur veut transformer/améliorer une image
    existante. Sert surtout quand une image est jointe au message Discord.
    """
    t = normaliser(texte)

    mots_visuels = (
        "image", "photo", "logo", "illustration", "dessin", "affiche",
        "sticker", "stickers", "icone", "avatar", "visuel", "fond",
    )
    verbes_modification = (
        "rend", "rends", "ameliore", "ameliorer", "embellis", "embellir",
        "modifie", "modifier", "transforme", "transformer", "retouche",
        "retoucher", "change", "changer", "remplace", "remplacer",
        "ajoute", "ajouter", "supprime", "supprimer", "enleve", "enlever",
        "corrige", "corriger", "refais", "refaire", "retravaille",
        "retravailler", "modernise", "moderniser", "stylise", "styliser",
    )

    return (
        any(mot in t for mot in mots_visuels)
        and any(verbe in t for verbe in verbes_modification)
    )


def pieces_contiennent_image(pieces) -> bool:
    for piece in pieces or []:
        ctype = str(piece.get("content_type") or "").lower()
        nom = str(piece.get("filename") or "").lower()
        if ctype.startswith("image/") or nom.endswith(
            (".png", ".jpg", ".jpeg", ".webp", ".gif")
        ):
            return True
    return False


def est_demande_code(texte: str) -> bool:
    t = normaliser(texte)

    if t.startswith("/code ") or t == "/code":
        return True

    termes = [
        "donne le code",
        "code final",
        "script python",
        "script lua",
        "script roblox",
        "ecris un script",
        "cree un script",
        "programme en",
        "code en python",
        "code python",
        "code lua",
        "code javascript",
        "code html",
        "code css",
    ]

    return any(x in t for x in termes)


def nettoyer_commande(texte: str) -> str:
    for prefixe in ("/image", "/code", "/ask"):
        if texte.lower().startswith(prefixe):
            reste = texte[len(prefixe):].strip()
            return reste or texte
    return texte


# ============================================================
# SERVICE CHATGPT
# ============================================================


class ChatGPTService:
    def __init__(self):
        # Selenium doit rester dans un seul thread.
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ChatGPTChromium")
        self.driver = None
        self.demarrage_lock = asyncio.Lock()
        self.channel_memory = self._charger_memoire()

    def _charger_memoire(self):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}
        except FileNotFoundError:
            pass
        except Exception as e:
            print("Memoire salons illisible :", repr(e))
        return {}

    def _sauver_memoire(self):
        try:
            dossier = os.path.dirname(MEMORY_FILE)
            if dossier:
                os.makedirs(dossier, exist_ok=True)
            # Garde les entrées les plus récentes si le fichier devient trop grand.
            if len(self.channel_memory) > MAX_MEMORY_CHANNELS:
                keys = list(self.channel_memory.keys())[-MAX_MEMORY_CHANNELS:]
                self.channel_memory = {k: self.channel_memory[k] for k in keys}
            tmp = MEMORY_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.channel_memory, f, ensure_ascii=False, indent=2)
            os.replace(tmp, MEMORY_FILE)
        except Exception as e:
            print("Impossible de sauvegarder la memoire salons :", repr(e))

    def _memoriser_url(self, channel_id, driver):
        try:
            url = driver.current_url or ""
        except Exception:
            return
        if "chatgpt.com/c/" not in url:
            return
        cle = str(channel_id)
        # Réinsère la clé pour conserver un ordre approximatif de récence.
        self.channel_memory.pop(cle, None)
        self.channel_memory[cle] = url
        self._sauver_memoire()

    def _preparer_channel_sync(self, driver, channel_id):
        """Ouvre la conversation ChatGPT mémorisée pour ce salon, sinon en crée une."""
        cle = str(channel_id)
        url = self.channel_memory.get(cle)
        if url and "chatgpt.com/c/" in url:
            try:
                driver.get(url)
                if _attendre_page_chatgpt(driver, timeout=25):
                    print(f"Memoire salon {channel_id} chargee.")
                    return
            except Exception as e:
                print("Conversation memorisee inaccessible, nouveau chat :", repr(e))
            self.channel_memory.pop(cle, None)
            self._sauver_memoire()

        preparer_chatgpt(driver, nouveau_chat=True)
        print(f"Nouvelle memoire ChatGPT pour le salon {channel_id}.")

    async def demarrer(self):
        async with self.demarrage_lock:
            if self.driver is not None:
                return
            loop = asyncio.get_running_loop()
            self.driver = await loop.run_in_executor(self.executor, connecter_chromium)

    async def _run(self, fonction, *args):
        if self.driver is None:
            await self.demarrer()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, fonction, self.driver, *args)

    def _redemarrer_chromium_sync(self):
        ancien_driver = self.driver
        self.driver = None

        try:
            fermer_chromium_complet(ancien_driver)
        except Exception as e:
            print("Erreur pendant la fermeture Chromium :", repr(e))

        # Petite pause pour laisser l'OS récupérer la RAM/processus.
        time.sleep(2.0)

        print("Nettoyage RAM : reouverture de Chromium...")
        nouveau_driver = connecter_chromium()
        self.driver = nouveau_driver
        print("Nettoyage RAM : Chromium et ChatGPT reouverts.")
        return True

    async def redemarrer_chromium(self):
        """
        Redémarre Chromium dans le même executor Selenium.
        Ainsi aucune génération ne peut utiliser le navigateur pendant le redémarrage.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self.executor,
            self._redemarrer_chromium_sync,
        )

    def _nouveau_chat_sync(self, driver):
        driver.get(CHATGPT_URL)
        time.sleep(1.8)

    async def nouveau_chat(self):
        await self._run(self._nouveau_chat_sync)

    def _question_texte_sync(self, driver, prompt, channel_id, pieces, cancel_event):
        self._preparer_channel_sync(driver, channel_id)
        chemins = []
        try:
            verifier_annulation(cancel_event)
            if pieces:
                chemins = televerser_pieces_jointes_chatgpt(driver, pieces, cancel_event)
            nombre_avant = envoyer_prompt(driver, prompt)
            # L'URL de conversation apparaît généralement juste après l'envoi.
            time.sleep(0.4)
            self._memoriser_url(channel_id, driver)
            texte = attendre_reponse_texte(
                driver,
                nombre_avant,
                cancel_event=cancel_event,
            )
            self._memoriser_url(channel_id, driver)
            return {
                "type": "texte",
                "texte": texte,
                "chat_url": driver.current_url,
            }
        finally:
            self._memoriser_url(channel_id, driver)
            supprimer_fichiers_temporaires(chemins)

    async def question_texte(self, prompt, channel_id, pieces=None, cancel_event=None):
        return await self._run(
            self._question_texte_sync,
            prompt,
            channel_id,
            pieces or [],
            cancel_event,
        )

    def _generer_image_sync(self, driver, prompt, channel_id, pieces, cancel_event):
        self._preparer_channel_sync(driver, channel_id)
        chemins = []
        try:
            verifier_annulation(cancel_event)
            if pieces:
                chemins = televerser_pieces_jointes_chatgpt(driver, pieces, cancel_event)

            prompt_final = (
                "Génère directement une image correspondant exactement à cette demande. "
                "Ne te contente pas de décrire l'image : crée réellement l'image. "
                "DEMANDE : " + prompt
            )

            # Snapshot APRES l'upload pour ne jamais confondre une pièce jointe avec le résultat.
            etat_images_avant = prendre_etat_images_page(driver)
            nombre_avant = envoyer_prompt(driver, prompt_final)
            time.sleep(0.4)
            self._memoriser_url(channel_id, driver)
            texte, image_bytes = attendre_image(
                driver,
                nombre_avant,
                etat_images_avant=etat_images_avant,
                cancel_event=cancel_event,
            )
            self._memoriser_url(channel_id, driver)
            return {
                "type": "image",
                "texte": texte,
                "image_bytes": image_bytes,
                "chat_url": driver.current_url,
            }
        finally:
            self._memoriser_url(channel_id, driver)
            supprimer_fichiers_temporaires(chemins)

    async def generer_image(self, prompt, channel_id, pieces=None, cancel_event=None):
        return await self._run(
            self._generer_image_sync,
            prompt,
            channel_id,
            pieces or [],
            cancel_event,
        )

    def _modifier_image_sync(self, driver, image_source_bytes, modifications, cancel_event):
        # Les modifications d'image restent dans un chat dédié afin d'éviter
        # qu'une ancienne image de la mémoire du salon soit choisie par erreur.
        preparer_chatgpt(driver, nouveau_chat=True)
        afficher_chromium(driver)
        chemin_temp = None
        try:
            verifier_annulation(cancel_event)
            chemin_temp = televerser_image_chatgpt(driver, image_source_bytes)
            etat_images_avant = prendre_etat_images_page(driver)
            empreinte_source = _empreinte_contenu_image(convertir_bytes_en_png(image_source_bytes))
            if empreinte_source:
                etat_images_avant.setdefault("empreintes", set()).add(empreinte_source)

            prompt = (
                "Modifie l'image jointe à ce message. "
                "Applique exactement les changements suivants et génère directement "
                "une NOUVELLE image modifiée. Conserve tout ce qui n'est pas demandé "
                "de changer. Ne te contente pas de décrire le résultat est donne uniquement "
                "la bonne image donne pas plusieurs images en meme temps.\n\n"
                f"MODIFICATIONS :\n{modifications}"
            )
            nombre_avant = envoyer_prompt(driver, prompt)
            minimum_total_images = int(etat_images_avant.get("total_images", 0) or 0) + 1
            minimum_modifier_count = int(etat_images_avant.get("modifier_count", 0) or 0) + 1
            texte, image_bytes = attendre_image(
                driver,
                nombre_avant,
                etat_images_avant=etat_images_avant,
                minimum_total_images=minimum_total_images,
                minimum_modifier_count=minimum_modifier_count,
                cancel_event=cancel_event,
            )
            if image_bytes and _empreinte_contenu_image(convertir_bytes_en_png(image_bytes)) == empreinte_source:
                print("⚠️ Image récupérée identique à l'image source : refusée.")
                image_bytes = None
            return {
                "type": "image",
                "texte": texte,
                "image_bytes": image_bytes,
                "chat_url": driver.current_url,
            }
        finally:
            _supprimer_fichier_temporaire(chemin_temp)

    async def modifier_image(self, image_source_bytes, modifications, cancel_event=None):
        return await self._run(
            self._modifier_image_sync,
            image_source_bytes,
            modifications,
            cancel_event,
        )

    def _rendre_png_sync(self, driver, image_source_bytes, cancel_event):
        preparer_chatgpt(driver, nouveau_chat=True)
        afficher_chromium(driver)
        chemin_temp = None
        try:
            verifier_annulation(cancel_event)
            chemin_temp = televerser_image_chatgpt(driver, image_source_bytes)
            etat_images_avant = prendre_etat_images_page(driver)
            empreinte_source = _empreinte_contenu_image(convertir_bytes_en_png(image_source_bytes))
            if empreinte_source:
                etat_images_avant.setdefault("empreintes", set()).add(empreinte_source)

            nombre_avant = envoyer_prompt(driver, PNG_REGEN_PROMPT)
            minimum_total_images = int(etat_images_avant.get("total_images", 0) or 0) + 1
            minimum_modifier_count = int(etat_images_avant.get("modifier_count", 0) or 0) + 1
            texte, image_bytes = attendre_image(
                driver,
                nombre_avant,
                etat_images_avant=etat_images_avant,
                minimum_total_images=minimum_total_images,
                minimum_modifier_count=minimum_modifier_count,
                cancel_event=cancel_event,
            )
            if image_bytes:
                image_bytes = convertir_bytes_en_png(image_bytes)
                if _empreinte_contenu_image(image_bytes) == empreinte_source:
                    print("⚠️ Image PNG récupérée identique à la source : refusée.")
                    image_bytes = None
            return {
                "type": "image",
                "texte": texte,
                "image_bytes": image_bytes,
                "chat_url": driver.current_url,
            }
        finally:
            _supprimer_fichier_temporaire(chemin_temp)

    async def rendre_png(self, image_source_bytes, cancel_event=None):
        return await self._run(self._rendre_png_sync, image_source_bytes, cancel_event)


chatgpt = ChatGPTService()


# ============================================================
# DISCORD : OUTILS D'ENVOI
# ============================================================


def couper_message(texte: str, limite: int = 3800):
    """Découpe proprement un long texte pour les descriptions d'embeds Discord."""
    texte = texte or ""
    if len(texte) <= limite:
        return [texte]

    morceaux = []
    reste = texte

    while reste:
        if len(reste) <= limite:
            morceaux.append(reste)
            break

        coupure = reste.rfind("\n", 0, limite)
        if coupure < limite // 3:
            coupure = reste.rfind(" ", 0, limite)
        if coupure <= 0:
            coupure = limite

        morceaux.append(reste[:coupure].rstrip())
        reste = reste[coupure:].lstrip()

    return [m for m in morceaux if m]



async def envoyer_reponse_texte(
    message_source: discord.Message,
    texte: str,
    question: str,
    type_generation: str = "texte",
):
    if not texte:
        texte = "ChatGPT n'a renvoye aucun texte."

    # Discord limite un message normal à 2000 caractères.
    morceaux = couper_message(texte, 1900)

    joindre_fichier = len(texte) > 7000 or texte.count("```") >= 2
    fichier = None
    if joindre_fichier:
        nom = "code_chatgpt_complet.txt" if type_generation == "code" else "reponse_chatgpt_complete.txt"
        fichier = discord.File(
            io.BytesIO(texte.encode("utf-8")),
            filename=nom,
        )

    for i, morceau in enumerate(morceaux, start=1):
        kwargs = {
            "content": morceau,
            "allowed_mentions": discord.AllowedMentions.none(),
        }

        if i == 1 and fichier is not None:
            kwargs["file"] = fichier

        if i == 1:
            kwargs["mention_author"] = False
            await message_source.reply(**kwargs)
        else:
            await message_source.channel.send(**kwargs)


# ============================================================
# DISCORD : FILE D'ATTENTE RÉELLE
# ============================================================


class GenerationQueue:
    def __init__(self):
        self.pending = deque()
        self.current = None
        self.worker_task = None
        self.guard = asyncio.Lock()
        self.numero = 0

        # Pour limiter la RAM de Chromium sur la VM :
        # après 2 générations/réponses terminées, on redémarre complètement
        # le navigateur en conservant le profil ChatGPT.
        self.generations_depuis_restart = 0
        self.restart_every = max(
            1,
            int(os.getenv("NATHGPT_RESTART_CHROMIUM_EVERY", "2")),
        )

    async def submit(self, status_message, type_generation, runner, label="Demande"):
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        async with self.guard:
            self.numero += 1
            job = {
                "id": self.numero,
                "status": status_message,
                "type": type_generation,
                "runner": runner,
                "future": future,
                "label": label,
            }
            self.pending.append(job)
            if self.worker_task is None or self.worker_task.done():
                self.worker_task = asyncio.create_task(self._worker())

        await self._actualiser_positions()
        return await future

    async def _snapshot(self):
        async with self.guard:
            return self.current, list(self.pending)

    async def _presence(self, current, pending):
        try:
            if client.user is None:
                return
            nb_attente = len(pending)
            if current is not None and nb_attente:
                texte = f"1 génération en cours | {nb_attente} demande{'s' if nb_attente > 1 else ''} en attente"
            elif current is not None:
                texte = "1 génération en cours"
            elif nb_attente:
                texte = f"{nb_attente} demande{'s' if nb_attente > 1 else ''} en attente"
            else:
                texte = "Disponible"
            await client.change_presence(
                status=discord.Status.online,
                activity=discord.Game(name=texte),
            )
        except Exception as e:
            print("Presence Discord impossible :", repr(e))

    async def actualiser_presence(self):
        current, pending = await self._snapshot()
        await self._presence(current, pending)

    async def _actualiser_positions(self):
        current, pending = await self._snapshot()
        total = len(pending) + (1 if current else 0)
        offset = 1 if current else 0

        for i, job in enumerate(pending, start=1):
            position = i + offset
            try:
                await job["status"].edit(
                    content=texte_file_simple(position, total),
                    embed=None,
                    attachments=[],
                )
            except Exception:
                pass
        await self._presence(current, pending)

    async def _worker(self):
        while True:
            async with self.guard:
                if not self.pending:
                    self.current = None
                    self.worker_task = None
                    current = None
                    pending = []
                    # présence mise à jour hors lock juste après
                    fin = True
                else:
                    job = self.pending.popleft()
                    self.current = job
                    fin = False

            if fin:
                await self._presence(current, pending)
                return

            await self._actualiser_positions()
            stop_event = asyncio.Event()
            cancel_event = threading.Event()
            progression = asyncio.create_task(
                boucle_progression(job["status"], job["type"], stop_event)
            )
            debut = time.monotonic()
            runner_task = asyncio.create_task(job["runner"](cancel_event))
            hard_timeout = AUTO_CANCEL_TIMEOUTS.get(job["type"], 300)

            try:
                try:
                    resultat = await asyncio.wait_for(
                        asyncio.shield(runner_task),
                        timeout=hard_timeout,
                    )
                except asyncio.TimeoutError:
                    cancel_event.set()
                    # Laisse au thread Selenium quelques secondes pour sortir de sa boucle.
                    try:
                        await asyncio.wait_for(asyncio.shield(runner_task), timeout=12)
                    except Exception:
                        pass
                    raise AutoCancelError(
                        f"generation annulee automatiquement apres {hard_timeout} secondes"
                    )

                duree = time.monotonic() - debut
                enregistrer_duree(job["type"], duree)

                stop_event.set()
                try:
                    await progression
                except Exception:
                    pass

                try:
                    await job["status"].delete()
                except Exception:
                    try:
                        final_txt = (
                            "image terminee 100%"
                            if job["type"] in ("image", "edition_image", "png_image")
                            else "reponse terminee"
                        )
                        await job["status"].edit(content=final_txt, embed=None, attachments=[])
                    except Exception:
                        pass

                if not job["future"].done():
                    job["future"].set_result((resultat, duree))

                self.generations_depuis_restart += 1

                if self.generations_depuis_restart >= self.restart_every:
                    self.generations_depuis_restart = 0
                    try:
                        print(
                            f"Nettoyage RAM : {self.restart_every} generations terminees, "
                            "redemarrage de Chromium."
                        )
                        await chatgpt.redemarrer_chromium()
                    except Exception as restart_error:
                        # Une réponse déjà terminée ne doit pas devenir une erreur
                        # uniquement parce que le nettoyage RAM a échoué.
                        print(
                            "Redemarrage automatique de Chromium impossible :",
                            repr(restart_error),
                        )
                        # Forcer une reconnexion au prochain job.
                        try:
                            chatgpt.driver = None
                        except Exception:
                            pass

            except Exception as e:
                cancel_event.set()
                stop_event.set()
                try:
                    await progression
                except Exception:
                    pass

                est_timeout = isinstance(e, (TimeoutError, AutoCancelError, GenerationCancelledError))
                try:
                    if est_timeout:
                        texte = "generation annulee automatiquement - utilise retry: pour relancer"
                    else:
                        detail = tronquer_texte(f"{type(e).__name__}: {e}", 1700)
                        texte = f"erreur pendant la generation : {detail} - utilise retry: pour relancer"
                    await job["status"].edit(
                        content=texte,
                        embed=None,
                        attachments=[],
                    )
                except Exception:
                    pass

                if not job["future"].done():
                    job["future"].set_exception(e)
            finally:
                async with self.guard:
                    self.current = None
                await self._actualiser_positions()


generation_queue = GenerationQueue()


# ============================================================
# DISCORD : CLIENT
# ============================================================


intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)


def channel_est_dans_categorie_nathgpt(channel) -> bool:
    """Accepte tous les salons/threads appartenant à la catégorie NathGPT."""
    try:
        category_id = getattr(channel, "category_id", None)
        if category_id == DISCORD_CATEGORY_ID:
            return True

        # Un thread n'a pas toujours category_id directement :
        # sa catégorie est celle de son salon parent.
        parent = getattr(channel, "parent", None)
        if parent is not None:
            parent_category_id = getattr(parent, "category_id", None)
            if parent_category_id == DISCORD_CATEGORY_ID:
                return True

        return False
    except Exception:
        return False


def extraire_texte_message_discord(message: discord.Message) -> str:
    """Lit le texte normal + le contenu utile des embeds de bots/webhooks."""
    morceaux = []

    if message.content and message.content.strip():
        morceaux.append(message.content.strip())

    for embed in getattr(message, "embeds", []) or []:
        try:
            if embed.title:
                morceaux.append(str(embed.title))
            if embed.description:
                morceaux.append(str(embed.description))
            for field in embed.fields:
                if field.name:
                    morceaux.append(str(field.name))
                if field.value:
                    morceaux.append(str(field.value))
        except Exception:
            pass

    # Évite de répéter exactement la même ligne venant de content + embed.
    uniques = []
    vus = set()
    for morceau in morceaux:
        cle = morceau.strip()
        if cle and cle not in vus:
            vus.add(cle)
            uniques.append(cle)

    return "\n".join(uniques).strip()


def analyser_commande_image_reponse(texte: str):
    """Retourne ("modify", prompt), ("png", "") ou None."""
    brut = (texte or "").strip()
    bas = brut.lower()

    if bas.startswith("modify:"):
        return "modify", brut.split(":", 1)[1].strip()

    if bas == "png" or bas == "png:" or bas.startswith("png:"):
        return "png", ""

    return None


def est_commande_decomp_cricut(texte: str) -> bool:
    return (texte or "").strip().lower() in DECOMP_CRICUT_COMMANDS


def piece_est_image(piece: dict) -> bool:
    ctype = str(piece.get("content_type") or "").lower()
    nom = str(piece.get("filename") or "").lower()
    return ctype.startswith("image/") or nom.endswith((".png", ".jpg", ".jpeg", ".webp"))


def premiere_piece_image(pieces):
    for piece in pieces or []:
        if piece_est_image(piece):
            return piece
    return None


def extraire_nombre_stickers(texte: str):
    for bloc in re.findall(r"\d+", texte or ""):
        try:
            n = int(bloc)
        except Exception:
            continue
        if 0 < n <= 500:
            return n
    return None


def formater_temps_restant_decomp(nb_restants: int) -> str:
    minutes = max(0, int(nb_restants)) * DECOMP_CRICUT_MINUTES_PER_IMAGE
    if minutes <= 0:
        return "0 min"
    heures, mins = divmod(minutes, 60)
    if heures and mins:
        return f"{heures}h{mins:02d}"
    if heures:
        return f"{heures}h"
    return f"{minutes} min"


def texte_progression_decomp(total: int, genere: int) -> str:
    return (
        f"{total} images a generer temps restant "
        f"{formater_temps_restant_decomp(total - genere)} "
        f"image genere {genere:02d}/{total}"
    )


def prompt_decomp_sticker(index: int) -> str:
    return DECOMP_CRICUT_ITEM_PROMPT_TEMPLATE.format(index=index)


async def recuperer_message_reference(message: discord.Message):
    ref = getattr(message, "reference", None)
    if ref is None or ref.message_id is None:
        return None

    resolved = getattr(ref, "resolved", None)
    if isinstance(resolved, discord.Message):
        return resolved

    try:
        return await message.channel.fetch_message(ref.message_id)
    except Exception:
        return None


def attachment_est_image(attachment: discord.Attachment) -> bool:
    content_type = (getattr(attachment, "content_type", None) or "").lower()
    nom = (getattr(attachment, "filename", "") or "").lower()
    return (
        content_type.startswith("image/")
        or nom.endswith((".png", ".jpg", ".jpeg", ".webp"))
    )


async def lire_image_du_message(message_image: discord.Message):
    for attachment in getattr(message_image, "attachments", []) or []:
        if not attachment_est_image(attachment):
            continue
        try:
            contenu = await attachment.read()
        except Exception:
            continue
        if contenu and len(contenu) > 10_000:
            return contenu
    return None



LAST_REQUESTS = {}


def memoriser_derniere_requete(channel_id, requete):
    cle = int(channel_id)
    # Réinsère pour garder un ordre de récence.
    LAST_REQUESTS.pop(cle, None)
    LAST_REQUESTS[cle] = requete
    while len(LAST_REQUESTS) > 50:
        LAST_REQUESTS.pop(next(iter(LAST_REQUESTS)))


def _nom_fichier_depuis_url(url: str, index: int, content_type: str = "") -> str:
    try:
        path = urllib.parse.urlparse(url).path
        nom = urllib.parse.unquote(os.path.basename(path)).strip()
    except Exception:
        nom = ""

    # Retire une éventuelle query encodée dans le nom.
    nom = nom.split("?", 1)[0].strip()

    if nom and "." in nom:
        return nom[:180]

    extension = mimetypes.guess_extension((content_type or "").split(";", 1)[0].strip()) or ""
    if extension == ".jpe":
        extension = ".jpg"
    if not extension:
        extension = ".png"

    return f"image_embed_{index}{extension}"


def _urls_images_embeds_discord(message: discord.Message):
    """
    Récupère les images affichées dans les embeds Discord.
    Important pour les bots/webhooks/apps : une image visible dans Discord
    n'est pas toujours présente dans message.attachments.
    """
    urls = []
    vus = set()

    for embed in getattr(message, "embeds", []) or []:
        for nom_attr in ("image", "thumbnail"):
            try:
                media = getattr(embed, nom_attr, None)
                if media is None:
                    continue

                candidats = [
                    getattr(media, "proxy_url", None),
                    getattr(media, "url", None),
                ]

                for url in candidats:
                    url = str(url or "").strip()
                    if not url or not url.startswith(("https://", "http://")):
                        continue
                    if url in vus:
                        continue
                    vus.add(url)
                    urls.append(url)
                    break
            except Exception:
                continue

    return urls


async def _telecharger_piece_depuis_url(url: str, index: int):
    timeout = aiohttp.ClientTimeout(total=35, connect=12, sock_read=25)
    headers = {
        "User-Agent": "Mozilla/5.0 NathGPT/1.0",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url, allow_redirects=True) as response:
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(
                    f"image embed inaccessible (HTTP {response.status})"
                )

            content_type = (
                response.headers.get("Content-Type", "")
                .split(";", 1)[0]
                .strip()
                .lower()
            )

            taille_annoncee = response.headers.get("Content-Length")
            if taille_annoncee:
                try:
                    if int(taille_annoncee) > MAX_ATTACHMENT_BYTES:
                        raise ValueError(
                            f"image embed trop grande "
                            f"(maximum {MAX_ATTACHMENT_BYTES // (1024*1024)} Mo)"
                        )
                except ValueError as e:
                    if "image embed trop grande" in str(e):
                        raise

            contenu = await response.read()

    if not contenu:
        raise RuntimeError("image embed vide")

    if len(contenu) > MAX_ATTACHMENT_BYTES:
        raise ValueError(
            f"image embed trop grande "
            f"(maximum {MAX_ATTACHMENT_BYTES // (1024*1024)} Mo)"
        )

    # Discord/proxy peut parfois renvoyer application/octet-stream.
    # On vérifie alors réellement les octets avec Pillow.
    est_image = content_type.startswith("image/")
    if not est_image:
        try:
            with Image.open(io.BytesIO(contenu)) as im:
                fmt = (im.format or "PNG").lower()
            content_type = f"image/{'jpeg' if fmt in ('jpg', 'jpeg') else fmt}"
            est_image = True
        except Exception:
            est_image = False

    if not est_image:
        raise RuntimeError(
            f"la ressource de l'embed n'est pas une image ({content_type or 'type inconnu'})"
        )

    nom = _nom_fichier_depuis_url(url, index, content_type)

    return {
        "filename": nom,
        "content_type": content_type or "image/png",
        "bytes": contenu,
        "source": "discord_embed",
    }


async def lire_pieces_jointes_discord(message: discord.Message):
    """
    Lit :
    - les vraies pièces jointes Discord ;
    - les images présentes dans les embeds des bots/webhooks/apps.

    Ainsi une image visible sous un message WEB_AI est réellement envoyée
    à ChatGPT, même si Discord ne la fournit pas dans message.attachments.
    """
    pieces = []
    total = 0
    urls_deja_vues = set()

    attachments = list(getattr(message, "attachments", []) or [])[:MAX_ATTACHMENTS]

    for att in attachments:
        taille = int(getattr(att, "size", 0) or 0)
        nom = getattr(att, "filename", None) or "fichier"

        if taille and taille > MAX_ATTACHMENT_BYTES:
            raise ValueError(
                f"piece jointe trop grande : {nom} "
                f"(maximum {MAX_ATTACHMENT_BYTES // (1024*1024)} Mo)"
            )

        try:
            contenu = await att.read()
        except Exception as e:
            raise RuntimeError(
                f"impossible de lire la piece jointe {nom}: {e}"
            )

        if len(contenu) > MAX_ATTACHMENT_BYTES:
            raise ValueError(
                f"piece jointe trop grande : {nom} "
                f"(maximum {MAX_ATTACHMENT_BYTES // (1024*1024)} Mo)"
            )

        total += len(contenu)
        if total > MAX_ATTACHMENTS_TOTAL_BYTES:
            raise ValueError(
                f"pieces jointes trop volumineuses "
                f"(maximum total {MAX_ATTACHMENTS_TOTAL_BYTES // (1024*1024)} Mo)"
            )

        url_att = str(getattr(att, "url", "") or "").strip()
        proxy_att = str(getattr(att, "proxy_url", "") or "").strip()
        if url_att:
            urls_deja_vues.add(url_att)
        if proxy_att:
            urls_deja_vues.add(proxy_att)

        pieces.append({
            "filename": nom,
            "content_type": (
                getattr(att, "content_type", None)
                or mimetypes.guess_type(nom)[0]
                or "application/octet-stream"
            ),
            "bytes": contenu,
            "source": "discord_attachment",
        })

    # Complète avec les images d'embed, dans la limite globale.
    places_restantes = max(0, MAX_ATTACHMENTS - len(pieces))
    if places_restantes:
        urls_embeds = _urls_images_embeds_discord(message)

        index_embed = 0
        for url in urls_embeds:
            if len(pieces) >= MAX_ATTACHMENTS:
                break
            if url in urls_deja_vues:
                continue

            index_embed += 1
            try:
                piece = await _telecharger_piece_depuis_url(url, index_embed)
            except Exception as e:
                # Une miniature cassée ne doit pas faire échouer tout le message.
                print("Image embed Discord ignoree :", repr(e))
                continue

            contenu = piece.get("bytes") or b""
            total += len(contenu)

            if total > MAX_ATTACHMENTS_TOTAL_BYTES:
                raise ValueError(
                    f"pieces jointes trop volumineuses "
                    f"(maximum total {MAX_ATTACHMENTS_TOTAL_BYTES // (1024*1024)} Mo)"
                )

            pieces.append(piece)
            urls_deja_vues.add(url)

    return pieces


def texte_par_defaut_pieces(pieces):
    if not pieces:
        return ""
    if len(pieces) == 1 and str(pieces[0].get("content_type", "")).startswith("image/"):
        return "Analyse cette image et reponds de facon utile."
    return "Analyse les pieces jointes et reponds de facon utile."


def est_commande_retry(texte: str) -> bool:
    t = (texte or "").strip().lower()
    return t in {"retry", "retry:", "/retry"}

async def envoyer_image_simple(
    message_source: discord.Message,
    image_bytes: bytes,
    prefixe_nom: str = "image_chatgpt",
):
    image_bytes = convertir_bytes_en_png(image_bytes)
    filename = f"{prefixe_nom}_{uuid.uuid4().hex[:8]}.png"
    fichier = discord.File(io.BytesIO(image_bytes), filename=filename)
    contenu = (
        "image generee\n"
        "reponds a cette image avec modify: ton prompt pour la modifier, "
        "ou png: pour l'optimiser en PNG transparent A4 / Cricut"
    )
    return await message_source.reply(
        content=contenu,
        file=fichier,
        mention_author=False,
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def envoyer_image_brute(
    message_source: discord.Message,
    image_bytes: bytes,
    contenu: str,
    prefixe_nom: str = "sticker",
):
    image_bytes = convertir_bytes_en_png(image_bytes)
    filename = f"{prefixe_nom}_{uuid.uuid4().hex[:8]}.png"
    fichier = discord.File(io.BytesIO(image_bytes), filename=filename)
    return await message_source.reply(
        content=contenu,
        file=fichier,
        mention_author=False,
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def traiter_commande_image_bytes(
    message: discord.Message,
    commande: str,
    argument: str,
    image_source_bytes: bytes,
    memoriser_retry: bool = True,
) -> bool:
    if commande == "modify" and not argument:
        await message.reply(
            "utilise `modify: ce que tu veux changer`",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True

    if memoriser_retry:
        memoriser_derniere_requete(
            message.channel.id,
            {
                "kind": "image_command",
                "commande": commande,
                "argument": argument,
                "image_source_bytes": image_source_bytes,
            },
        )

    statut = await message.reply(
        content="demande en attente",
        mention_author=False,
        allowed_mentions=discord.AllowedMentions.none(),
    )

    if commande == "modify":
        type_generation = "edition_image"
        runner = lambda cancel_event: chatgpt.modifier_image(
            image_source_bytes,
            argument,
            cancel_event=cancel_event,
        )
        prefixe = "image_modifiee"
        label = "Modification image"
    else:
        type_generation = "png_image"
        runner = lambda cancel_event: chatgpt.rendre_png(
            image_source_bytes,
            cancel_event=cancel_event,
        )
        prefixe = "image_png"
        label = "PNG A4 sans fond"

    try:
        resultat, _duree = await generation_queue.submit(
            statut,
            type_generation,
            runner,
            label=label,
        )
    except Exception as e:
        print("Erreur commande image :", repr(e))
        return True

    image_bytes = resultat.get("image_bytes")
    texte = (resultat.get("texte") or "").strip()
    if not image_bytes:
        detail = texte or "ChatGPT a termine mais aucune nouvelle image n'a pu etre recuperee."
        await message.reply(
            tronquer_texte(detail, 1900),
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True

    await envoyer_image_simple(message, image_bytes, prefixe_nom=prefixe)
    return True


async def traiter_commande_image_reponse(message: discord.Message, commande, argument: str) -> bool:
    """Traite modify:/png: quand le message est une réponse à une image du bot."""
    message_image = await recuperer_message_reference(message)
    if message_image is None:
        await message.reply(
            "reponds directement au message qui contient l'image",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True

    if client.user is not None and message_image.author.id != client.user.id:
        await message.reply(
            "la commande doit etre envoyee en reponse a une image de NathGPT",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True

    image_source_bytes = await lire_image_du_message(message_image)
    if not image_source_bytes:
        await message.reply(
            "aucune image exploitable n'a ete trouvee dans le message",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True

    return await traiter_commande_image_bytes(
        message,
        commande,
        argument,
        image_source_bytes,
        memoriser_retry=True,
    )


async def traiter_decomp_cricut_image_bytes(
    message: discord.Message,
    image_source_bytes: bytes,
    memoriser_retry: bool = True,
) -> bool:
    if memoriser_retry:
        memoriser_derniere_requete(
            message.channel.id,
            {
                "kind": "decomp_cricut",
                "image_source_bytes": image_source_bytes,
            },
        )

    await message.reply(
        "dedutcion du temps de chargement",
        mention_author=False,
        allowed_mentions=discord.AllowedMentions.none(),
    )

    status_count = await message.channel.send(
        "dedutcion du temps de chargement",
        allowed_mentions=discord.AllowedMentions.none(),
    )

    piece_image = {
        "filename": "decomp_cricut_source.png",
        "content_type": "image/png",
        "bytes": convertir_bytes_en_png(image_source_bytes),
        "source": "decomp_cricut_memory",
    }

    pseudo_channel_id = f"{message.channel.id}{DECOMP_CRICUT_MEMORY_KEY_SUFFIX}:{message.id}"

    prompts_comptage = [
        DECOMP_CRICUT_COUNT_PROMPT,
        "regarde uniquement l'image jointe et reponds seulement par le nombre total de stickers visibles",
    ]

    resultat_count = None
    derniere_erreur_count = None
    for tentative_count, prompt_count in enumerate(prompts_comptage, start=1):
        runner_count = lambda cancel_event, p=prompt_count: chatgpt.question_texte(
            p,
            channel_id=pseudo_channel_id,
            pieces=[piece_image],
            cancel_event=cancel_event,
        )

        try:
            resultat_count, _ = await generation_queue.submit(
                status_count,
                "decomp_count",
                runner_count,
                label=f"Decomp Cricut - comptage {tentative_count}/{len(prompts_comptage)}",
            )
        except Exception as e:
            derniere_erreur_count = e
            print("Erreur comptage decomp_cricut :", repr(e))
            continue

        total_test = extraire_nombre_stickers((resultat_count or {}).get("texte", ""))
        if total_test:
            break
        resultat_count = None
        derniere_erreur_count = RuntimeError("reponse de comptage sans nombre exploitable")
        print("⚠️ Comptage decomp_cricut sans nombre exploitable, nouvelle tentative.")

    if resultat_count is None:
        if derniere_erreur_count:
            print("Echec final comptage decomp_cricut :", repr(derniere_erreur_count))
        await message.reply(
            "impossible de compter les stickers automatiquement - utilise retry: pour relancer",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True

    total = extraire_nombre_stickers((resultat_count or {}).get("texte", ""))
    if not total:
        await message.reply(
            "impossible de determiner combien de stickers il y a dans l'image",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True

    genere = 0
    await message.channel.send(
        texte_progression_decomp(total, genere),
        allowed_mentions=discord.AllowedMentions.none(),
    )

    empreinte_source_decomp = _empreinte_contenu_image(convertir_bytes_en_png(image_source_bytes))

    for index in range(1, total + 1):
        status_image = await message.channel.send(
            texte_progression_decomp(total, genere),
            allowed_mentions=discord.AllowedMentions.none(),
        )

        prompt = prompt_decomp_sticker(index)
        image_bytes = None
        derniere_erreur = None

        for tentative in range(1, 4):
            runner_image = lambda cancel_event, p=prompt: chatgpt.modifier_image(
                image_source_bytes,
                p,
                cancel_event=cancel_event,
            )

            try:
                resultat_image, _ = await generation_queue.submit(
                    status_image,
                    "decomp_image",
                    runner_image,
                    label=f"Decomp Cricut {index}/{total} (tentative {tentative}/3)",
                )
            except Exception as e:
                derniere_erreur = e
                print("Erreur generation sticker decomp_cricut :", repr(e))
                continue

            image_candidate = (resultat_image or {}).get("image_bytes")
            if not image_candidate:
                derniere_erreur = RuntimeError("aucune image generee")
                print(f"⚠️ Sticker {index}/{total} : aucune image recue (tentative {tentative}/3).")
                continue

            empreinte_candidate = _empreinte_contenu_image(convertir_bytes_en_png(image_candidate))
            if empreinte_candidate and empreinte_candidate == empreinte_source_decomp:
                derniere_erreur = RuntimeError("image source renvoyee a la place de l'image generee")
                print(
                    f"⚠️ Sticker {index}/{total} : image source detectee au lieu du resultat genere "
                    f"(tentative {tentative}/3)."
                )
                continue

            image_bytes = image_candidate
            break

        if not image_bytes:
            if derniere_erreur:
                print("Echec final generation sticker decomp_cricut :", repr(derniere_erreur))
            await message.reply(
                f"generation du sticker {index}/{total} impossible",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await message.channel.send(
                "TERMINATED",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True

        await envoyer_image_brute(
            message,
            image_bytes,
            contenu=f"sticker {index}/{total}",
            prefixe_nom=f"sticker_{index:02d}",
        )

        genere = index
        await message.channel.send(
            texte_progression_decomp(total, genere),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    await message.channel.send(
        "TERMINATED",
        allowed_mentions=discord.AllowedMentions.none(),
    )
    return True


async def traiter_decomp_cricut(message: discord.Message) -> bool:
    try:
        pieces = await lire_pieces_jointes_discord(message)
    except Exception as e:
        await message.reply(
            f"piece jointe refusee : {tronquer_texte(str(e), 1700)}",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True

    piece_image = premiere_piece_image(pieces)
    if piece_image is None:
        await message.reply(
            "ajoute une image a ton message avec :decomp_cricut",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True

    image_source_bytes = piece_image.get("bytes") or b""
    if not image_source_bytes:
        await message.reply(
            "impossible de lire l'image jointe",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True

    return await traiter_decomp_cricut_image_bytes(
        message,
        image_source_bytes,
        memoriser_retry=True,
    )


@client.event
async def on_ready():
    print()
    print("=================================================")
    print(" NATHGPT FINAL ORACLE - DISCORD -> CHATGPT")
    print("=================================================")
    print(f"Bot : {client.user}")
    print(f"Bot ID : {client.user.id}")
    print(f"Categorie Discord : {DISCORD_CATEGORY_ID}")
    print("Memoire par salon : OUI")
    print("Pieces jointes : OUI")
    print("Annulation automatique : OUI")
    print()

    try:
        await chatgpt.demarrer()
        print("ChatGPT / Chromium pret.")
    except Exception as e:
        print("ChatGPT / Chromium non pret :", repr(e))
        print("Connecte-toi manuellement a ChatGPT dans Chromium.")

    await generation_queue.actualiser_presence()
    print()
    print("Commandes :")
    print("  /image ton prompt")
    print("  /code ta demande")
    print("  /ask ta question")
    print("  retry:")
    print("  modify: ... en reponse a une image")
    print("  png: en reponse a une image")
    print("  :decomp_cricut avec une image jointe")
    print()


async def traiter_message_discord(
    message: discord.Message,
    requete_override=None,
    memoriser_retry: bool = True,
):
    if requete_override is None:
        question_originale = extraire_texte_message_discord(message)
        try:
            pieces = await lire_pieces_jointes_discord(message)
        except Exception as e:
            await message.reply(
                f"piece jointe refusee : {tronquer_texte(str(e), 1700)}",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if not question_originale.strip():
            question_originale = texte_par_defaut_pieces(pieces)
        question = nettoyer_commande(question_originale)

        if (
            est_demande_image(question_originale)
            or (
                pieces_contiennent_image(pieces)
                and est_demande_modification_image(question_originale)
            )
        ):
            type_generation = "image"
        elif est_demande_code(question_originale):
            type_generation = "code"
        else:
            type_generation = "texte"

        requete = {
            "kind": "normal",
            "question_originale": question_originale,
            "question": question,
            "type_generation": type_generation,
            "pieces": pieces,
        }
    else:
        requete = requete_override
        question_originale = requete.get("question_originale", "")
        question = requete.get("question", "")
        type_generation = requete.get("type_generation", "texte")
        pieces = requete.get("pieces", [])

    if not question:
        return

    if memoriser_retry:
        memoriser_derniere_requete(message.channel.id, requete)

    auteur = getattr(message.author, "display_name", None) or str(message.author)
    print(
        f"Type : {type_generation} | Salon : {message.channel.id} | "
        f"Auteur : {auteur} | Pieces : {len(pieces)} | "
        f"Images embeds : {len(_urls_images_embeds_discord(message))} | "
        f"Message : {question_originale!r}"
    )

    statut = await message.reply(
        content="demande en attente",
        mention_author=False,
        allowed_mentions=discord.AllowedMentions.none(),
    )

    if type_generation == "image":
        runner = lambda cancel_event: chatgpt.generer_image(
            question,
            channel_id=message.channel.id,
            pieces=pieces,
            cancel_event=cancel_event,
        )
    else:
        runner = lambda cancel_event: chatgpt.question_texte(
            question,
            channel_id=message.channel.id,
            pieces=pieces,
            cancel_event=cancel_event,
        )

    try:
        resultat, _duree = await generation_queue.submit(
            statut,
            type_generation,
            runner,
            label=f"Demande de {auteur}",
        )
    except Exception as e:
        print("ERREUR :", repr(e))
        return

    if type_generation == "image":
        image_bytes = resultat.get("image_bytes")
        texte = (resultat.get("texte") or "").strip()
        if not image_bytes:
            detail = texte or "ChatGPT a termine mais aucune image n'a pu etre recuperee."
            await message.reply(
                tronquer_texte(detail, 1900),
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await envoyer_image_simple(message, image_bytes, prefixe_nom="image_chatgpt")
    else:
        await envoyer_reponse_texte(
            message,
            resultat.get("texte", ""),
            question=question,
            type_generation=type_generation,
        )


async def traiter_retry(message: discord.Message):
    requete = LAST_REQUESTS.get(message.channel.id)
    if not requete:
        await message.reply(
            "aucune demande precedente a relancer dans ce salon",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    await message.reply(
        "relance de la derniere demande",
        mention_author=False,
        allowed_mentions=discord.AllowedMentions.none(),
    )

    if requete.get("kind") == "image_command":
        await traiter_commande_image_bytes(
            message,
            requete.get("commande", "modify"),
            requete.get("argument", ""),
            requete.get("image_source_bytes", b""),
            memoriser_retry=False,
        )
        return

    if requete.get("kind") == "decomp_cricut":
        await traiter_decomp_cricut_image_bytes(
            message,
            requete.get("image_source_bytes", b""),
            memoriser_retry=False,
        )
        return

    await traiter_message_discord(
        message,
        requete_override=requete,
        memoriser_retry=False,
    )


@client.event
async def on_message(message: discord.Message):
    if client.user is not None and message.author.id == client.user.id:
        return
    if message.author.id in IGNORE_BOT_IDS:
        return
    if getattr(message.author, "bot", False) and not ACCEPTER_MESSAGES_BOTS:
        return
    if getattr(message, "webhook_id", None) is not None and not ACCEPTER_WEBHOOKS:
        return
    if not channel_est_dans_categorie_nathgpt(message.channel):
        return

    if est_commande_retry(message.content or ""):
        await traiter_retry(message)
        return

    if est_commande_decomp_cricut(message.content or ""):
        await traiter_decomp_cricut(message)
        return

    commande_image = analyser_commande_image_reponse(message.content or "")
    if commande_image is not None:
        commande, argument = commande_image
        await traiter_commande_image_reponse(message, commande, argument)
        return

    # Un message contenant uniquement une pièce jointe OU une image d'embed
    # est aussi une vraie demande.
    if (
        not extraire_texte_message_discord(message)
        and not getattr(message, "attachments", None)
        and not _urls_images_embeds_discord(message)
    ):
        return

    await traiter_message_discord(message)


# ============================================================
# PROGRAMME PRINCIPAL
# ============================================================


def verifier_configuration():
    if not DISCORD_TOKEN or DISCORD_TOKEN == "MET_TON_TOKEN_ICI":
        print("❌ Mets le token du bot dans DISCORD_TOKEN.")
        return False

    if (
        not isinstance(DISCORD_CATEGORY_ID, int)
        or DISCORD_CATEGORY_ID <= 0
    ):
        print("Mets l'ID de la categorie Discord dans DISCORD_CATEGORY_ID.")
        return False

    return True


def main():
    print()
    print("=================================================")
    print("   DISCORD → CHROMIUM → CHATGPT → DISCORD")
    print("=================================================")
    print()

    if not verifier_configuration():
        return

    try:
        client.run(DISCORD_TOKEN, log_handler=None)
    except KeyboardInterrupt:
        print("\n🛑 Arrêt demandé.")
    except Exception as e:
        print(f"\n❌ Impossible de lancer le bot : {e}")


if __name__ == "__main__":
    main()
