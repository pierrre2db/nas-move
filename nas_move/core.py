"""Moteur de déplacement NAS1 -> NAS2 (sans dépendance, sans interface).

Principe : copier -> vérifier -> supprimer la source. Comme un fichier n'est
supprimé de la source qu'après vérification, la reprise après un plantage est
simplement « relancer sur les mêmes dossiers » : ce qui reste dans la source
est ce qui reste à faire.
"""
from __future__ import annotations

import errno
import fnmatch
import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("nas_move")

CHUNK = 4 * 1024 * 1024
RETRIES = 3
PART_SUFFIX = ".nasmove.part"
DEFAULT_EXCLUDES = [".DS_Store", "._*"]
STATE_DIR = Path.home() / "Library" / "Application Support" / "NasMove"
STATE_FILE = STATE_DIR / "last_job.json"


class Cancelled(Exception):
    pass


class VerifyError(OSError):
    pass


@dataclass
class Options:
    src: str
    dst: str
    verify: str = "sha256"  # "size" | "sha256"
    on_conflict: str = "skip"  # "skip" | "overwrite" | "rename"
    keep_source: bool = False
    excludes: list = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    dry_run: bool = False


@dataclass
class Summary:
    total_files: int = 0
    total_bytes: int = 0
    moved: int = 0
    skipped: int = 0
    errors: list = field(default_factory=list)
    bytes_done: int = 0
    cancelled: bool = False
    seconds: float = 0.0
    # activité en direct (fichier en cours)
    current: str = ""
    phase: str = ""
    file_done: int = 0
    file_size: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors and not self.cancelled


# --------------------------------------------------------------------------
# Volumes macOS (/Volumes/NomDuNAS)
# --------------------------------------------------------------------------
def volume_root(path) -> Optional[Path]:
    parts = Path(path).parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        return Path("/", "Volumes", parts[2])
    return None


def volume_connected(path) -> bool:
    """Vrai si le chemin n'est pas sur un volume, ou si ce volume est monté.

    Important : un NAS démonté laisse parfois un dossier vide dans /Volumes ;
    y écrire remplirait le disque du Mac au lieu du NAS.
    """
    root = volume_root(path)
    return root is None or (root.is_dir() and os.path.ismount(root))


@dataclass
class Check:
    label: str
    ok: bool
    detail: str = ""


