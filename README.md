# NAS Move

Déplace un dossier (et tous ses sous-dossiers) d'un NAS vers un autre, depuis macOS, avec une interface graphique.

## Lancer
- Monter les deux NAS dans le Finder (**Aller > Se connecter au serveur…**, `smb://…`). Ils apparaissent dans `/Volumes`.
- Double-clic sur `NasMove.command` (ou `python3 -m nas_move` dans le Terminal).
- Si Tk manque : `brew install python-tk` (le Python de python.org l'inclut).

## Si « Choisir… » ne marche pas
- Le sélecteur utilise la boîte de dialogue native de macOS. Au premier accès à un NAS, macOS demande l'autorisation : accepter.
- Sinon : **Réglages Système > Confidentialité et sécurité > Accès aux fichiers et dossiers** (ou *Accès complet au disque*) et autoriser **Terminal**.
- En dernier recours, saisir le chemin à la main dans le champ (ex. `/Volumes/NAS1/MonDossier`).

## Utilisation
1. **Source** : le dossier à déplacer. **Destination** : le dossier où il sera placé (`Destination/<nom du dossier>/…`).
2. **Simulation** d'abord : liste ce qui serait fait, sans rien modifier.
3. **Démarrer** : copie → vérification (SHA-256) → suppression du fichier source.

## Si ça plante
- Un fichier n'est supprimé de la source qu'**après** vérification : rien n'est perdu.
- Relancer l'application : elle propose de **reprendre** le déplacement interrompu (mêmes dossiers, mêmes options). Seul le reste est traité.
- Si un NAS se déconnecte pendant le transfert, l'application **attend** sa reconnexion puis continue seule.
- Les fichiers partiels (`*.nasmove.part`) sont nettoyés à la reprise.

## Sécurités
- Refuse : source = destination, destination dans la source, racine d'un volume, volume non monté (évite d'écrire sur le disque du Mac par erreur).
- Empêche la mise en veille du Mac pendant le transfert (`caffeinate`).
- Journal : `~/Library/Application Support/NasMove/nasmove.log`.

## Tests (moteur)
```
python3 -m unittest discover -s tests -t .
```
