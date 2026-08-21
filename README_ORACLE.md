# NathGPT FINAL MEMORY — Oracle Cloud

Cette version conserve tous les correctifs précédents et ajoute :

- mémoire persistante indépendante pour chaque salon Discord de la catégorie `1539922989200576512` ;
- support des pièces jointes Discord (images, PDF, texte, code et autres fichiers acceptés par ChatGPT) ;
- annulation automatique des générations bloquées ;
- `retry:` pour relancer la dernière demande du salon ;
- statut Discord dynamique : `Disponible`, `1 generation en cours`, ou le nombre de demandes en attente ;
- détection renforcée de l'image finale ;
- `modify:` et `png:` en réponse aux images ;
- mise à jour automatique de la VM avec le BAT fourni.

La mémoire des salons est sauvegardée sur la VM dans :
`/home/ubuntu/NathGPT_V11_Oracle/channel_memory.json`

Le BAT de mise à jour ne remplace pas `.env`, le profil Chromium ni ce fichier de mémoire.


### Correctif image de base / decomp_cricut
- Le mode `decomp_cricut` refuse maintenant l'image source si ChatGPT la renvoie par erreur.
- Le bot attend un **nouveau bloc image** et non plus l'image d'origine.
- Si la source est encore détectée, il retente automatiquement jusqu'à 3 fois avant d'abandonner proprement.


### Correctif timeout `:decomp_cricut`
- Le comptage initial des stickers utilise maintenant un délai plus long (`decomp_count` : 600s par défaut).
- Les générations individuelles utilisent aussi un type dédié (`decomp_image` : 480s par défaut).
- Le comptage est retenté automatiquement avec un second prompt si ChatGPT ne renvoie pas un nombre exploitable.
- Chaque commande `:decomp_cricut` utilise une conversation de comptage isolée pour éviter qu'un ancien contexte bloque la nouvelle demande.


### Nettoyage automatique de la RAM
Cette version redémarre complètement Chromium toutes les 2 générations
terminées avec succès. Le profil Chromium persistant n'est pas supprimé,
donc la session ChatGPT reste conservée.

Variable optionnelle :
`NATHGPT_RESTART_CHROMIUM_EVERY=2`