def run_with_timeout(fn: Callable, timeout: float):
    """Exécute fn dans un fil : un NAS bloqué ne doit pas figer le programme."""
    box: dict = {}

    def target():
        try:
            box["v"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["e"] = e

    th = threading.Thread(target=target, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        raise TimeoutError(f"pas de réponse après {int(timeout)} s")
    if "e" in box:
        raise box["e"]
    return box.get("v")


def mount_info(path) -> Optional[str]:
    """Ex. '//user@192.168.1.5/Backup (smbfs)' pour un volume monté, sinon None."""
    root = volume_root(path)
    if root is None:
        return None
    try:
        out = subprocess.run(["mount"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if f" on {root} (" in line:
            src, rest = line.split(f" on {root} (", 1)
            return f"{src} ({rest.split(',')[0].rstrip(')')})"
    return None


def responsive(path, timeout: float = 30) -> bool:
    try:
        run_with_timeout(lambda: os.listdir(path), timeout)
        return True
    except (OSError, TimeoutError):
        return False


def check_location(path, label: str, write: bool, timeout: float = 60) -> list[Check]:
    """Vérifie qu'un emplacement est monté, répond, et (destination) accepte l'écriture."""
    checks: list[Check] = []
    root = volume_root(path)
    if root is not None:
        mounted = volume_connected(path)
        info = mount_info(path) if mounted else None
        checks.append(Check(f"{label} : volume {root.name} monté", mounted,
                            info or ("" if mounted else "non monté : le connecter dans le Finder")))
        if not mounted:
            return checks
    t0 = time.monotonic()
    try:
        n = run_with_timeout(lambda: len(os.listdir(path)), timeout)
        checks.append(Check(f"{label} : lecture du dossier", True,
                            f"{n} éléments, réponse en {time.monotonic() - t0:.1f} s"))
    except (OSError, TimeoutError) as e:
        checks.append(Check(f"{label} : lecture du dossier", False, str(e)))
        return checks
    if write:
        probe = Path(path) / f".nasmove_test_{os.getpid()}"
        data = os.urandom(256 * 1024)

        def roundtrip():
            with open(probe, "wb") as f:
                f.write(data)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            ok = probe.read_bytes() == data
            probe.unlink()
            return ok

        t0 = time.monotonic()
        try:
            ok = run_with_timeout(roundtrip, timeout)
            checks.append(Check(f"{label} : écriture + relecture", bool(ok),
                                f"256 Ko en {time.monotonic() - t0:.1f} s" if ok else "données relues différentes"))
        except (OSError, TimeoutError) as e:
            checks.append(Check(f"{label} : écriture + relecture", False, str(e)))
            try:
                probe.unlink()
            except OSError:
                pass
    return checks


def preflight(opts: "Options") -> list[Check]:
    """Contrôles de départ : les deux NAS sont-ils connectés et fonctionnels ?"""
    checks = check_location(opts.src, "Source", write=False)
    checks += check_location(opts.dst, "Destination", write=not opts.dry_run)
    return checks


def save_state(opts: Options, status: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        data = {"status": status, "time": time.time(), "options": asdict(opts)}
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, STATE_FILE)
    except OSError as e:  # l'état est un confort, jamais bloquant
        log.warning("Impossible d'écrire l'état de reprise : %s", e)


def load_interrupted() -> Optional[Options]:
    """Options du dernier job s'il ne s'est pas terminé proprement."""
    try:
        data = json.loads(STATE_FILE.read_text())
        if data.get("status") == "finished":
            return None
        return Options(**data["options"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


# --------------------------------------------------------------------------
# Outils
# --------------------------------------------------------------------------
def unique_name(path: Path) -> Path:
    n = 1
    while True:
        cand = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not cand.exists():
            return cand
        n += 1


def validate(opts: Options) -> list[str]:
    """Retourne la liste des problèmes bloquants (vide = OK)."""
    problems = []
    src, dst = Path(opts.src), Path(opts.dst)
    if not src.is_dir():
        problems.append(f"Source introuvable : {src}")
    if not dst.is_dir():
        problems.append(f"Destination introuvable : {dst}")
    if problems:
        return problems
    for p in (src, dst):
        if not volume_connected(p):
            problems.append(f"Volume non monté : {volume_root(p)}")
    if src.parent == src or volume_root(src) == src.resolve():
        problems.append("La source ne peut pas être la racine d'un disque/NAS.")
    rs, rd = src.resolve(), dst.resolve()
    if rs == rd or rs in rd.parents:
        problems.append("La destination ne peut pas être dans la source.")
    if opts.verify not in ("size", "sha256"):
        problems.append("Mode de vérification invalide.")
    if opts.on_conflict not in ("skip", "overwrite", "rename"):
        problems.append("Politique de conflit invalide.")
    return problems


# --------------------------------------------------------------------------
# Moteur
# --------------------------------------------------------------------------
class Mover:
    def __init__(
        self,
        opts: Options,
        on_progress: Callable[[Summary, str], None] = lambda s, cur: None,
        on_status: Callable[[str], None] = lambda msg: None,
    ):
        self.opts = opts
        self.src_root = Path(opts.src)
        # On déplace le dossier lui-même : dst/<nom_du_dossier>/...
        self.dst_root = Path(opts.dst) / self.src_root.name
        self.on_progress = on_progress
        self.on_status = on_status
        self.summary = Summary()
        self.n_excluded = 0
        self._base = 0
        self._last_tick = 0.0
        self._cancel = threading.Event()
        self._running = threading.Event()
        self._running.set()

    # -- contrôle ----------------------------------------------------------
    def cancel(self) -> None:
        self._cancel.set()
        self._running.set()

    def pause(self) -> None:
        self._running.clear()

    def resume(self) -> None:
        self._running.set()

    def _checkpoint(self) -> None:
        if self._cancel.is_set():
            raise Cancelled
        if not self._running.is_set():
            self.on_status("En pause")
            self._running.wait()
            if self._cancel.is_set():
                raise Cancelled

    def _wait_volumes(self, deep: bool = False) -> None:
        """Bloque tant qu'un NAS est démonté ou muet (reprend seul à la reconnexion).

        deep=True : après une erreur E/S, vérifie aussi que chaque NAS répond.
        """
        def healthy() -> bool:
            if not (volume_connected(self.src_root) and volume_connected(self.dst_root)):
                return False
            if deep:
                return responsive(self.src_root) and responsive(Path(self.opts.dst))
            return True

        warned = False
        while not healthy():
            if not warned:
                msg = "NAS déconnecté ou sans réponse : en attente de reconnexion…"
                log.warning(msg)
                self.on_status(msg)
                warned = True
            self._checkpoint()
            time.sleep(3)
        if warned:
            log.info("Volume reconnecté, reprise.")
            self.on_status("Reconnecté, reprise")

    def _tick(self, phase: str, done: int, force: bool = False) -> None:
        """Signale l'activité en cours (limité à 4 fois/s)."""
        now = time.monotonic()
        if force or now - self._last_tick >= 0.25:
            self._last_tick = now
            self.summary.phase = phase
            self.summary.file_done = done
            self.on_progress(self.summary, self.summary.current)

    def _hash(self, path: Path, phase: str) -> str:
        h = hashlib.sha256()
        done = 0
        self._tick(phase, 0, force=True)
        with open(path, "rb") as f:
            while chunk := f.read(CHUNK):
                self._checkpoint()
                h.update(chunk)
                done += len(chunk)
                self._tick(phase, done)
        return h.hexdigest()

    # -- scan --------------------------------------------------------------
    def _excluded(self, name: str) -> bool:
        return any(fnmatch.fnmatch(name, pat) for pat in self.opts.excludes)

    def scan(self) -> tuple[list[tuple[str, int]], list[str]]:
        files: list[tuple[str, int]] = []
        dirs: list[str] = []
        def on_error(e: OSError) -> None:
            # os.walk ignore les erreurs par défaut : un NAS sans autorisation
            # macOS paraîtrait simplement vide.
            log.error("Lecture impossible %s : %s", e.filename, e)
            self.summary.errors.append((str(e.filename), str(e)))

        for root, dnames, fnames in os.walk(self.src_root, onerror=on_error):
            self._checkpoint()
            rel_root = os.path.relpath(root, self.src_root)
            rel_root = "" if rel_root == "." else rel_root
            dirs.append(rel_root)
            for name in fnames:
                if self._excluded(name):
                    self.n_excluded += 1
                    continue
                rel = os.path.join(rel_root, name)
                try:
                    files.append((rel, os.path.getsize(self.src_root / rel)))
                except OSError as e:
                    log.warning("Illisible (ignoré) %s : %s", rel, e)
        return files, dirs

    # -- un fichier --------------------------------------------------------
    def _same(self, s: Path, d: Path) -> bool:
        if s.stat().st_size != d.stat().st_size:
            return False
        if self.opts.verify == "size":
            return True
        return self._hash(s, "Comparaison : source") == self._hash(d, "Comparaison : destination")

    def _copy(self, s: Path, d: Path) -> int:
        d.parent.mkdir(parents=True, exist_ok=True)
        tmp = d.with_name(d.name + PART_SUFFIX)
        h = hashlib.sha256()
        size = 0
        stage = "ouverture"
        try:
            with open(s, "rb") as fin, open(tmp, "wb") as fout:
                while True:
                    stage = "lecture source"
                    chunk = fin.read(CHUNK)
                    if not chunk:
                        break
                    self._checkpoint()
                    stage = "écriture destination"
                    fout.write(chunk)
                    h.update(chunk)
                    size += len(chunk)
                    self.summary.bytes_done = self._base + size
                    self._tick("Copie", size, force=size == len(chunk))
                fout.flush()
                stage = "fsync"
                try:
                    os.fsync(fout.fileno())
                except OSError as e:
                    # Certains partages SMB/NAS refusent fsync : sans gravité.
                    if e.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP):
                        raise
            stage = "vérification"
            if tmp.stat().st_size != size:
                raise VerifyError(f"Taille incorrecte après copie : {d}")
            if self.opts.verify == "sha256" and self._hash(tmp, "Vérification") != h.hexdigest():
                raise VerifyError(f"Checksum différent après copie : {d}")
            stage = "renommage"
            os.replace(tmp, d)
            try:
                os.utime(d, ns=(s.stat().st_atime_ns, s.stat().st_mtime_ns))
            except OSError:
                pass  # certains NAS refusent la modification des dates
        except OSError as e:
            if not isinstance(e, VerifyError) and e.strerror:
                e.strerror = f"[{stage}, {size} octets copiés] {e.strerror}"
            raise
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        return size

    def _move_file(self, rel: str, size: int) -> str:
        """Retourne 'moved' ou 'skipped'."""
        s, d = self.src_root / rel, self.dst_root / rel
        if d.exists():
            if self._same(s, d):  # déjà copié avant un plantage
                if not self.opts.keep_source:
                    s.unlink()
                return "moved"
            if self.opts.on_conflict == "skip":
                return "skipped"
            if self.opts.on_conflict == "rename":
                d = unique_name(d)
        self._copy(s, d)
        if not self.opts.keep_source:
            s.unlink()
        return "moved"

    def _process(self, rel: str, size: int) -> None:
        for attempt in range(1, RETRIES + 1):
            self._wait_volumes()
            try:
                result = self._move_file(rel, size)
                if result == "moved":
                    self.summary.moved += 1
                    log.info("OK %s (%d octets)", rel, size)
                else:
                    self.summary.skipped += 1
                    log.info("Ignoré (existe déjà, différent) : %s", rel)
                return
            except Cancelled:
                raise
            except OSError as e:
                log.warning("Échec %s (essai %d/%d) : %s", rel, attempt, RETRIES, e)
                self._wait_volumes(deep=True)  # NAS toujours joignables ?
                if attempt == RETRIES:
                    self.summary.errors.append((rel, str(e)))
                    return
                time.sleep(2 * attempt)

    # -- nettoyage ---------------------------------------------------------
    def _remove_stale_parts(self) -> None:
        if not self.dst_root.is_dir():
            return
        for root, _, fnames in os.walk(self.dst_root):
            for name in fnames:
                if name.endswith(PART_SUFFIX):
                    try:
                        os.remove(os.path.join(root, name))
                        log.info("Fichier partiel supprimé : %s", name)
                    except OSError:
                        pass

    def _remove_empty_dirs(self) -> None:
        for root, _, fnames in os.walk(self.src_root, topdown=False):
            for name in fnames:
                if self._excluded(name):
                    try:
                        os.remove(os.path.join(root, name))
                    except OSError:
                        pass
            try:
                os.rmdir(root)  # échoue si non vide : voulu
            except OSError:
                pass

    # -- run ---------------------------------------------------------------
    def run(self) -> Summary:
        t0 = time.time()
        sm = self.summary
        log.info("Démarrage%s : %s -> %s (vérif=%s, conflit=%s)",
                 " (simulation)" if self.opts.dry_run else "", self.opts.src, self.opts.dst,
                 self.opts.verify, self.opts.on_conflict)
        problems = validate(self.opts)
        if problems:
            for p in problems:
                log.error(p)
            sm.errors.extend(("", p) for p in problems)
            return sm
        try:
            self.on_status("Vérification des NAS…")
            checks = preflight(self.opts)
            for c in checks:
                log.log(logging.INFO if c.ok else logging.ERROR, "%s %s : %s", "✔" if c.ok else "✘", c.label, c.detail)
            failed = [c for c in checks if not c.ok]
            if failed:
                sm.errors.extend(("", f"{c.label} : {c.detail}") for c in failed)
                return sm
            self.on_status("Analyse de la source…")
            files, dirs = self.scan()
            sm.total_files = len(files)
            sm.total_bytes = sum(sz for _, sz in files)
            log.info("%d fichiers, %d octets à traiter (%d dossiers parcourus, %d fichiers exclus)",
                     sm.total_files, sm.total_bytes, len(dirs), self.n_excluded)
            free = shutil.disk_usage(self.opts.dst).free
            log.info("Espace libre destination : %d octets", free)
            if files and free < sm.total_bytes and not self.opts.dry_run:
                msg = f"Espace insuffisant sur la destination : {free} octets libres, {sm.total_bytes} nécessaires."
                log.error(msg)
                sm.errors.append(("", msg))
                return sm
            if not files:
                msg = (f"Aucun fichier trouvé ({len(dirs)} dossiers parcourus, {self.n_excluded} fichiers exclus). "
                       "Dossier vide, ou accès refusé par macOS : "
                       "Réglages Système > Confidentialité et sécurité > Accès aux fichiers et dossiers "
                       "(ou Accès complet au disque) > autoriser Terminal.")
                log.error(msg)
                sm.errors.append(("", msg))
            if self.opts.dry_run:
                for rel, _ in files:
                    d = self.dst_root / rel
                    if d.exists():
                        sm.skipped += 1
                    else:
                        sm.moved += 1
                sm.seconds = time.time() - t0
                return sm

            save_state(self.opts, "running")
            self._remove_stale_parts()
            for rel in dirs:
                self._wait_volumes()
                (self.dst_root / rel).mkdir(parents=True, exist_ok=True)

            self.on_status("Déplacement en cours")
            for rel, size in files:
                self._checkpoint()
                self._base = sm.bytes_done
                sm.current, sm.file_size, sm.file_done, sm.phase = rel, size, 0, "Préparation"
                self._tick("Préparation", 0, force=True)
                self._process(rel, size)
                sm.bytes_done = self._base + size
            sm.current, sm.phase, sm.file_done, sm.file_size = "", "Nettoyage", 0, 0
            self.on_progress(sm, "")
            if not self.opts.keep_source:
                self._remove_empty_dirs()
            save_state(self.opts, "finished" if not sm.errors else "running")
        except Cancelled:
            sm.cancelled = True
            save_state(self.opts, "cancelled")
        sm.seconds = time.time() - t0
        return sm
