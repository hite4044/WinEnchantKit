from threading import Thread
from time import sleep

import win32con as con
import win32gui
import wx

cur_map: dict[int, wx.Cursor] = {}

def send_msg_gui(_):
    Thread(target=send_msg, daemon=True).start()


def to_pos(x: int, y: int):
    return x | (y << 16)
def send_msg():

    # SysListView32窗口消息监测法
    MESSAGES: list[tuple[int, int, int] | None] = [
        # (con.WM_MOUSEMOVE, 0x0, to_pos(969, 783)),
        (con.WM_LBUTTONDOWN, con.MK_LBUTTON, to_pos(969, 783)),
        # (con.WM_MOUSEMOVE, con.MK_LBUTTON, to_pos(969, 783)),
        None,
        # (con.WM_MOUSEMOVE, con.MK_LBUTTON, to_pos(969, 783)),
        (con.WM_LBUTTONUP, 0x0, to_pos(969, 783)),
    ]

    hwnd = list_view.GetHandle()
    Message = win32gui.PostMessage
    sleep(1)
    # win32gui.SetForegroundWindow(hwnd)
    for msg in MESSAGES:
        if msg is None:
            sleep(0.25)
        else:
            print(msg)
            Message(hwnd, msg[0], msg[1], to_pos(*win32gui.GetCursorPos()))
            print(hex(to_pos(*win32gui.GetCursorPos())))


def on_mouse_event(event: wx.MouseEvent):
    if event.LeftDown():
        msg = (con.WM_LBUTTONDOWN, con.MK_LBUTTON)
    elif event.RightDown():
        msg = (con.WM_RBUTTONDOWN, con.MK_RBUTTON)
    elif event.MiddleDown():
        msg = (con.WM_MBUTTONDOWN, con.MK_MBUTTON)
    elif event.LeftUp():
        msg = (con.WM_LBUTTONUP, 0x0)
    elif event.RightUp():
        msg = (con.WM_RBUTTONUP, 0x0)
    elif event.MiddleUp():
        msg = (con.WM_MBUTTONUP, 0x0)
    elif event.Moving():
        msg = (con.WM_MOUSEMOVE, 0x0)
        ret = win32gui.SendMessage(cls_desk, con.WM_SETCURSOR, 0x0, to_pos(*win32gui.GetCursorPos()))
        if ret in cur_map:
            list_view.SetCursor(cur_map[ret])
        else:
            cur = wx.Cursor()
            cur.SetHandle(ret)
            cur_map[ret] = cur
            list_view.SetCursor(cur)
    elif event.Dragging():
        msg = (con.WM_MOUSEMOVE, 0x0)
    else:
        return
    print(msg[0], msg[1], win32gui.GetCursorPos(), event.GetButton())
    win32gui.PostMessage(cls_desk, msg[0], msg[1], to_pos(*win32gui.GetCursorPos()))
    #win32gui.SetForegroundWindow(cls_desk)

def on_get_focus(event: wx.ActivateEvent):
    win32gui.SetForegroundWindow(cls_desk)

def find_cls_desk():
    def on_enum_window(hwnd: int, extra: list):
        if win32gui.GetClassName(hwnd) == "WorkerW":
            extra.append(hwnd)
    worker_W_windows = []
    win32gui.EnumWindows(on_enum_window, worker_W_windows)
    for window in worker_W_windows:
        cls_desk = win32gui.FindWindowEx(window, None, "_cls_desk_", None)
        if cls_desk:
            return cls_desk
    return 0

cls_desk = find_cls_desk()
print(cls_desk)

app = wx.App()
frame = wx.Frame(None, title="666", style=wx.DEFAULT_FRAME_STYLE)
list_view = wx.ListView(frame, style=wx.LC_LIST)
list_view.Bind(wx.EVT_MOUSE_EVENTS, on_mouse_event)
sizer = wx.BoxSizer(wx.VERTICAL)
sizer.Add(list_view, 1, wx.EXPAND)
frame.SetSizer(sizer)
#frame.Bind(wx.EVT_ACTIVATE, on_get_focus)
frame.SetTransparent(80)
frame.Show()
app.MainLoop()
