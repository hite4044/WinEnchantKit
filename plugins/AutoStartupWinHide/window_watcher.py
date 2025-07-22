import ctypes
from ctypes.wintypes import *
from threading import Thread
from typing import Callable
import win32con as con
import win32gui
from ctypes import POINTER
import faulthandler


faulthandler.enable()

HANDLE = ctypes.c_void_p
LONG = ctypes.c_long
HWINEVENTHOOK = HANDLE
WINEVENTPROC = ctypes.WINFUNCTYPE(None, HWINEVENTHOOK, DWORD, HWND, LONG, LONG, DWORD, DWORD)

SetWinEventHook = ctypes.windll.user32.SetWinEventHook
SetWinEventHook.argtypes = [DWORD, DWORD, HMODULE, WINEVENTPROC, DWORD, DWORD, DWORD]
SetWinEventHook.restype = HWINEVENTHOOK
GetMessage = ctypes.windll.user32.GetMessageA
GetMessage.argtypes = [POINTER(MSG), HWND, UINT, UINT]
GetMessage.restype = BOOL
TranslateMessage = ctypes.windll.user32.TranslateMessage
TranslateMessage.argtypes = [POINTER(MSG)]
DispatchMessage = ctypes.windll.user32.DispatchMessageW
DispatchMessage.argtypes = [POINTER(MSG)]
CoInitialize = ctypes.windll.ole32.CoInitialize
CoInitialize.argtypes = [POINTER(ctypes.HRESULT)]
CoInitialize.restype = ctypes.HRESULT
CoUninitialize = ctypes.windll.ole32.CoUninitialize
CoUninitialize.argtypes = []



def register_hook(type_: int, proc: WINEVENTPROC):
    return SetWinEventHook(type_, type_,
                           None, proc,
                           0, 0,
                           con.WINEVENT_OUTOFCONTEXT | con.WINEVENT_SKIPOWNPROCESS)


def unregister_hook(hhook: int):
    ctypes.windll.user32.UnhookWinEvent(hhook)


class WindowWatcher:
    def __init__(self, event_type: int, proc: Callable[[int], None]):
        self.hhook = None
        self.event_type = event_type
        self.proc = proc
        self.thread:  Thread | None = None
        self.proc_type = WINEVENTPROC

    def start(self):
        self.thread = Thread(target=self.run, daemon=True)
        self.thread.start()

    def proc_warp(self,
                  h_win_event_hook: HWINEVENTHOOK,
                  event: DWORD, hwnd: int,
                  id_object: LONG, id_child: LONG,
                  dw_event_thread: DWORD, dw_ms_event_time: DWORD):
        self.proc(hwnd)

    def run(self):
        CoInitialize(None)
        t = WINEVENTPROC(self.proc_warp)
        self.hhook = register_hook(self.event_type, t)
        if self.hhook == 0:
            raise ctypes.WinError(0, "Register SerWinEventHook failed")
        msg = MSG()
        ret = ctypes.c_bool(True)
        while ret != 0:
            ret = GetMessage(ctypes.byref(msg), None, 0, 0)
            if ret == -1:
                raise ctypes.WinError()
            TranslateMessage(ctypes.byref(msg))
            DispatchMessage(ctypes.byref(msg))
        unregister_hook(self.hhook)
        CoUninitialize()

    def stop(self, timeout: float | None = None):
        if self.thread is None:
            raise RuntimeError("Thread has not started")
        win32gui.PostThreadMessage(self.thread.native_id, con.WM_QUIT, 0, 0)
        self.thread.join(timeout=timeout)
        self.thread = None
