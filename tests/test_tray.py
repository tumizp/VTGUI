"""Exercise native icon registration, callbacks, shell recovery and cleanup."""
import ctypes
import os
import queue
import unittest
from unittest.mock import patch

from vtgui.tray import WindowsTray, WM_TRAY, WM_LBUTTONDBLCLK, WM_RBUTTONUP


@unittest.skipUnless(os.name == "nt", "Windows notification area only")
class NativeTrayTests(unittest.TestCase):
    def setUp(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        user32.FindWindowW.restype = ctypes.c_void_p
        if not user32.FindWindowW("Shell_TrayWnd", None):
            self.skipTest("Windows Explorer notification area is not running")
        self.tray = WindowsTray("VTGUI native tray test")
        self.addCleanup(self.tray.stop)
        self.assertTrue(self.tray.start(), self.tray.error)

    def test_double_click_and_menu_actions_reach_event_queue(self):
        api = self.tray._api
        api.user32.PostMessageW(self.tray.hwnd, WM_TRAY, 1, WM_LBUTTONDBLCLK)
        self.assertEqual(self.tray.events.get(timeout=3), "restore")
        for selection, action in ((1, "restore"), (2, "exit")):
            with patch.object(api.user32, "TrackPopupMenu", return_value=selection):
                api.user32.PostMessageW(self.tray.hwnd, WM_TRAY, 1, WM_RBUTTONUP)
                self.assertEqual(self.tray.events.get(timeout=3), action)

    def test_explorer_restart_reregisters_icon_and_stop_cleans_up(self):
        api = self.tray._api
        self.assertTrue(api.shell32.Shell_NotifyIconW(2, ctypes.byref(self.tray._data)))
        taskbar_created = api.user32.RegisterWindowMessageW("TaskbarCreated")
        # Ordered messages ensure the re-registration finishes before we inspect it.
        api.user32.PostMessageW(self.tray.hwnd, taskbar_created, 0, 0)
        api.user32.PostMessageW(self.tray.hwnd, WM_TRAY, 1, WM_LBUTTONDBLCLK)
        self.assertEqual(self.tray.events.get(timeout=3), "restore")
        self.assertTrue(api.shell32.Shell_NotifyIconW(1, ctypes.byref(self.tray._data)))
        hwnd = self.tray.hwnd
        self.tray.stop()
        self.assertFalse(api.user32.IsWindow(hwnd))
        self.assertFalse(self.tray._thread.is_alive())
        self.assertFalse(self.tray._registered)
        self.tray.stop()  # Idempotent shutdown, including main.py's cleanup path.

    def test_shell_failure_requests_window_restore(self):
        api = self.tray._api
        with patch.object(api.shell32, "Shell_NotifyIconW", return_value=0):
            message = api.user32.RegisterWindowMessageW("TaskbarCreated")
            api.user32.PostMessageW(self.tray.hwnd, message, 0, 0)
            self.assertEqual(self.tray.events.get(timeout=3), "unavailable")


if __name__ == "__main__":
    unittest.main()
