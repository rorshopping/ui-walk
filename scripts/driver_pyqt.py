"""driver_pyqt — in-process UI walk for Python/Qt applications.

Boots the target app's QMainWindow OFFSCREEN (nothing appears on the
user's desktop), patches native file/message dialogs to deterministic
answers so the walk never blocks, and then walks the app like a cautious
user:

  - menu walk: every menu-bar action is triggered; dialogs are photographed
    and dismissed (multi-stage "hammer" timers cover dialogs the regular
    closer misses)
  - tools walk (optional): inserts every tool from a catalog module and
    photographs each resulting properties/inspector panel
  - custom steps (optional): a JSON list of scripted user actions — call a
    method, load a file, wait, screenshot — for the app's end-to-end task

Requires a config JSON (--app-config). Example for a hypothetical app:

  {
    "module": "src.gui.main_window",
    "class": "MainWindow",
    "env": {"MYAPP_DATA_DIR": "C:/Temp/walk_appdata"},
    "window": {"width": 1680, "height": 1050},
    "steps": [
      {"call": "_load_from_file", "args": ["samples/demo.json"]},
      {"call": "fit_view"},
      {"shot": "demo_loaded"},
      {"call": "run_graph"},
      {"wait": 5},
      {"shot": "after_run"}
    ],
    "tools": {
      "catalog": "src.core.tool_catalog:CATEGORIES",
      "add": "add_node_auto_connect",
      "panel": "props_dock"
    }
  }

CLI:
  python driver_pyqt.py --app-config app.json --out C:/Temp/walk
  python driver_pyqt.py --app-config app.json --only menus,steps --seq 100

Phases: boot (default shots), steps (custom), menus, tools. Partitioned
runs (--only) write into the same gallery with --seq numbering offsets.
"""
import argparse
import faulthandler
import json
import os
import sys
import time
import traceback

faulthandler.enable()

SKIP_TEXT = ("exit", "quit", "schedule this", "run workflow", "stop",
             "delete", "format", "uninstall")


def patch_native_dialogs(open_paths, save_path):
    """Deterministic user answers for native dialogs (never blocks)."""
    from PyQt6.QtWidgets import QFileDialog, QInputDialog, QMessageBox
    seq = {"i": 0}

    def _next():
        p = open_paths[seq["i"] % len(open_paths)] if open_paths else ""
        seq["i"] += 1
        return p

    QFileDialog.getOpenFileName = staticmethod(
        lambda *a, **k: (_next(), "Files (*.*)"))
    QFileDialog.getOpenFileNames = staticmethod(
        lambda *a, **k: ([_next()], "Files (*.*)"))
    QFileDialog.getSaveFileName = staticmethod(
        lambda *a, **k: (save_path, ""))
    QFileDialog.getExistingDirectory = staticmethod(
        lambda *a, **k: (open_paths[0] if open_paths else os.getcwd()))
    QMessageBox.information = staticmethod(
        lambda *a, **k: QMessageBox.StandardButton.Ok)
    QMessageBox.warning = staticmethod(
        lambda *a, **k: QMessageBox.StandardButton.Ok)
    QMessageBox.critical = staticmethod(
        lambda *a, **k: QMessageBox.StandardButton.Ok)
    QMessageBox.question = staticmethod(
        lambda *a, **k: QMessageBox.StandardButton.Yes)
    QInputDialog.getText = staticmethod(lambda *a, **k: ("walked item", True))
    QInputDialog.getItem = staticmethod(lambda *a, **k: ("Option 1", True))
    QInputDialog.getInt = staticmethod(lambda *a, **k: (10, True))
    QInputDialog.getDouble = staticmethod(lambda *a, **k: (1.0, True))
    if hasattr(os, "startfile"):
        os.startfile = lambda *a, **k: None
    try:
        import webbrowser
        webbrowser.open = lambda *a, **k: True
    except Exception:
        pass


