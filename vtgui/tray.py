"""Windows notification icon; native callbacks communicate through a queue only."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import queue
import threading


WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_TRAY = 0x8001
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B


class WindowsTray:
    def __init__(self, title="VTGUI"):
        self.title = title
        self.events = queue.Queue()
        self.error = ""
        self.hwnd = None
        self._thread = None
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self._registered = False

    def start(self):
        """Return true only after Windows has accepted the notification icon."""
        if os.name != "nt":
            return False
        if self._thread and self._thread.is_alive():
            return self._registered
        self._ready.clear()
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="VTGUI-tray", daemon=True)
        self._thread.start()
        if not self._ready.wait(3):
            self.error = "创建系统托盘图标超时"
            self.stop()
            return False
        return self._registered

    def stop(self):
        self._stopping.set()
        if self.hwnd:
            self._api.user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)

    def _run(self):
        api = None
        atom = None
        try:
            self._api = api = _WindowsAPI()
            instance = api.kernel32.GetModuleHandleW(None)
            class_name = f"VTGUI.Tray.{id(self)}"
            taskbar_created = api.user32.RegisterWindowMessageW("TaskbarCreated")

            @api.WNDPROC
            def window_proc(hwnd, message, wparam, lparam):
                if message == WM_TRAY:
                    if lparam == WM_LBUTTONDBLCLK:
                        self.events.put("restore")
                    elif lparam in (WM_RBUTTONUP, WM_CONTEXTMENU):
                        self._menu(hwnd)
                    return 0
                if taskbar_created and message == taskbar_created:
                    self._registered = bool(api.shell32.Shell_NotifyIconW(0, ctypes.byref(self._data)))
                    if not self._registered:
                        self.events.put("unavailable")
                    return 0
                if message == WM_CLOSE:
                    api.user32.DestroyWindow(hwnd)
                    return 0
                if message == WM_DESTROY:
                    api.user32.PostQuitMessage(0)
                    return 0
                return api.user32.DefWindowProcW(hwnd, message, wparam, lparam)

            window_class = api.WNDCLASS()
            window_class.lpfnWndProc = window_proc
            window_class.hInstance = instance
            window_class.lpszClassName = class_name
            atom = api.user32.RegisterClassW(ctypes.byref(window_class))
            if not atom:
                raise ctypes.WinError(ctypes.get_last_error())
            # A hidden top-level window receives Explorer's TaskbarCreated broadcast.
            self.hwnd = api.user32.CreateWindowExW(0, class_name, self.title, 0,
                                                  0, 0, 0, 0, None, None, instance, None)
            if not self.hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            self._data = api.NOTIFYICONDATA()
            self._data.cbSize = ctypes.sizeof(self._data)
            self._data.hWnd = self.hwnd
            self._data.uID = 1
            self._data.uFlags = 1 | 2 | 4  # NIF_MESSAGE | NIF_ICON | NIF_TIP
            self._data.uCallbackMessage = WM_TRAY
            self._data.hIcon = api.user32.LoadIconW(None, ctypes.c_void_p(32512))
            self._data.szTip = self.title[:127]
            if not api.shell32.Shell_NotifyIconW(0, ctypes.byref(self._data)):
                raise OSError("无法创建系统托盘图标，请检查 Windows 资源管理器是否正在运行")
            self._registered = True
            self._ready.set()
            message = wintypes.MSG()
            while not self._stopping.is_set():
                result = api.user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                if result == 0:
                    break
                api.user32.TranslateMessage(ctypes.byref(message))
                api.user32.DispatchMessageW(ctypes.byref(message))
        except Exception as exc:
            self.error = str(exc)
            self.events.put("unavailable")
        finally:
            self._registered = False
            if api and self.hwnd:
                if hasattr(self, "_data"):
                    api.shell32.Shell_NotifyIconW(2, ctypes.byref(self._data))  # NIM_DELETE
                if api.user32.IsWindow(self.hwnd):
                    api.user32.DestroyWindow(self.hwnd)
                self.hwnd = None
            if api and atom:
                api.user32.UnregisterClassW(class_name, instance)
            self._ready.set()

    def _menu(self, hwnd):
        api = self._api
        menu = api.user32.CreatePopupMenu()
        if not menu:
            self.events.put("restore")
            return
        try:
            api.user32.AppendMenuW(menu, 0, 1, "显示主窗口")
            api.user32.AppendMenuW(menu, 0x0800, 0, None)  # MF_SEPARATOR
            api.user32.AppendMenuW(menu, 0, 2, "退出程序")
            point = wintypes.POINT()
            api.user32.GetCursorPos(ctypes.byref(point))
            api.user32.SetForegroundWindow(hwnd)
            choice = api.user32.TrackPopupMenu(menu, 0x0100 | 0x0002, point.x, point.y,
                                              0, hwnd, None)  # TPM_RETURNCMD | TPM_RIGHTBUTTON
            api.user32.PostMessageW(hwnd, 0, 0, 0)  # Dismiss correctly on the next opening.
            if choice:
                self.events.put("restore" if choice == 1 else "exit")
        finally:
            api.user32.DestroyMenu(menu)


class _WindowsAPI:
    """Explicit pointer-sized signatures keep native handles safe on Windows x64."""
    def __init__(self):
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                         wintypes.WPARAM, wintypes.LPARAM)

        class WNDCLASS(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", self.WNDPROC),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

        class NOTIFYICONDATA(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                        ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                        ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                        ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
                        ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                        ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                        ("dwInfoFlags", wintypes.DWORD), ("guidItem", ctypes.c_byte * 16),
                        ("hBalloonIcon", wintypes.HICON)]

        self.WNDCLASS, self.NOTIFYICONDATA = WNDCLASS, NOTIFYICONDATA
        signatures = {
            "RegisterClassW": ([ctypes.POINTER(WNDCLASS)], wintypes.WORD),
            "UnregisterClassW": ([wintypes.LPCWSTR, wintypes.HINSTANCE], wintypes.BOOL),
            "CreateWindowExW": ([wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID], wintypes.HWND),
            "DefWindowProcW": ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], ctypes.c_ssize_t),
            "RegisterWindowMessageW": ([wintypes.LPCWSTR], wintypes.UINT),
            "LoadIconW": ([wintypes.HINSTANCE, ctypes.c_void_p], wintypes.HICON),
            "PostMessageW": ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
            "DestroyWindow": ([wintypes.HWND], wintypes.BOOL),
            "IsWindow": ([wintypes.HWND], wintypes.BOOL),
            "PostQuitMessage": ([ctypes.c_int], None),
            "GetMessageW": ([ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT], wintypes.BOOL),
            "TranslateMessage": ([ctypes.POINTER(wintypes.MSG)], wintypes.BOOL),
            "DispatchMessageW": ([ctypes.POINTER(wintypes.MSG)], ctypes.c_ssize_t),
            "CreatePopupMenu": ([], wintypes.HMENU),
            "AppendMenuW": ([wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR], wintypes.BOOL),
            "GetCursorPos": ([ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            "SetForegroundWindow": ([wintypes.HWND], wintypes.BOOL),
            "TrackPopupMenu": ([wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, wintypes.HWND, ctypes.c_void_p], wintypes.UINT),
            "DestroyMenu": ([wintypes.HMENU], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.user32, name)
            function.argtypes, function.restype = args, result
        self.kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self.kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        self.shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATA)]
        self.shell32.Shell_NotifyIconW.restype = wintypes.BOOL
