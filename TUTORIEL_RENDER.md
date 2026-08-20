# Mettre NathGPT en ligne avec Render

Ce guide met en ligne le site NathGPT, le relais Discord et l'historique des
conversations.

## 1. Préparer le bot relais Discord

Le token `DISCORD_TOKEN` doit appartenir au bot qui crée les salons et envoie
les demandes. Ce bot est distinct du bot qui génère les images, dont l'ID est
déjà configuré dans le projet : `1539359893063209053`.

1. Va sur [Discord Developer Portal](https://discord.com/developers/applications).
2. Crée une application, puis ouvre l'onglet **Bot** et crée son bot.
3. Dans **Privileged Gateway Intents**, active **Message Content Intent**.
4. Copie le token du bot avec **Reset Token** puis **Copy**.
5. Invite le bot dans le même serveur Discord que le bot générateur.

Le bot relais doit avoir, dans la catégorie `1539922989200576512`, ces
permissions :

- Voir les salons
- Lire l'historique des messages
- Envoyer des messages
- Gérer les salons

Ne partage jamais le token. S'il est exposé, utilise immédiatement **Reset
Token** dans le portail Discord.

## 2. Mettre le projet sur GitHub

1. Crée un nouveau dépôt GitHub privé.
2. Envoie le dossier complet `NathGpt_WEB` dans ce dépôt.
3. Vérifie que `.env` n'est pas envoyé : il contient une place réservée pour
   ton token et est exclu par `.gitignore`.
4. Vérifie que `render.yaml` est bien présent à la racine du dépôt.

## 3. Créer le service Render

1. Connecte-toi sur [Render](https://dashboard.render.com/).
2. Clique sur **New** puis **Blueprint**.
3. Connecte ton compte GitHub et sélectionne le dépôt NathGPT.
4. Render détecte automatiquement `render.yaml`.
5. Garde le plan **Starter** : il est nécessaire pour le disque persistant qui
   conserve comptes, conversations et résultats après un redéploiement.
6. Clique sur **Apply** ou **Create New Resources**.

## 4. Ajouter le token Discord dans Render

Pendant la création du Blueprint, Render demande la valeur de
`DISCORD_TOKEN`.

1. Colle le token du bot relais Discord.
2. Ne mets pas de guillemets autour du token.
3. Valide, puis lance le déploiement.

Si le service existe déjà : ouvre-le dans Render, va dans **Environment**, crée
ou modifie `DISCORD_TOKEN`, enregistre, puis utilise **Manual Deploy** pour
redéployer.

Les autres variables sont déjà prêtes :

| Variable | Rôle |
| --- | --- |
| `NATHGPT_SECRET_KEY` | Sécurise les sessions ; Render la génère. |
| `NATHGPT_DATA_DIR` | Pointe vers le disque persistant Render. |
| `SESSION_COOKIE_SECURE` | Active les cookies HTTPS. |
| `DISCORD_CATEGORY_ID` | Catégorie où créer les salons de discussion. |
| `DISCORD_TARGET_BOT_ID` | Bot Discord qui répond et génère les images. |

## 5. Vérifier le déploiement

Quand le déploiement est terminé :

1. Ouvre l'URL `https://ton-service.onrender.com/` donnée par Render.
2. Crée un compte et envoie une demande.
3. Vérifie qu'un salon apparaît dans la catégorie Discord configurée.
4. Vérifie dans les logs Render la ligne `Bot Discord connecté`.
5. Envoie une demande d'image et vérifie que le résultat arrive dans le site.

Tu peux aussi ouvrir `https://ton-service.onrender.com/health`. La réponse doit
être :

```json
{"status":"ok"}
```

## Dépannage

### « Discord n'est pas configuré : DISCORD_TOKEN est manquant »

Ajoute `DISCORD_TOKEN` dans **Environment** sur Render, puis redéploie.

### Le bot ne crée pas de salon

Vérifie que le bot relais est dans le bon serveur Discord, que l'ID de la
catégorie est correct et que la permission **Gérer les salons** est accordée.

### Le bot reçoit la question mais le site ne reçoit rien

Vérifie que **Message Content Intent** est activé pour le bot relais. Vérifie
aussi que le bot générateur est bien celui dont l'ID est
`1539359893063209053` et qu'il répond dans le même salon.

### Les conversations disparaissent après un redéploiement

Vérifie que le service utilise le plan Starter et que le disque persistant
`nathgpt-data` est bien attaché. Ne supprime pas ce disque dans Render.
