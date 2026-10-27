"""Run with Python 3.11+ or build a standalone Windows executable."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import traceback


def main():
    parser = argparse.ArgumentParser(description="VTGUI · DRMLAB / unshackle 任务工作台")
    parser.add_argument("--data-dir", type=Path, help="独立配置与日志目录，默认 %%LOCALAPPDATA%%/VTGUI")
    parser.add_argument("--smoke-test", action="store_true", help="创建界面并自动退出，不执行任何命令")
    args = parser.parse_args()
    data = args.data_dir or Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share"))) / "VTGUI"
    lock = None
    engine = None
    try:
        if os.name == "nt":
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except (OSError, AttributeError):
                pass
        from vtgui.core import Engine, InstanceLock, Store
        from vtgui.ui import App
        lock = InstanceLock(data)
        store = Store(data)
        engine = Engine(store)
        app = App(store, engine)
        if args.smoke_test:
            app.after(500, app.destroy)
        else:
            engine.start()
        app.mainloop()
        engine.close()
    except Exception as exc:
        data.mkdir(parents=True, exist_ok=True)
        (data / "startup-error.log").write_text(traceback.format_exc(), encoding="utf-8")
        from tkinter import messagebox
        messagebox.showerror("VTGUI 启动失败", f"{exc}\n\n详情：{data / 'startup-error.log'}")
        return 1
    finally:
        if engine:
            engine.close()
        if lock:
            lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
