# NAS Move — contexte pour Claude Code (Mac local)

App Python (stdlib seule, tkinter) : déplace un dossier + sous-dossiers d'un NAS à un autre, sous macOS.
Lancer : `python3 -m nas_move` — Tests : `python3 -m unittest discover -s tests -t .`
Langue : répondre en français, réponses courtes, listes à puces.

## Règles non négociables
- Jamais supprimer un fichier source sans copie + vérification réussie (SHA-256 par défaut).
- Refuser : source = destination, destination dans la source, dest/<nom> == source, racine d'un volume, volume non monté.
- Ne jamais écrire dans `/Volumes/X` si X n'est pas monté (`os.path.ismount`).
- Copie via `<fichier>.nasmove.part` puis `os.replace`. Le `.part` est conservé après erreur E/S/arrêt (reprise dans le fichier), supprimé si la vérification échoue.

## Architecture
- `nas_move/core.py` : moteur (Options, Mover, Summary, preflight, validate, état de reprise JSON dans `~/Library/Application Support/NasMove/last_job.json`).
- `nas_move/gui.py` : interface tkinter (sélecteur natif via osascript, barres globale + fichier, témoin d'activité, bouton « Vérifier les NAS »). Piège déjà rencontré : ne pas nommer de méthode `_options` (écrase Tk).
- `tests/test_core.py` : tests du moteur (tmp dirs).
- Journal : `~/Library/Application Support/NasMove/nasmove.log`.

## État
Fait : move + vérif, reprise de job, reprise dans un gros fichier, contrôle des NAS au départ et après erreur E/S, suivi d'activité en direct, fsync tolérant.
Environnement de l'utilisateur : Mac, Python 3.15 Homebrew + python-tk, NAS en SMB (`/Volumes/Backup`, `/Volumes/photo`), NAS lents mais pas en panne.

## À faire (par priorité)
1. Élucider les erreurs vues sur `Backup/Video Google archive 24/Photos.zip` (~19 Go) : `Errno 5` puis `Errno 22` (le journal indique maintenant l'étape `[lecture source|écriture destination|fsync|...]`). Tester en réel.
2. Verrou d'instance unique (deux fenêtres sur les mêmes dossiers).
3. Valider l'interface sur le vrai Mac (jamais exécutée dans le cloud).
4. Copie en parallèle, rapport CSV, exclusions réglables, métadonnées macOS (xattr, dates de création), noms NFC/NFD.
5. Cahier des charges + critères de validation dans le README.
