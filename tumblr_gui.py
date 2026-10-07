# -*- coding: utf-8 -*-
"""
Tumblr Archiver GUI. Needs tumblr_core.py in the same folder.

    python tumblr_gui.py
"""

import json
import os
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import tumblr_core as core


APP_DIR = os.path.dirname(os.path.realpath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "tumblr_gui_config.json")
POST_STATE_PATH = os.path.join(APP_DIR, "post_state.json")
MAX_LOG_LINES = 2000
STATE_KEYS = ("last_downloaded", "newest_post_ts")

API_FIELDS = [
    ("consumer_key", "Consumer key"),
    ("consumer_secret", "Secret key"),
    ("token", "Token"),
    ("token_secret", "Token secret"),
]


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


class BlogRow(object):
    """One blog: name box, 'skip reblogs' checkbox, status text, remove button."""

    def __init__(self, app, parent, name="", skip=True, only_new=True, state=None):
        self.key = None    # cleaned blog name while a crawl is running
        self.state = dict(state) if state else {}      # last_downloaded / newest_post_ts
        self.state_name = core.clean_blog_name(name)   # the blog those dates belong to
        self.name_var = tk.StringVar(value=name)
        self.skip_var = tk.BooleanVar(value=skip)
        self.only_new_var = tk.BooleanVar(value=only_new)
        self.status_var = tk.StringVar(value="")
        self.info_var = tk.StringVar(value="")

        self.frame = ttk.Frame(parent)
        self.frame.columnconfigure(4, weight=1)

        self.entry = ttk.Entry(self.frame, textvariable=self.name_var, width=26)
        self.entry.grid(row=0, column=0, padx=(0, 8), pady=(4, 0))
        self.entry.bind("<FocusOut>", lambda e: app.refresh_info())

        self.check = ttk.Checkbutton(self.frame, text="Skip reblogs", variable=self.skip_var)
        self.check.grid(row=0, column=1, padx=(0, 8), pady=(4, 0))

        self.only_new_check = ttk.Checkbutton(self.frame, text="Only new posts",
                                              variable=self.only_new_var)
        self.only_new_check.grid(row=0, column=2, padx=(0, 8), pady=(4, 0))

        self.start_btn = ttk.Button(self.frame, text="Start", width=7,
                                    command=lambda: app.start_row(self))
        self.start_btn.grid(row=0, column=3, padx=(0, 8), pady=(4, 0))

        self.status = ttk.Label(self.frame, textvariable=self.status_var, anchor="w")
        self.status.grid(row=0, column=4, sticky="ew", padx=(0, 8), pady=(4, 0))

        self.remove = ttk.Button(self.frame, text="\u00d7", width=3,
                                 command=lambda: app.remove_row(self))
        self.remove.grid(row=0, column=5, pady=(4, 0))

        self.info = ttk.Label(self.frame, textvariable=self.info_var,
                              foreground="gray", anchor="w")
        self.info.grid(row=1, column=0, columnspan=6, sticky="w", pady=(0, 4))

        self.frame.pack(fill="x", padx=4)

    def state_for_name(self):
        """The stored dates, but only while the name box still names the same blog."""
        if core.clean_blog_name(self.name_var.get()) == self.state_name:
            return self.state
        return {}

    def set_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        for w in (self.entry, self.check, self.only_new_check, self.start_btn, self.remove):
            w.configure(state=state)

    def destroy(self):
        self.frame.destroy()


