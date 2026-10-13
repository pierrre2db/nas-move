"""Interface graphique macOS (tkinter, fournie avec Python)."""
from __future__ import annotations

import collections
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from logging.handlers import RotatingFileHandler

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import core

VOLUMES = "/Volumes" if os.path.isdir("/Volumes") else os.path.expanduser("~")
VERIFY = {"Complète (SHA-256, recommandé)": "sha256", "Rapide (taille)": "size"}
CONFLICT = {"Ignorer": "skip", "Écraser": "overwrite", "Renommer la copie": "rename"}


def fmt_size(n: float) -> str:
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if n < 1024 or unit == "To":
            return f"{n:.1f} {unit}" if unit != "o" else f"{int(n)} o"
        n /= 1024


def fmt_duration(sec: float) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h} h {m:02d} min" if h else (f"{m} min {s:02d} s" if m else f"{s} s")


def setup_logging(q: queue.Queue) -> None:
    core.STATE_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    fh = RotatingFileHandler(core.STATE_DIR / "nasmove.log", maxBytes=2_000_000, backupCount=3)
    fh.setFormatter(fmt)

    class QH(logging.Handler):
        def emit(self, record):
            q.put(("log", self.format(record)))

    qh = QH()
    qh.setFormatter(fmt)
    lg = logging.getLogger("nas_move")
    lg.setLevel(logging.INFO)
    lg.addHandler(fh)
    lg.addHandler(qh)
    lg.info("NAS Move démarré (journal : %s)", core.STATE_DIR / "nasmove.log")


