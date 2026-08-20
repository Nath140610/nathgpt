NathGPT - version inscription/connexion

Structure :
- server.py
- logo.png              <-- ajoute ton logo ici
- requirements.txt
- templates/
  - login.html
  - register.html
  - chat.html
- static/
  - style.css
- data/                  <-- créé/utilisé automatiquement
  - users.json
  - tokens.json
  - secret_key.txt

Installation :
    pip install -r requirements.txt

Lancement :
    python server.py

Puis ouvre :
    http://127.0.0.1:5000

IMPORTANT :
ALLOW_IP_AUTOLOGIN = True dans server.py permet la reconnexion automatique
par IP lorsqu'une seule personne est associée à cette IP.

Le cookie remember_token est également utilisé et est plus fiable/sûr que l'IP.

Pour un site public en HTTPS :
- passe SESSION_COOKIE_SECURE=True
- passe secure=True dans set_cookie()
- il est recommandé de mettre ALLOW_IP_AUTOLOGIN=False