class App(object):

    def __init__(self, root):
        self.root = root
        root.title("Tumblr Archiver")
        root.geometry("1180x780")
        root.minsize(980, 560)

        self.events = queue.Queue()     # worker threads -> GUI thread
        self.rows = []
        self.crawler = None
        self.running = False
        self.lockable = []              # widgets disabled while a crawl runs

        cfg = load_config()
        self.migrated = False
        self._build(cfg)
        self.post_state, self.post_state_writable = self._load_post_state()
        self.post_id_sets = dict(
            (name, set(record["downloaded_ids"]))
            for name, record in self.post_state.items())
        if not self.post_state_writable:
            self._append_log("Could not read post_state.json; preserving legacy dates and not "
                             "overwriting the post state.")

        out_dir = cfg.get("out_dir") or os.path.join(APP_DIR, "downloads")
        for blog in cfg.get("blogs") or []:
            if isinstance(blog, dict) and blog.get("name"):
                name = core.clean_blog_name(blog["name"])
                state = self.post_state.get(name, {})
                legacy_state = dict((k, blog[k]) for k in STATE_KEYS if k in blog)
                if (self.post_state_writable
                        and not any(key in state for key in STATE_KEYS)
                        and not legacy_state):
                    legacy_state.update(self._import_old_state(
                        cfg.get("dates"), out_dir, name))
                if legacy_state and self.post_state_writable:
                    self.migrated = True
                if self.post_state_writable:
                    state = self.post_state.setdefault(name, {"downloaded_ids": []})
                    self.post_id_sets.setdefault(name, set())
                    for key, value in legacy_state.items():
                        if key not in state:
                            state[key] = value
                else:
                    state = legacy_state
                row_state = dict((k, state[k]) for k in STATE_KEYS if k in state)
                self.add_row(blog["name"], bool(blog.get("skip_reblogs", True)),
                             bool(blog.get("only_new", True)), state=row_state)
        if not self.rows:
            self.add_row()
        if self.post_state_writable:
            self._scan_existing_post_ids(out_dir)
            self._save_post_state()
        self.refresh_info()
        if self.migrated and self.post_state_writable:
            self.save_config()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self._poll)

    # --- layout ---------------------------------------------------------------

    def _build(self, cfg):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=3)
        main.columnconfigure(1, weight=2)
        main.rowconfigure(2, weight=1)

        # Save folder
        top = ttk.Frame(main)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="Save to:").grid(row=0, column=0, padx=(0, 6))
        self.out_var = tk.StringVar(value=cfg.get("out_dir") or os.path.join(APP_DIR, "downloads"))
        out_entry = ttk.Entry(top, textvariable=self.out_var)
        out_entry.grid(row=0, column=1, sticky="ew")
        out_entry.bind("<FocusOut>", lambda e: self.refresh_info())
        browse = ttk.Button(top, text="Browse\u2026", command=self.browse)
        browse.grid(row=0, column=2, padx=(6, 0))
        self.lockable += [out_entry, browse]

        # API credentials
        api = ttk.LabelFrame(
            main, padding=8,
            text="Tumblr API credentials (optional \u2014 only for blogs the normal method can't reach)")
        api.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        saved_api = cfg.get("api") if isinstance(cfg.get("api"), dict) else {}
        self.cred_vars = {}
        for i, (key, label) in enumerate(API_FIELDS):
            r, c = divmod(i, 2)
            ttk.Label(api, text=label).grid(row=r, column=c * 2, sticky="e", padx=(0, 4), pady=2)
            var = tk.StringVar(value=saved_api.get(key, ""))
            entry = ttk.Entry(api, textvariable=var, show="*", width=30)
            entry.grid(row=r, column=c * 2 + 1, sticky="ew", padx=(0, 12))
            api.columnconfigure(c * 2 + 1, weight=1)
            self.cred_vars[key] = var
            self.lockable.append(entry)
        ttk.Label(api, text="Saved in plain text in tumblr_gui_config.json next to this program. "
                            "Keep that file private.",
                  foreground="gray").grid(row=2, column=0, columnspan=4, sticky="w", pady=(4, 0))

        # Blog list
        blogs_box = ttk.LabelFrame(main, text="Blogs", padding=6)
        blogs_box.grid(row=2, column=0, sticky="nsew", pady=(0, 8))
        blogs_box.rowconfigure(0, weight=1)
        blogs_box.columnconfigure(0, weight=1)

        bg = ttk.Style().lookup("TFrame", "background") or self.root.cget("bg")
        self.canvas = tk.Canvas(blogs_box, height=190, highlightthickness=0, bg=bg)
        scroll = ttk.Scrollbar(blogs_box, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")

        self.inner = ttk.Frame(self.canvas)
        window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>",
                        lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(window, width=e.width))
        self.canvas.bind("<Enter>", self._bind_wheel)
        self.canvas.bind("<Leave>", self._unbind_wheel)

        self.add_btn = ttk.Button(blogs_box, text="+ Add blog",
                                  command=lambda: self.add_row(focus=True))
        self.add_btn.grid(row=1, column=0, sticky="w", pady=(6, 0))

        # Start / Stop
        buttons = ttk.Frame(main)
        buttons.grid(row=3, column=0, sticky="w", pady=(0, 8))
        self.start_btn = ttk.Button(buttons, text="Start all", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(buttons, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0))
        self.single_btn = ttk.Button(buttons, text="Download singular posts",
                         command=self.open_single_post_dialog)
        self.single_btn.pack(side="left", padx=(8, 0))

        # Log
        log_box = ttk.LabelFrame(main, text="Log", padding=6)
        log_box.grid(row=0, column=1, rowspan=4, sticky="nsew", padx=(8, 0))
        log_box.rowconfigure(0, weight=1)
        log_box.columnconfigure(0, weight=1)
        self.log_text = tk.Text(log_box, height=10, wrap="word", state="disabled")
        log_scroll = ttk.Scrollbar(log_box, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll.grid(row=0, column=1, sticky="ns")

    # --- scrolling ------------------------------------------------------------

    def _bind_wheel(self, _event):
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)
        self.canvas.bind_all("<Button-4>", self._on_wheel)
        self.canvas.bind_all("<Button-5>", self._on_wheel)

    def _unbind_wheel(self, _event):
        self.canvas.unbind_all("<MouseWheel>")
        self.canvas.unbind_all("<Button-4>")
        self.canvas.unbind_all("<Button-5>")

    def _on_wheel(self, event):
        if event.num == 4:
            step = -1
        elif event.num == 5:
            step = 1
        else:
            step = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(step, "units")

    # --- blog rows ------------------------------------------------------------

    def add_row(self, name="", skip=True, only_new=True, focus=False, state=None):
        if self.running:
            return
        row = BlogRow(self, self.inner, name, skip, only_new, state)
        self.rows.append(row)
        if focus:
            row.entry.focus_set()
            self.root.update_idletasks()
            self.canvas.yview_moveto(1.0)

    def remove_row(self, row):
        if self.running:
            return
        row.destroy()
        self.rows.remove(row)

    def browse(self):
        folder = filedialog.askdirectory(initialdir=self.out_var.get() or APP_DIR)
        if folder:
            self.out_var.set(folder)
            self.refresh_info()

    # --- running --------------------------------------------------------------

    def start_row(self, row):
        """Start button on a single blog row: crawl only that blog."""
        self.start(only_row=row)

    def open_single_post_dialog(self):
        if self.running:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Download singular posts")
        dialog.transient(self.root)
        dialog.resizable(False, False)

        content = ttk.Frame(dialog, padding=12)
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=1)
        ttk.Label(content, text="Tumblr post URL:").grid(row=0, column=0, sticky="w")
        url_var = tk.StringVar()
        entry = ttk.Entry(content, textvariable=url_var, width=72)
        entry.grid(row=1, column=0, sticky="ew", pady=(4, 10))

        buttons = ttk.Frame(content)
        buttons.grid(row=2, column=0, sticky="e")
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right")

        def submit():
            post_url = url_var.get().strip()
            if not post_url:
                messagebox.showerror("Tumblr Archiver", "Enter a Tumblr post URL.", parent=dialog)
                entry.focus_set()
                return
            dialog.destroy()
            self.start_single_post(post_url)

        ttk.Button(buttons, text="Download", command=submit).pack(side="right", padx=(0, 8))
        dialog.bind("<Return>", lambda _event: submit())
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.grab_set()
        entry.focus_set()

    def start_single_post(self, post_url):
        if self.running:
            return
        out_dir = self.out_var.get().strip() or os.path.join(APP_DIR, "downloads")
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Tumblr Archiver", "Can't use that save folder:\n%s" % e)
            return
        self.save_config()
        creds = dict((key, value.get()) for key, value in self.cred_vars.items())
        self.crawler = core.Crawler(
            [], out_dir, creds=creds,
            log=lambda msg: self.events.put(("log", msg)),
            status=lambda site, text: self.events.put(("status", site, text)),
            record_download=lambda site, post_id: self.events.put(
                ("downloaded", site, post_id)))
        self._set_running(True)
        self._append_log("Downloading single post: %s" % post_url)
        threading.Thread(target=self._run_single_post,
                         args=(self.crawler, post_url), daemon=True).start()

    def _run_single_post(self, crawler, post_url):
        try:
            crawler.download_single_post(post_url)
        except Exception as e:
            self.events.put(("log", "Fatal error: %r" % (e,)))
        finally:
            self.events.put(("finished",))

    def start(self, only_row=None):
        if self.running:
            return

        blogs = []
        seen = set()
        for row in self.rows:
            row.key = None
            if only_row is not None and row is not only_row:
                continue
            name = core.clean_blog_name(row.name_var.get())
            if not name:
                continue
            if name in seen:
                self._append_log("Skipping duplicate entry: %s" % name)
                continue
            seen.add(name)
            row.name_var.set(name)
            row.key = name
            if row.state_name != name:      # the box now names a different blog
                row.state, row.state_name = {}, name
            row.status_var.set("waiting\u2026")
            blogs.append((name, bool(row.skip_var.get()), bool(row.only_new_var.get())))

        if not blogs:
            messagebox.showinfo(
                "Tumblr Archiver",
                "Type a blog name first." if only_row is not None
                else "Add at least one blog name first.")
            return

        out_dir = self.out_var.get().strip() or os.path.join(APP_DIR, "downloads")
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Tumblr Archiver", "Can't use that save folder:\n%s" % e)
            return

        self.save_config()
        states = dict((r.key, r.state) for r in self.rows if r.key)
        creds = dict((k, v.get()) for k, v in self.cred_vars.items())
        self.crawler = core.Crawler(
            blogs, out_dir, creds=creds,
            log=lambda msg: self.events.put(("log", msg)),
            status=lambda site, text: self.events.put(("status", site, text)),
            done=lambda site: self.events.put(("done", site)),
            get_state=lambda site: states.get(site, {}),
            save_state=lambda site, state: self.events.put(("state", site, state)),
            record_download=lambda site, post_id: self.events.put(
                ("downloaded", site, post_id)))

        self._set_running(True)
        self._append_log("Starting: %s" % ", ".join(
            "%s (%s reblogs, %s)" % (n, "skip" if s else "keep",
                                     "new posts only" if o else "all posts")
            for n, s, o in blogs))
        threading.Thread(target=self._run, args=(self.crawler,), daemon=True).start()

    def _run(self, crawler):
        try:
            crawler.run()
        except Exception as e:
            self.events.put(("log", "Fatal error: %r" % (e,)))
        finally:
            self.events.put(("finished",))

    def stop(self):
        if self.crawler:
            self.crawler.stop()
            self.stop_btn.configure(state="disabled")
            self._append_log("Stopping\u2026 finishing the files in progress.")

    def _set_running(self, running):
        self.running = running
        for w in self.lockable + [self.add_btn]:
            w.configure(state="disabled" if running else "normal")
        for row in self.rows:
            row.set_enabled(not running)
        self.start_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")
        self.single_btn.configure(state="disabled" if running else "normal")

    # --- events from worker threads ---------------------------------------------

    def _poll(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "log":
                    self._append_log(event[1])
                elif event[0] == "status":
                    for row in self.rows:
                        if row.key == event[1]:
                            row.status_var.set(event[2])
                elif event[0] == "state":
                    for row in self.rows:
                        if row.key == event[1]:
                            row.state, row.state_name = dict(event[2]), event[1]
                    self._update_post_state(event[1], event[2])
                    if not self.post_state_writable:
                        self.save_config()
                elif event[0] == "downloaded":
                    self._record_downloaded_post(event[1], event[2])
                elif event[0] == "done":
                    self.refresh_info()
                elif event[0] == "finished":
                    self._save_post_state()
                    self.crawler = None
                    self._set_running(False)
                    self.refresh_info()
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _append_log(self, text):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.log_text.delete("1.0", "%d.0" % (lines - MAX_LOG_LINES + 1))
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # --- config / closing ----------------------------------------------------------

    def refresh_info(self):
        """Show each blog's stored dates."""
        for row in self.rows:
            if core.clean_blog_name(row.name_var.get()):
                row.info_var.set(core.describe_state(row.state_for_name()))
            else:
                row.info_var.set("")

    def _load_post_state(self):
        try:
            with open(POST_STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}, True
        except (OSError, ValueError):
            return {}, False

        if not isinstance(data, dict) or not isinstance(data.get("blogs"), dict):
            return {}, False

        state = {}
        for name, record in data["blogs"].items():
            if not isinstance(name, str) or not isinstance(record, dict):
                return {}, False
            ids = record.get("downloaded_ids", [])
            if (not isinstance(ids, list)
                    or any(not isinstance(post_id, (str, int)) or isinstance(post_id, bool)
                           for post_id in ids)):
                return {}, False
            state[name] = {
                "downloaded_ids": sorted(set(str(post_id) for post_id in ids)),
            }
            for key in STATE_KEYS:
                if key in record:
                    state[name][key] = record[key]
        return state, True

    def _scan_existing_post_ids(self, out_dir):
        """Import completed archive IDs once into the root-level registry."""
        try:
            blog_dirs = os.scandir(out_dir)
        except FileNotFoundError:
            return
        except OSError as e:
            self._append_log("Could not scan downloaded posts in %s: %s" % (out_dir, e))
            return

        with blog_dirs:
            for blog_entry in blog_dirs:
                try:
                    if not blog_entry.is_dir(follow_symlinks=False):
                        continue
                    post_dirs = os.scandir(blog_entry.path)
                except OSError as e:
                    self._append_log("Could not scan %s: %s" % (blog_entry.path, e))
                    continue

                ids = set()
                with post_dirs:
                    for post_entry in post_dirs:
                        try:
                            if not post_entry.is_dir(follow_symlinks=False):
                                continue
                            post_id = post_entry.name.rsplit("_", 1)[-1]
                            if (post_id.isdigit()
                                    and os.path.isfile(os.path.join(
                                        post_entry.path, "post.json"))):
                                ids.add(post_id)
                        except OSError as e:
                            self._append_log("Could not inspect %s: %s" %
                                             (post_entry.path, e))
                if ids:
                    self.post_state.setdefault(
                        blog_entry.name, {"downloaded_ids": []})
                    self.post_id_sets.setdefault(blog_entry.name, set()).update(ids)

    def _save_post_state(self):
        if not self.post_state_writable:
            return False
        temp_path = POST_STATE_PATH + ".part"
        try:
            blogs = {}
            for name, record in self.post_state.items():
                stored = dict(record)
                stored["downloaded_ids"] = sorted(
                    self.post_id_sets.get(name, set()))
                blogs[name] = stored
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump({"blogs": blogs}, f, indent=4, ensure_ascii=False)
            os.replace(temp_path, POST_STATE_PATH)
            return True
        except OSError as e:
            self._append_log("Could not save post_state.json: %s" % e)
            self.post_state_writable = False
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass
            return False

    def _update_post_state(self, name, state):
        if not self.post_state_writable:
            return
        record = self.post_state.setdefault(name, {"downloaded_ids": []})
        for key in STATE_KEYS:
            if key in state:
                record[key] = state[key]
        self._save_post_state()

    def _record_downloaded_post(self, name, post_id):
        if not self.post_state_writable:
            self._append_log("Could not record downloaded post %s for %s: "
                             "post_state.json is unavailable." % (post_id, name))
            return
        self.post_state.setdefault(name, {"downloaded_ids": []})
        self.post_id_sets.setdefault(name, set()).add(str(post_id))

    def _import_old_state(self, old_dates, out_dir, name):
        """Dates from earlier versions: the old 'dates' section of this config, or a
        <save folder>/<blog>/_archive.json (deleted once imported)."""
        state = None
        if isinstance(old_dates, dict):
            by_blog = old_dates.get(os.path.abspath(out_dir))
            if isinstance(by_blog, dict):
                state = by_blog.get(name)
        if not isinstance(state, dict):
            legacy = os.path.join(out_dir, name, "_archive.json")
            try:
                with open(legacy, "r", encoding="utf-8") as f:
                    state = json.load(f)
            except (OSError, ValueError):
                state = None
            if isinstance(state, dict):
                try:
                    os.remove(legacy)
                except OSError:
                    pass
        if isinstance(state, dict):
            self.migrated = True
            return dict((k, state[k]) for k in STATE_KEYS if k in state)
        return {}

    def save_config(self):
        data = {
            "out_dir": self.out_var.get().strip(),
            "api": dict((k, v.get().strip()) for k, v in self.cred_vars.items()),
            "blogs": [],
        }
        for r in self.rows:
            name = core.clean_blog_name(r.name_var.get())
            if not name:
                continue
            entry = {"name": name,
                     "skip_reblogs": bool(r.skip_var.get()),
                     "only_new": bool(r.only_new_var.get())}
            state = r.state_for_name()
            if not self.post_state_writable:
                for k in STATE_KEYS:
                    if k in state:
                        entry[k] = state[k]
            data["blogs"].append(entry)
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
        except OSError as e:
            self._append_log("Could not save settings: %s" % e)

    def on_close(self):
        if self.crawler:
            self.crawler.stop()
        self.save_config()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