def choose_folder(start: str) -> str | None:
    """Sélecteur de dossier : boîte native macOS (osascript), repli sur Tk."""
    if shutil.which("osascript"):
        loc = start if os.path.isdir(start) else VOLUMES
        script = (
            'POSIX path of (choose folder with prompt "Choisir un dossier" '
            f'default location (POSIX file "{loc}"))'
        )
        try:
            r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        except OSError:
            r = None
        if r is not None:
            if r.returncode == 0:
                return r.stdout.strip().rstrip("/") or "/"
            if "-128" in r.stderr:  # annulé par l'utilisateur
                return None
            logging.getLogger("nas_move").warning("osascript : %s", r.stderr.strip())
    try:
        return filedialog.askdirectory(initialdir=start if os.path.isdir(start) else VOLUMES) or None
    except tk.TclError as e:
        messagebox.showerror("Sélecteur indisponible", f"{e}\n\nSaisissez le chemin à la main (ex. /Volumes/NAS1/dossier).")
        return None


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("NAS Move")
        self.minsize(720, 560)
        self.q: queue.Queue = queue.Queue()
        setup_logging(self.q)
        self.mover: core.Mover | None = None
        self.worker: threading.Thread | None = None
        self.caffeinate: subprocess.Popen | None = None
        self.paused = False
        self.t0 = 0.0
        self.last_progress = 0.0
        self.running = False
        self.samples: collections.deque = collections.deque(maxlen=80)  # (t, octets de la phase)
        self.sample_key = None

        self.src = tk.StringVar()
        self.dst = tk.StringVar()
        self.verify = tk.StringVar(value=list(VERIFY)[0])
        self.conflict = tk.StringVar(value=list(CONFLICT)[0])
        self.keep = tk.BooleanVar(value=False)
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll)
        self.after(400, self._offer_resume)
        self.after(1000, self._heartbeat)

    # -- construction ------------------------------------------------------
    def _build(self) -> None:
        pad = {"padx": 10, "pady": 4}
        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)

        for row, (label, var) in enumerate((("Source (NAS 1)", self.src), ("Destination (NAS 2)", self.dst))):
            ttk.Label(f, text=label).grid(row=row, column=0, sticky="w", **pad)
            ttk.Entry(f, textvariable=var).grid(row=row, column=1, sticky="ew", **pad)
            ttk.Button(f, text="Choisir…", command=lambda v=var: self._pick(v)).grid(row=row, column=2, **pad)

        ttk.Label(f, text="Vérification").grid(row=2, column=0, sticky="w", **pad)
        ttk.Combobox(f, textvariable=self.verify, values=list(VERIFY), state="readonly").grid(
            row=2, column=1, sticky="ew", **pad)
        ttk.Label(f, text="Si le fichier existe").grid(row=3, column=0, sticky="w", **pad)
        ttk.Combobox(f, textvariable=self.conflict, values=list(CONFLICT), state="readonly").grid(
            row=3, column=1, sticky="ew", **pad)
        ttk.Checkbutton(f, text="Copier seulement (ne pas supprimer la source)",
                        variable=self.keep).grid(row=4, column=1, sticky="w", **pad)

        bar = ttk.Frame(f)
        bar.grid(row=5, column=0, columnspan=3, sticky="ew", pady=8)
        self.btn_check = ttk.Button(bar, text="Vérifier les NAS", command=self._check_nas)
        self.btn_sim = ttk.Button(bar, text="Simulation", command=lambda: self._start(True))
        self.btn_go = ttk.Button(bar, text="Démarrer", command=lambda: self._start(False))
        self.btn_pause = ttk.Button(bar, text="Pause", command=self._toggle_pause, state="disabled")
        self.btn_stop = ttk.Button(bar, text="Arrêter", command=self._stop, state="disabled")
        for b in (self.btn_check, self.btn_sim, self.btn_go, self.btn_pause, self.btn_stop):
            b.pack(side="left", padx=4)

        self.progress = ttk.Progressbar(f, mode="determinate", maximum=100)
        self.progress.grid(row=6, column=0, columnspan=3, sticky="ew", **pad)
        self.status = ttk.Label(f, text="Prêt")
        self.status.grid(row=7, column=0, columnspan=3, sticky="w", **pad)
        self.fprogress = ttk.Progressbar(f, mode="determinate", maximum=100)
        self.fprogress.grid(row=8, column=0, columnspan=3, sticky="ew", **pad)
        self.current = ttk.Label(f, text="", foreground="gray")
        self.current.grid(row=9, column=0, columnspan=3, sticky="w", **pad)
        self.activity = ttk.Label(f, text="")
        self.activity.grid(row=10, column=0, columnspan=3, sticky="w", **pad)

        box = ttk.Frame(f)
        box.grid(row=11, column=0, columnspan=3, sticky="nsew", pady=6)
        f.rowconfigure(11, weight=1)
        self.text = tk.Text(box, height=10, wrap="none", state="disabled")
        sb = ttk.Scrollbar(box, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

    def _pick(self, var: tk.StringVar) -> None:
        d = choose_folder(var.get().strip() or VOLUMES)
        if d:
            var.set(d)

    def _log(self, line: str) -> None:
        self.text.configure(state="normal")
        self.text.insert("end", line + "\n")
        self.text.see("end")
        self.text.configure(state="disabled")

    # -- reprise -----------------------------------------------------------
    def _offer_resume(self) -> None:
        opts = core.load_interrupted()
        if not opts:
            return
        self.src.set(opts.src)
        self.dst.set(opts.dst)
        self.verify.set(next(k for k, v in VERIFY.items() if v == opts.verify))
        self.conflict.set(next(k for k, v in CONFLICT.items() if v == opts.on_conflict))
        self.keep.set(opts.keep_source)
        if messagebox.askyesno(
            "Déplacement interrompu",
            f"Un déplacement n'a pas été terminé :\n\n{opts.src}\n→ {opts.dst}\n\n"
            "Reprendre là où il s'est arrêté ?\n"
            "(Seuls les fichiers restants seront traités.)"):
            self._launch(opts)

    # -- lancement ---------------------------------------------------------
    def _read_options(self, dry: bool) -> core.Options:
        return core.Options(
            src=self.src.get().strip(), dst=self.dst.get().strip(),
            verify=VERIFY[self.verify.get()], on_conflict=CONFLICT[self.conflict.get()],
            keep_source=self.keep.get(), dry_run=dry)

    def _start(self, dry: bool) -> None:
        opts = self._read_options(dry)
        problems = core.validate(opts)
        if problems:
            messagebox.showerror("Impossible de démarrer", "\n".join(problems))
            return
        if not dry and not opts.keep_source and not messagebox.askyesno(
                "Confirmer", "Les fichiers seront SUPPRIMÉS de la source après vérification.\nContinuer ?"):
            return
        self._launch(opts)

    def _launch(self, opts: core.Options) -> None:
        if self.worker and self.worker.is_alive():
            return
        self.mover = core.Mover(
            opts,
            on_progress=lambda s, cur: self.q.put(("progress", (
                s.bytes_done, s.total_bytes, s.moved + s.skipped + len(s.errors), s.total_files,
                cur, s.phase, s.file_done, s.file_size))),
            on_status=lambda m: self.q.put(("status", m)))
        self.t0 = time.time()
        self.paused = False
        self.progress["value"] = 0
        self.fprogress["value"] = 0
        self.samples.clear()
        self.sample_key = None
        self.last_progress = time.time()
        self.running = True
        self._set_running(True)
        self._keep_awake(True)
        self.worker = threading.Thread(target=self._work, daemon=True)
        self.worker.start()

    def _work(self) -> None:
        try:
            summary = self.mover.run()
        except Exception as e:  # filet de sécurité : l'état reste « à reprendre »
            logging.getLogger("nas_move").exception("Erreur inattendue")
            self.q.put(("crash", str(e)))
            return
        self.q.put(("done", summary))

    def _check_nas(self) -> None:
        """Teste source et destination (montage, réponse, écriture) sans rien déplacer."""
        opts = self._read_options(False)
        self.btn_check.configure(state="disabled")
        self.status.configure(text="Vérification des NAS en cours…")

        def work():
            problems = core.validate(opts)
            checks = [] if problems else core.preflight(opts)
            self.q.put(("checked", (problems, checks)))

        threading.Thread(target=work, daemon=True).start()

    def _show_checks(self, problems, checks) -> None:
        self.btn_check.configure(state="normal")
        lines = list(problems) + [f"{'✔' if c.ok else '✘'} {c.label}" + (f" — {c.detail}" if c.detail else "") for c in checks]
        for line in lines:
            self._log(line)
        ok = not problems and all(c.ok for c in checks)
        self.status.configure(text="NAS OK" if ok else "Problème avec un NAS")
        (messagebox.showinfo if ok else messagebox.showerror)(
            "Vérification des NAS", "\n".join(lines) or "Rien à vérifier.")

    # -- contrôles ---------------------------------------------------------
    def _toggle_pause(self) -> None:
        if not self.mover:
            return
        self.paused = not self.paused
        (self.mover.pause if self.paused else self.mover.resume)()
        self.btn_pause.configure(text="Reprendre" if self.paused else "Pause")

    def _stop(self) -> None:
        if self.mover and messagebox.askyesno("Arrêter", "Arrêter le déplacement ?\nVous pourrez le reprendre ensuite."):
            self.mover.cancel()

    def _set_running(self, running: bool) -> None:
        self.btn_check.configure(state="disabled" if running else "normal")
        self.btn_sim.configure(state="disabled" if running else "normal")
        self.btn_go.configure(state="disabled" if running else "normal")
        self.btn_pause.configure(state="normal" if running else "disabled", text="Pause")
        self.btn_stop.configure(state="normal" if running else "disabled")

    def _keep_awake(self, on: bool) -> None:
        """Empêche la mise en veille du Mac pendant le transfert."""
        if on and shutil.which("caffeinate") and not self.caffeinate:
            self.caffeinate = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        elif not on and self.caffeinate:
            self.caffeinate.terminate()
            self.caffeinate = None

    # -- file d'événements -------------------------------------------------
    def _poll(self) -> None:
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "log":
                    self._log(data)
                elif kind == "status":
                    self.status.configure(text=data)
                elif kind == "progress":
                    self._on_progress(*data)
                elif kind == "checked":
                    self._show_checks(*data)
                elif kind == "done":
                    self._finish(data)
                elif kind == "crash":
                    self.running = False
                    self._set_running(False)
                    self._keep_awake(False)
                    messagebox.showerror("Erreur", f"{data}\n\nRelancez l'application pour reprendre.")
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _on_progress(self, done, total, nf, tf, cur, phase, fdone, fsize) -> None:
        now = time.time()
        self.last_progress = now
        self.progress["value"] = 100 * done / total if total else 0
        self.fprogress["value"] = 100 * fdone / fsize if fsize else 0
        # vitesse glissante sur la phase en cours (copie ou vérification)
        key = (cur, phase)
        if key != self.sample_key:
            self.samples.clear()
            self.sample_key = key
        self.samples.append((now, fdone))
        speed = 0.0
        if len(self.samples) > 1:
            (t1, b1), (t2, b2) = self.samples[0], self.samples[-1]
            speed = (b2 - b1) / (t2 - t1) if t2 > t1 else 0.0
        elapsed = max(now - self.t0, 1)
        avg = done / elapsed
        eta = f" — reste ≈ {fmt_duration((total - done) / avg)}" if avg > 0 and total > done else ""
        self.status.configure(
            text=f"{nf}/{tf} fichiers — {fmt_size(done)} / {fmt_size(total)} — écoulé {fmt_duration(elapsed)}{eta}")
        if cur:
            self.current.configure(
                text=f"{phase} : {cur}\n{fmt_size(fdone)} / {fmt_size(fsize)} — {fmt_size(speed)}/s")

    def _heartbeat(self) -> None:
        """Témoin d'activité : alerte si plus aucune donnée ne passe."""
        if self.running:
            age = time.time() - self.last_progress
            if self.paused:
                self.activity.configure(text="⏸ En pause", foreground="gray")
            elif age < 15:
                self.activity.configure(text="● Actif", foreground="#2e7d32")
            else:
                self.activity.configure(
                    text=f"⚠ Aucune activité depuis {int(age)} s — NAS lent, en attente ou bloqué ?",
                    foreground="#e65100")
        else:
            self.activity.configure(text="")
        self.after(1000, self._heartbeat)

    def _finish(self, s: core.Summary) -> None:
        self.running = False
        self.fprogress["value"] = 0
        self._set_running(False)
        self._keep_awake(False)
        self.current.configure(text="")
        dry = self.mover.opts.dry_run
        if s.cancelled:
            title, icon = "Arrêté", messagebox.showwarning
            self.status.configure(text="Arrêté — relancez pour reprendre")
        else:
            title, icon = ("Simulation terminée" if dry else "Terminé"), (messagebox.showinfo if s.ok else messagebox.showwarning)
            self.progress["value"] = 100 if s.ok and not dry else self.progress["value"]
            self.status.configure(text=title)
        lines = [f"Fichiers : {s.total_files} ({fmt_size(s.total_bytes)})",
                 f"{'À déplacer' if dry else 'Déplacés'} : {s.moved}",
                 f"Ignorés : {s.skipped}", f"Erreurs : {len(s.errors)}", f"Durée : {s.seconds:.0f} s"]
        for rel, err in s.errors[:10]:
            lines.append(f"  • {rel or '-'} : {err}")
        for line in lines:
            self._log(line)
        icon(title, "\n".join(lines))

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("Quitter", "Un déplacement est en cours.\nQuitter ? Il pourra être repris au prochain lancement."):
                return
            self.mover.cancel()
        self._keep_awake(False)
        self.destroy()


def main() -> None:
    App().mainloop()
