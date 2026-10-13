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

## Contrôle des NAS (automatique au départ)
- Bouton **Vérifier les NAS** : source et destination sont-elles montées ? répondent-elles ? la destination accepte-t-elle l'écriture (fichier test de 256 Ko écrit, relu, supprimé) ? Affiche le type de montage (ex. `smbfs`) et les temps de réponse.
- Les mêmes contrôles tournent avant chaque Simulation/Démarrer/Reprise, plus l'espace libre sur la destination.
- Pas de faux positif « NAS lent » : un NAS lent passe tant qu'il répond (délai max 60 s). Seule une absence de réponse ou une erreur bloque.
- Après une erreur d'E/S pendant le transfert, le programme vérifie que les deux NAS répondent encore et attend leur retour avant de réessayer.

## Si ça plante
- Un fichier n'est supprimé de la source qu'**après** vérification : rien n'est perdu.
- Relancer l'application : elle propose de **reprendre** le déplacement interrompu (mêmes dossiers, mêmes options). Seul le reste est traité.
- Si un NAS se déconnecte pendant le transfert, l'application **attend** sa reconnexion puis continue seule.
- **Reprise au milieu d'un gros fichier** : en cas d'erreur réseau, d'arrêt ou de plantage, le fichier partiel (`*.nasmove.part`) est conservé. Au réessai, le début déjà copié est comparé à la source bloc par bloc (seule la partie identique est gardée), puis la copie continue là où elle s'était arrêtée. Le SHA-256 final porte sur le fichier entier.
- Une copie dont la vérification échoue est supprimée et recommence de zéro. Les `.part` dont le fichier source n'existe plus sont nettoyés.

## Sécurités
- Refuse : source = destination, destination dans la source, racine d'un volume, volume non monté (évite d'écrire sur le disque du Mac par erreur).
- Empêche la mise en veille du Mac pendant le transfert (`caffeinate`).
- Journal : `~/Library/Application Support/NasMove/nasmove.log`.

## Tests (moteur)
```
python3 -m unittest discover -s tests -t .
```
