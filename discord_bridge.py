"""Relais asynchrone entre NathGPT et un bot Discord."""

import asyncio
import io
import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import threading
import uuid


DEFAULT_CATEGORY_ID = 1539922989200576512
DEFAULT_TARGET_BOT_ID = 1539359893063209053
TEXT_RESPONSE_SETTLE_SECONDS = 1.6


def load_local_env(project_dir: Path):
    """Charge les variables Discord depuis .env sans dépendance supplémentaire."""
    env_path = project_dir / ".env"

    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"').strip("'")

        if name in {
            "DISCORD_TOKEN",
            "DISCORD_CATEGORY_ID",
            "DISCORD_TARGET_BOT_ID",
        } and name not in os.environ:
            os.environ[name] = value


class DiscordBridge:
    """Gère un client Discord dans son propre thread et expose des jobs à Flask."""

    def __init__(self, data_dir: Path):
        load_local_env(data_dir.parent)
        self.token = os.environ.get("DISCORD_TOKEN", "").strip()
        self.category_id = int(os.environ.get("DISCORD_CATEGORY_ID", DEFAULT_CATEGORY_ID))
        self.target_bot_id = int(os.environ.get("DISCORD_TARGET_BOT_ID", DEFAULT_TARGET_BOT_ID))
        self.response_timeout = max(
            30,
            int(os.environ.get("DISCORD_RESPONSE_TIMEOUT_SECONDS", "240"))
        )
        self.store_path = data_dir / "discord_conversations.json"
        self.image_store_path = data_dir / "discord_image_messages.json"
        self._lock = threading.RLock()
        self._ready = threading.Event()
        self._loop = None
        self._client = None
        self._discord = None
        self._started = False
        self._startup_error = None
        self._result_handler = None
        self._jobs = {}
        self._channel_jobs = {}
        self._conversations = self._load_conversations()
        self._image_messages = self._load_image_messages()

    @property
    def enabled(self):
        return bool(self.token)

    def start(self):
        """Démarre le bot une seule fois, sans bloquer Flask."""
        if self._started:
            return

        if not self.token:
            print("Discord désactivé : définis DISCORD_TOKEN avant de lancer le serveur.")
            return

        try:
            import discord
        except ImportError as error:
            print(f"Discord désactivé : dépendance discord.py absente ({error}).")
            return

        self._discord = discord
        self._started = True
        self._startup_error = None
        print("Connexion du bot Discord en cours...", flush=True)
        threading.Thread(target=self._run, name="nathgpt-discord", daemon=True).start()

    def set_result_handler(self, handler):
        """Enregistre le résultat final, même sans client web connecté."""
        self._result_handler = handler

    def start_turn(
        self,
        username,
        conversation_id,
        question,
        reference_images=None,
        expects_image=False,
    ):
        """Envoie une question et retourne immédiatement l'identifiant de son flux."""
        if not self.enabled:
            raise RuntimeError("Discord n'est pas configuré : DISCORD_TOKEN est manquant.")
        if not self._started:
            self.start()

        if self._startup_error:
            raise RuntimeError(self._startup_error)

        if not self._ready.wait(timeout=25):
            if self._startup_error:
                raise RuntimeError(self._startup_error)

            raise RuntimeError(
                "Le bot Discord ne s'est pas connecté. Vérifie le token "
                "et active Message Content Intent dans Discord Developer Portal."
            )

        job_id = uuid.uuid4().hex
        with self._lock:
            self._jobs[job_id] = {
                "events": Queue(),
                "username": username,
                "conversation_id": conversation_id,
                "conversation_key": f"{username.casefold()}:{conversation_id}",
                "expects_image": bool(expects_image),
                "text_messages": {},
                "text_message_order": [],
                "text_result_timer": None,
            }

        future = asyncio.run_coroutine_threadsafe(
            self._send_turn(
                job_id,
                username,
                conversation_id,
                question,
                reference_images or [],
            ),
            self._loop,
        )

        try:
            future.result(timeout=30)
        except Exception:
            with self._lock:
                self._jobs.pop(job_id, None)
            raise

        timeout_timer = threading.Timer(
            self.response_timeout,
            self._timeout_job,
            args=(job_id,)
        )
        timeout_timer.daemon = True
        timeout_timer.start()

        return job_id

    def next_event(self, job_id, username, timeout=15):
        with self._lock:
            job = self._jobs.get(job_id)
        if not job or job["username"] != username:
            return {"type": "error", "message": "Cette génération n'existe plus."}

        try:
            return job["events"].get(timeout=timeout)
        except Empty:
            return None

    def job_conversation(self, job_id, username):
        with self._lock:
            job = self._jobs.get(job_id)

        if not job or job["username"] != username:
            return None

        return job["conversation_id"]

    def _run(self):
        try:
            asyncio.run(self._run_client())
        except Exception as error:
            self._startup_error = (
                "La connexion du bot Discord a échoué : "
                f"{type(error).__name__}. Vérifie DISCORD_TOKEN et les intents."
            )
            self._started = False
            self._ready.clear()
            print(self._startup_error, flush=True)

    async def _run_client(self):
        self._loop = asyncio.get_running_loop()

        intents = self._discord.Intents.default()
        intents.message_content = True
        self._client = self._discord.Client(intents=intents)

        @self._client.event
        async def on_ready():
            self._ready.set()
            print(f"Bot Discord connecté : {self._client.user}", flush=True)

        @self._client.event
        async def on_disconnect():
            self._ready.clear()

        @self._client.event
        async def on_message(message):
            await self._handle_message(message)

        @self._client.event
        async def on_message_edit(before, after):
            await self._handle_message(after)

        try:
            await self._client.start(self.token)
        finally:
            self._ready.clear()

    def delete_user_conversations(self, username):
        """Supprime les salons Discord appartenant au compte indiquÃ©.

        La suppression locale n'est faite qu'aprÃ¨s la suppression des salons,
        afin d'Ã©viter de faire croire Ã  l'utilisateur que Discord a Ã©tÃ©
        effacÃ© lorsque le bot n'a pas les droits nÃ©cessaires.
        """
        prefix = f"{username.casefold()}:"

        with self._lock:
            conversation_entries = {
                key: channel_id
                for key, channel_id in self._conversations.items()
                if key.startswith(prefix)
            }

        if conversation_entries:
            if not self.enabled or not self._ready.is_set() or not self._loop:
                raise RuntimeError(
                    "Le bot Discord doit Ãªtre connectÃ© pour supprimer les salons de ce compte."
                )

            future = asyncio.run_coroutine_threadsafe(
                self._delete_channels(list(conversation_entries.values())),
                self._loop,
            )
            try:
                future.result(timeout=45)
            except Exception as error:
                raise RuntimeError(
                    "Impossible de supprimer tous les salons Discord de ce compte. "
                    "VÃ©rifie que le bot a la permission GÃ©rer les salons."
                ) from error

        with self._lock:
            for key in conversation_entries:
                self._conversations.pop(key, None)
            for key in list(self._image_messages):
                if key.startswith(prefix):
                    self._image_messages.pop(key, None)
            for channel_id in conversation_entries.values():
                self._channel_jobs.pop(channel_id, None)

            self._save_conversations()
            self._save_image_messages()

    async def _delete_channels(self, channel_ids):
        for channel_id in channel_ids:
            channel = self._client.get_channel(channel_id)

            if channel is None:
                try:
                    channel = await self._client.fetch_channel(channel_id)
                except self._discord.NotFound:
                    continue

            if isinstance(channel, self._discord.TextChannel):
                try:
                    await channel.delete(reason="Suppression d'un compte NathGPT")
                except self._discord.NotFound:
                    continue

    async def _send_turn(
        self,
        job_id,
        username,
        conversation_id,
        question,
        reference_images,
    ):
        channel = await self._get_or_create_channel(username, conversation_id)
        with self._lock:
            self._channel_jobs[channel.id] = job_id

        files = [
            self._discord.File(
                io.BytesIO(image["data"]),
                filename=image["filename"],
            )
            for image in reference_images
        ]

        is_image_follow_up = (
            question.startswith("modify:") or
            question == "png:"
        )

        if is_image_follow_up:
            conversation_key = f"{username.casefold()}:{conversation_id}"

            with self._lock:
                image_message_id = self._image_messages.get(conversation_key)

            if not image_message_id:
                async for message in channel.history(limit=50, oldest_first=False):
                    if (
                        message.author.id == self.target_bot_id and
                        self._image_url_from(message)
                    ):
                        image_message_id = message.id
                        with self._lock:
                            self._image_messages[conversation_key] = image_message_id
                            self._save_image_messages()
                        break

            if not image_message_id:
                raise RuntimeError("Aucune image de référence n'est disponible dans cette discussion.")

            await channel.get_partial_message(image_message_id).reply(
                question,
                files=files,
                mention_author=False,
                allowed_mentions=self._discord.AllowedMentions.none(),
            )

        else:
            await channel.send(
                question,
                files=files,
                allowed_mentions=self._discord.AllowedMentions.none(),
            )
        self._publish(job_id, {"type": "status", "message": "Demande envoyée au moteur d'image..."})

    async def _get_or_create_channel(self, username, conversation_id):
        key = f"{username.casefold()}:{conversation_id}"
        with self._lock:
            channel_id = self._conversations.get(key)

        if channel_id:
            channel = self._client.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self._client.fetch_channel(channel_id)
                except self._discord.HTTPException:
                    channel = None
            if isinstance(channel, self._discord.TextChannel):
                return channel

        category = self._client.get_channel(self.category_id)
        if category is None:
            category = await self._client.fetch_channel(self.category_id)
        if not isinstance(category, self._discord.CategoryChannel):
            raise RuntimeError("DISCORD_CATEGORY_ID ne correspond pas à une catégorie Discord.")

        safe_user = re.sub(r"[^a-z0-9-]+", "-", username.casefold()).strip("-") or "utilisateur"
        channel_name = f"nathgpt-{safe_user}-{conversation_id[:8]}"[:100]
        channel = await category.create_text_channel(
            channel_name,
            topic=f"Conversation NathGPT de {username}",
            reason="Nouvelle conversation NathGPT",
        )

        with self._lock:
            self._conversations[key] = channel.id
            self._save_conversations()
        return channel

    async def _handle_message(self, message):
        if message.author.id != self.target_bot_id:
            return

        with self._lock:
            job_id = self._channel_jobs.get(message.channel.id)

            job = self._jobs.get(job_id) if job_id else None

        if not job_id or not job:
            return

        image_url = self._image_url_from(message)
        if image_url:
            with self._lock:
                self._image_messages[job["conversation_key"]] = message.id
                self._save_image_messages()

            self._publish(job_id, {"type": "image", "url": image_url}, final=True)
            return

        content = (message.content or "").strip()
        if not content:
            return

        # Certains bots annoncent "Image gÃ©nÃ©rÃ©e" avant d'ajouter le vrai
        # fichier dans un second message. Pour une demande d'image, cette
        # annonce reste une Ã©tape d'attente : seul le fichier/embedd image
        # termine la gÃ©nÃ©ration cÃ´tÃ© site.
        if job.get("expects_image") and self._is_image_completion_notice(content):
            self._publish(
                job_id,
                {"type": "status", "message": "Image prÃªte, rÃ©ception du fichier..."},
            )
            return

        if self._is_progress(content):
            self._publish(job_id, {"type": "status", "message": content})
            return

        self._queue_text_response(job_id, message.id, content)

    def _queue_text_response(self, job_id, message_id, content):
        """Regroupe les messages successifs du bot dans une seule rÃ©ponse."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return

            text_messages = job["text_messages"]
            if message_id not in text_messages:
                job["text_message_order"].append(message_id)
            text_messages[message_id] = content

            previous_timer = job.get("text_result_timer")
            if previous_timer:
                previous_timer.cancel()

            result_timer = threading.Timer(
                TEXT_RESPONSE_SETTLE_SECONDS,
                self._publish_combined_text,
                args=(job_id,),
            )
            result_timer.daemon = True
            job["text_result_timer"] = result_timer
            result_timer.start()

    def _publish_combined_text(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return

            parts = [
                job["text_messages"][message_id]
                for message_id in job["text_message_order"]
                if job["text_messages"].get(message_id)
            ]
            job["text_result_timer"] = None

        if parts:
            self._publish(
                job_id,
                {"type": "text", "message": "\n\n".join(parts)},
                final=True,
            )

    def _image_url_from(self, message):
        for attachment in message.attachments:
            content_type = attachment.content_type or ""
            filename = getattr(attachment, "filename", "") or ""
            if (
                content_type.startswith("image/") or
                re.search(r"\.(png|jpe?g|webp|gif|avif)(?:\?|$)", filename, re.I) or
                re.search(r"\.(png|jpe?g|webp|gif|avif)(?:\?|$)", attachment.url, re.I)
            ):
                return attachment.url

        for embed in message.embeds:
            if embed.image and embed.image.url:
                return embed.image.url
            if embed.thumbnail and embed.thumbnail.url:
                return embed.thumbnail.url
            if getattr(embed, "type", "") == "image" and getattr(embed, "url", None):
                return embed.url

        match = re.search(r"https?://\S+\.(?:png|jpe?g|webp|gif|avif)(?:\?\S*)?", message.content or "", re.I)
        return match.group(0) if match else None

    @staticmethod
    def _is_image_completion_notice(content):
        text = " ".join((content or "").casefold().split())
        text = text.replace("**", "").replace("__", "")
        return bool(re.search(
            r"(?:image\s+(?:g[ée]n[ée]r[ée]e?|generee|pr[ée]te|prete|"
            r"termin[ée]e?|finie)|r[ée]ponds?\s+[àa]\s+cette\s+image|"
            r"(?:modify:|png:)\s*(?:ton|votre|le|la)?)",
            text,
            re.I,
        ))

    @staticmethod
    def _is_progress(content):
        """Retourne True uniquement pour les messages de progression.

        Une rÃ©ponse normale peut parler d'une image "gÃ©nÃ©rÃ©e" ou d'une
        "crÃ©ation". Ces mots seuls ne doivent jamais masquer une rÃ©ponse
        finale : seuls les courts messages indiquant explicitement une attente
        ou un pourcentage sont traitÃ©s comme une progression.
        """
        text = " ".join((content or "").split())
        if not text:
            return False

        # Une vraie rÃ©ponse peut exceptionnellement citer un pourcentage ;
        # les statuts de gÃ©nÃ©ration, eux, restent volontairement courts.
        if len(text) > 320:
            return False

        return bool(re.search(
            r"(?:\b\d{1,3}\s*%|demande\s+en\s+attente|request\s+pending|"
            r"r[ée]flexion\s+en\s+cours|\bthinking\b|\bqueued?\b|\bqueue\b|"
            r"(?:g[ée]n[ée]ration|generation|cr[ée]ation|creation|image)"
            r".{0,80}(?:en\s+cours|charg|processing|render|patiente|attend))",
            text,
            re.I,
        ))

    def _publish(self, job_id, event, final=False):
        result_handler = None
        result_context = None

        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job["events"].put(event)
            if final:
                text_result_timer = job.get("text_result_timer")
                if text_result_timer:
                    text_result_timer.cancel()
                    job["text_result_timer"] = None
                result_handler = self._result_handler
                result_context = (
                    job["username"],
                    job["conversation_id"],
                )
                for channel_id, active_job_id in list(self._channel_jobs.items()):
                    if active_job_id == job_id:
                        del self._channel_jobs[channel_id]
                cleanup_timer = threading.Timer(
                    900,
                    self._forget_job,
                    args=(job_id,)
                )
                cleanup_timer.daemon = True
                cleanup_timer.start()

        if result_handler and result_context:
            result_handler(
                result_context[0],
                result_context[1],
                event,
            )

    def _forget_job(self, job_id):
        with self._lock:
            self._jobs.pop(job_id, None)

    def _timeout_job(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)

            if not job or job_id not in self._channel_jobs.values():
                return

        self._publish(
            job_id,
            {
                "type": "error",
                "message": (
                    "Le bot Discord n'a pas donné de résultat final. "
                    "Réessaie dans quelques instants."
                ),
            },
            final=True,
        )

    def _load_conversations(self):
        try:
            return json.loads(self.store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_conversations(self):
        temporary = self.store_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._conversations, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.store_path)

    def _load_image_messages(self):
        try:
            return json.loads(self.image_store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_image_messages(self):
        temporary = self.image_store_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._image_messages, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.image_store_path)
