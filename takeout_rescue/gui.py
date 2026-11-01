"""A very simple window for Takeout Rescue (tkinter only).

Slow work runs in a background thread.  The thread puts messages in a queue
and the window checks the queue every 100 ms with after(), so it never freezes.
"""

import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__, core

BIG = ("Segoe UI", 14)
HEADING = ("Segoe UI", 18, "bold")


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Takeout Rescue %s" % __version__)
        root.geometry("760x640")

        self.export_dir = ""
        self.out_dir = ""
        self.plan = None
        self.messages = queue.Queue()
        self.busy = False
        self.applied = False        # True after a successful run, until the next scan

        frame = tk.Frame(root, padx=24, pady=16)
        frame.pack(fill="both", expand=True)

        tk.Label(frame, text="Takeout Rescue", font=HEADING).pack(anchor="w")
        tk.Label(frame, font=BIG, justify="left", wraplength=700,
                 text="Fix the dates on your Google Photos download. "
                      "Your original folder is never changed.").pack(anchor="w", pady=(0, 12))

        # Step 1
        self.btn_choose = tk.Button(frame, text="1. Choose your Google Photos download folder",
                                    font=BIG, command=self.choose_export)
        self.btn_choose.pack(fill="x", pady=4)
        self.lbl_export = tk.Label(frame, text="(nothing chosen yet)", font=BIG,
                                   fg="#555555", wraplength=700, justify="left")
        self.lbl_export.pack(anchor="w")

        # Step 2
        self.btn_scan = tk.Button(frame, text="2. Scan", font=BIG, command=self.start_scan,
                                  state="disabled")
        self.btn_scan.pack(fill="x", pady=(12, 4))
        self.lbl_summary = tk.Label(frame, text="", font=BIG, wraplength=700, justify="left")
        self.lbl_summary.pack(anchor="w")

        # Step 3
        self.btn_out = tk.Button(frame, text="3. Choose where to save the clean photos",
                                 font=BIG, command=self.choose_out, state="disabled")
        self.btn_out.pack(fill="x", pady=(12, 4))
        self.lbl_out = tk.Label(frame, text="(nothing chosen yet)", font=BIG,
                                fg="#555555", wraplength=700, justify="left")
        self.lbl_out.pack(anchor="w")

        # Step 4
        self.btn_fix = tk.Button(frame, text="4. Fix my photos", font=BIG,
                                 command=self.start_apply, state="disabled")
        self.btn_fix.pack(fill="x", pady=(12, 4))
        self.progress = ttk.Progressbar(frame, mode="determinate", length=700)
        self.progress.pack(fill="x", pady=4)
        self.lbl_status = tk.Label(frame, text="", font=BIG, wraplength=700, justify="left")
        self.lbl_status.pack(anchor="w")
        self.btn_open = tk.Button(frame, text="Open folder", font=BIG, command=self.open_folder)
        # The "Open folder" button only appears when everything is finished.

        # Undo works on the chosen save folder; enabled only if it holds a manifest.
        self.btn_undo = tk.Button(frame, text="Undo last fix", font=BIG,
                                  command=self.start_undo, state="disabled")
        self.btn_undo.pack(anchor="w", pady=(12, 0))

        root.after(100, self.poll)

    # ---- helpers ---------------------------------------------------------

    def update_buttons(self):
        idle = "disabled" if self.busy else "normal"
        self.btn_choose.config(state=idle)
        self.btn_scan.config(state="normal" if (self.export_dir and not self.busy) else "disabled")
        self.btn_out.config(state="normal" if (self.plan and not self.busy) else "disabled")
        ready = self.plan and self.out_dir and not self.busy and not self.applied
        self.btn_fix.config(state="normal" if ready else "disabled")
        can_undo = bool(self.out_dir) and not self.busy and self.manifest_count() is not None
        self.btn_undo.config(state="normal" if can_undo else "disabled")

    def manifest_count(self):
        """Number of files a previous fix recorded in the save folder, or None if no manifest."""
        if not self.out_dir:
            return None
        path = os.path.join(self.out_dir, core.MANIFEST_NAME)
        if not os.path.isfile(path):
            return None
        try:
            return len(core._load_manifest(path)["files"])
        except Exception:
            return 0

    def run_in_thread(self, work, done_kind):
        """Run work(progress) in a thread; results come back through the queue."""
        self.busy = True
        self.update_buttons()

        def progress(done, total, name):
            self.messages.put(("progress", done, total, name))

        def target():
            try:
                self.messages.put((done_kind, work(progress)))
            except Exception as e:      # shown to the user in a message box
                self.messages.put(("error", str(e)))

        threading.Thread(target=target, daemon=True).start()

    # ---- button actions --------------------------------------------------

    def choose_export(self):
        folder = filedialog.askdirectory(title="Choose your Google Photos download folder")
        if folder:
            self.export_dir = os.path.normpath(folder)
            self.lbl_export.config(text=self.export_dir)
            self.plan = None
            self.applied = False
            self.lbl_summary.config(text="")
            self.btn_open.pack_forget()
            self.lbl_status.config(text="")
            self.update_buttons()

    def start_scan(self):
        self.lbl_summary.config(text="Looking through your photos...")
        self.progress.config(value=0)
        export_dir = self.export_dir
        self.run_in_thread(lambda progress: core.scan(export_dir, progress), "scan_done")

    def choose_out(self):
        folder = filedialog.askdirectory(title="Choose where to save the clean photos")
        if folder:
            self.out_dir = os.path.normpath(folder)
            self.lbl_out.config(text=self.out_dir)
            self.update_buttons()

    def start_apply(self):
        self.btn_open.pack_forget()
        self.lbl_status.config(text="Copying your photos...")
        self.progress.config(value=0)
        plan, out_dir = self.plan, self.out_dir
        self.run_in_thread(lambda progress: core.apply(plan, out_dir, progress), "apply_done")

    def start_undo(self):
        count = self.manifest_count()
        if count is None or self.busy:
            return
        out_dir = self.out_dir
        if not messagebox.askyesno(
                "Takeout Rescue",
                "Remove the %d photos Takeout Rescue copied into %s?\n\n"
                "Your original download is not touched." % (count, out_dir)):
            return
        self.btn_open.pack_forget()
        self.lbl_status.config(text="Removing the copied photos...")

        def work(progress):
            problems = []
            return core.undo(out_dir, problems), problems

        self.run_in_thread(work, "undo_done")

    def open_folder(self):
        if self.out_dir and os.path.isdir(self.out_dir):
            os.startfile(self.out_dir)

    # ---- queue polling ---------------------------------------------------

    def poll(self):
        try:
            while True:
                try:
                    msg = self.messages.get_nowait()
                except queue.Empty:
                    break
                try:
                    self.handle(msg)
                except Exception as e:      # never let one bad message stop the window
                    self.busy = False
                    try:
                        self.update_buttons()
                    except Exception:
                        pass
                    messagebox.showerror("Takeout Rescue", "Something went wrong: %s" % e)
        finally:
            self.root.after(100, self.poll)

    def handle(self, msg):
        kind = msg[0]
        if kind == "progress":
            _, done, total, name = msg
            self.progress.config(maximum=max(total, 1), value=done)
            self.lbl_status.config(text="%d of %d: %s" % (done, total, name))
        elif kind == "scan_done":
            self.busy = False
            self.applied = False
            self.plan = msg[1]
            self.lbl_summary.config(text=self.plan.plain_summary())
            self.lbl_status.config(text="")
            self.progress.config(value=0)
            self.update_buttons()
        elif kind == "apply_done":
            self.busy = False
            self.applied = True
            counts = msg[1]
            text = "Done! Your clean photos are ready."
            if counts["already_done"]:
                text += " (%d were already there.)" % counts["already_done"]
            if counts["errors"]:
                text += " (%d files could not be copied. See the report file.)" % counts["errors"]
            self.lbl_status.config(text=text)
            self.btn_open.pack(anchor="w", pady=6)
            self.update_buttons()
            if counts["warnings"]:
                messagebox.showwarning("Takeout Rescue", "\n\n".join(counts["warnings"]))
        elif kind == "undo_done":
            self.busy = False
            self.applied = False
            removed, problems = msg[1]
            self.lbl_status.config(text="Removed %d files." % removed)
            self.update_buttons()
            if problems:
                messagebox.showwarning("Takeout Rescue", "Removed %d files, but:\n\n%s"
                                       % (removed, "\n".join(problems)))
        elif kind == "error":
            self.busy = False
            self.lbl_status.config(text="")
            self.lbl_summary.config(text=self.plan.plain_summary() if self.plan else "")
            self.update_buttons()
            messagebox.showerror("Takeout Rescue", msg[1])


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