class Walker:
    def __init__(self, app, mw, cfg, out_dir, seq=0):
        self.app = app
        self.mw = mw
        self.cfg = cfg
        self.out_dir = out_dir
        self.report = {"steps": [], "exceptions": []}
        self.modals = []
        self.n = seq

    # -------------------------------------------------------------- shots
    def shot(self, widget, name):
        try:
            if widget is None:
                return ""
            self.app.processEvents()
            self.n += 1
            path = os.path.join(self.out_dir, f"{self.n:03d}_{name}.png")
            widget.grab().save(path)
            return path
        except Exception as e:
            self._log(f"shot:{name}", False, repr(e))
            return ""

    def _log(self, step, ok, detail="", png=""):
        self.report["steps"].append(
            {"step": step, "ok": bool(ok), "detail": str(detail)[:500],
             "png": png})
        print(("PASS " if ok else "FAIL ") + step,
              "" if ok else str(detail)[:160], flush=True)

    def pump(self, cond, timeout=30.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.app.processEvents()
            if cond():
                return True
            time.sleep(0.01)
        return False

    def close_all_dialogs(self):
        from PyQt6.QtWidgets import QDialog
        for w in self.app.topLevelWidgets():
            if isinstance(w, QDialog) and w.isVisible():
                try:
                    w.reject()
                except Exception:
                    try:
                        w.close()
                    except Exception:
                        pass

    def start_closer(self):
        from PyQt6.QtCore import QTimer
        seen = set()

        def tick():
            w = self.app.activeModalWidget()
            if w is not None and id(w) not in seen:
                seen.add(id(w))
                png = self.shot(w, f"modal_{type(w).__name__}")
                self.modals.append({"dialog": type(w).__name__, "png": png})
                try:
                    w.reject()
                except Exception:
                    try:
                        w.close()
                    except Exception:
                        pass
        timer = QTimer()
        timer.timeout.connect(tick)
        timer.start(80)
        return timer

    # ------------------------------------------------------------- phases
    def phase_boot(self):
        self.mw.resize(*(self.cfg.get("window") or {"width": 1680,
                                                    "height": 1050}).values())
        self.mw.show()
        self.app.processEvents()
        self._log("boot", True, "", self.shot(self.mw, "main_window"))

    def phase_steps(self):
        for i, step in enumerate(self.cfg.get("steps") or []):
            try:
                if "call" in step:
                    fn = getattr(self.mw, step["call"])
                    fn(*(step.get("args") or []))
                    self.pump(lambda: False, step.get("pump", 0.2))
                elif "wait" in step:
                    self.pump(lambda: False, float(step["wait"]))
                elif "shot" in step:
                    widget = self.mw
                    if step.get("widget") == "panel":
                        widget = getattr(self.mw, step.get("panel",
                                                           "props_dock")).widget()
                    self.shot(widget, step["shot"])
                self._log(f"step{i}:{step.get('call') or step.get('shot')
                                  or step.get('wait')}", True)
            except Exception as e:
                self.report["exceptions"].append(
                    {"step": i, "trace": traceback.format_exc()[-800:]})
                self._log(f"step{i}", False, repr(e))
                self.close_all_dialogs()

    def phase_menus(self):
        mb = self.mw.menuBar()
        actions = []
        for tm in mb.actions():
            menu = tm.menu()
            if menu is None:
                continue
            for act in menu.actions():
                if act.menu() is not None:
                    for sub in act.menu().actions():
                        actions.append((f"{tm.text()}>{act.text()}"
                                        f">{sub.text()}", sub))
                elif act.text():
                    actions.append((f"{tm.text()}>{act.text()}", act))
        self._log("menus:discovered", True, f"{len(actions)} actions")
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QDialog
        for label, act in actions:
            if any(s in (label or "").lower() for s in SKIP_TEXT):
                self._log(f"menu:skip {label}", True, "blocklisted")
                continue
            try:
                shot_done = set()

                def hammer():
                    w = self.app.activeModalWidget()
                    if w is None:
                        for top in self.app.topLevelWidgets():
                            if isinstance(top, QDialog) and top.isVisible() \
                                    and type(top).__name__ not in shot_done:
                                w = top
                                break
                    if w is None or type(w).__name__ in shot_done:
                        return
                    shot_done.add(type(w).__name__)
                    self.modals.append(
                        {"dialog": type(w).__name__,
                         "png": self.shot(w, f"hammer_{type(w).__name__}")})
                    try:
                        w.reject()
                    except Exception:
                        w.close()
                for delay in (1000, 1400, 1800, 2200):
                    QTimer.singleShot(delay, hammer)
                act.trigger()
                deadline = time.time() + 8
                while time.time() < deadline:
                    self.app.processEvents()
                    if self.app.activeModalWidget() is None:
                        break
                    time.sleep(0.05)
                time.sleep(0.1)
                self.app.processEvents()
                self._log(f"menu:{label}", True)
            except Exception as e:
                self.report["exceptions"].append(
                    {"step": label, "trace": traceback.format_exc()[-800:]})
                self._log(f"menu:{label}", False, repr(e))
                self.close_all_dialogs()

    def phase_tools(self):
        tcfg = self.cfg.get("tools") or {}
        mod_name, attr = tcfg["catalog"].split(":")
        catalog = getattr(__import__(mod_name, fromlist=[attr]), attr)
        add_fn = getattr(self.mw, tcfg.get("add", "add_node_auto_connect"))
        panel_holder = tcfg.get("panel", "props_dock")
        total = 0
        for _cat, tools in catalog.items():
            for tool in tools:
                total += 1
                try:
                    before = self._node_count()
                    add_fn(tool)
                    self.app.processEvents()
                    ok = self._node_count() > before
                    self.app.processEvents()
                    time.sleep(0.12)
                    self.app.processEvents()
                    holder = getattr(self.mw, panel_holder, None)
                    widget = holder.widget() if holder is not None else self.mw
                    png = self.shot(widget, f"tool_{str(tool).replace(' ', '_')}")
                    self._log(f"tool:{tool}", ok, "" if ok else "not added",
                              png)
                except Exception as e:
                    self.report["exceptions"].append(
                        {"step": f"tool:{tool}",
                         "trace": traceback.format_exc()[-800:]})
                    self._log(f"tool:{tool}", False, repr(e))

    def _node_count(self):
        from PyQt6.QtWidgets import QGraphicsItem
        return sum(1 for i in self.mw.scene().items()
                   if isinstance(i, QGraphicsItem)
                   and hasattr(i, "tool_type"))

    def finish(self, tag="all"):
        self.report["summary"] = {
            "steps": len(self.report["steps"]),
            "failed": sum(1 for s in self.report["steps"] if not s["ok"]),
            "exceptions": len(self.report["exceptions"]),
        }
        path = os.path.join(self.out_dir, f"ui_walk_report_{tag}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.report, f, indent=1, ensure_ascii=False)
        print(json.dumps(self.report["summary"]), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-config", required=True)
    ap.add_argument("--out", default="C:/Temp/uiwalk")
    ap.add_argument("--only", default="",
                    help="comma list: boot,steps,menus,tools")
    ap.add_argument("--seq", type=int, default=0,
                    help="screenshot numbering offset for partitioned runs")
    args = ap.parse_args()

    with open(args.app_config, encoding="utf-8") as f:
        cfg = json.load(f)
    for k, v in (cfg.get("env") or {}).items():
        os.environ.setdefault(k, v)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QT_QPA_FONTDIR", "C:\\Windows\\Fonts")
    os.makedirs(args.out, exist_ok=True)

    open_paths = [s.get("args", [""])[0] for s in (cfg.get("steps") or [])
                  if "call" in s and "load" in s["call"].lower()]
    patch_native_dialogs(
        open_paths or [cfg.get("sample_file", "")],
        os.path.join(args.out, "redirected_save.json"))

    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(["uiwalk"])

    # import the target app with its repo on sys.path (config keys are
    # repo-relative), BEFORE constructing anything from it
    repo = os.path.abspath(cfg.get("repo_root") or ".")
    if repo not in sys.path:
        sys.path.insert(0, repo)
    os.chdir(repo)

    mod_name, cls_name = cfg["module"], cfg["class"]
    mod = __import__(mod_name, fromlist=[cls_name])
    mw = getattr(mod, cls_name)()

    # never let the walk write inside the target repo
    def _guard(fn, repo=repo):
        def safe(file_path, *a, **k):
            try:
                if os.path.normcase(os.path.abspath(file_path or "")) \
                        .startswith(os.path.normcase(repo)):
                    file_path = os.path.join(args.out, "redirected_save.json")
                    mw.current_file = file_path
            except Exception:
                pass
            return fn(file_path, *a, **k)
        return safe
    if hasattr(mw, "_save_to_file"):
        mw._save_to_file = _guard(mw._save_to_file)

    only = set(s.strip() for s in args.only.split(",") if s.strip()) \
        or {"boot", "steps", "menus", "tools"}
    walker = Walker(app, mw, cfg, args.out, args.seq)
    closer = walker.start_closer()

    if "boot" in only:
        walker.phase_boot()
    if "steps" in only:
        walker.phase_steps()
    if "menus" in only:
        walker.phase_menus()
    if "tools" in only:
        walker.phase_tools()

    closer.stop()
    walker.finish("-".join(sorted(only)) if args.only else "all")


if __name__ == "__main__":
    main()
