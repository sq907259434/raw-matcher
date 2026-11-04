# -*- coding: utf-8 -*-
"""单元测试：验证原生拖拽层能正确解析真实路径、并判断落在哪个框里。"""
import ctypes
import ctypes.wintypes as wt
import os
import struct
import sys
import time

# 拖拽层源码在 ../windows 下
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "windows"))

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wt.HGLOBAL
kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
kernel32.GetModuleHandleW.restype = wt.HMODULE

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON),
    ]


user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.CreateWindowExW.restype = wt.HWND
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DestroyWindow.argtypes = [wt.HWND]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]

TITLE = "RAW拖拽层单元测试窗口"
WM_DROPFILES = 0x0233
_keep = []


def pump(seconds):
    """抽消息：父窗口在本线程，必须自己泵，否则子窗口创建会卡住。"""
    msg = wt.MSG()
    end = time.time() + seconds
    while time.time() < end:
        if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        else:
            time.sleep(0.01)


def make_hdrop(paths, x, y):
    data = struct.pack("<IiiII", 20, x, y, 0, 1)
    for p in paths:
        data += p.encode("utf-16-le") + b"\x00\x00"
    data += b"\x00\x00"
    h = kernel32.GlobalAlloc(0x0040, len(data))
    ptr = kernel32.GlobalLock(h)
    ctypes.memmove(ptr, data, len(data))
    kernel32.GlobalUnlock(h)
    return h


def main():
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:  # noqa: BLE001
        pass

    hinst = kernel32.GetModuleHandleW(None)

    def wndproc(hwnd, msg, wp, lp):
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    proc = WNDPROC(wndproc)
    _keep.append(proc)
    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.lpfnWndProc = proc
    wc.hInstance = hinst
    wc.lpszClassName = "RawMatcherTestHost"
    user32.RegisterClassExW(ctypes.byref(wc))
    parent = user32.CreateWindowExW(0, "RawMatcherTestHost", TITLE, 0x00CF0000,
                                    120, 120, 900, 600, None, None, hinst, None)
    assert parent, "创建测试窗口失败：%d" % ctypes.get_last_error()
    user32.ShowWindow(parent, 5)

    import droptarget
    layer = droptarget.start(TITLE)
    t0 = time.time()
    while not layer.hwnd and time.time() - t0 < 6:
        pump(0.1)
    assert layer.hwnd, "拖拽层没挂上；error=%r" % (layer.error,)
    print("拖拽层已创建：hwnd=%s" % layer.hwnd)

    layer.set_zones({"raw": (10, 10, 300, 60), "jpg": (10, 90, 300, 140)})
    pump(0.8)
    print("层矩形 = %r" % (layer._rect,))
    ox, oy = (layer._rect[0], layer._rect[1]) if layer._rect else (0, 0)

    # 注意：DragQueryPoint 给的是屏幕坐标，所以这里必须传"屏坐标 = 原点 + 框内偏移"
    cases = [
        ([r"E:\raw"], ox + 150, oy + 30, "raw"),
        ([r"C:\Users\Public\Pictures\raw"], ox + 150, oy + 30, "raw"),
        ([r"D:\迅雷下载\jpg"], ox + 150, oy + 110, "jpg"),
        ([r"E:\新建 文件夹\子目录"], ox + 150, oy + 30, "raw"),
        ([r"E:\raw"], ox + 600, oy + 400, None),
        # 反过来：屏幕坐标没换算时，(150,30) 会被当成框内点；换算后它落在左上角，
        # 属于"框外但离 raw 很近"，按最近距离归到 raw —— 两种结果不同，正好盯住换算有没有做
        # 另外：拖偏到标签/边缘也要能落到最近的框（真实拖放实测就会这样落）
        ([r"F:\外面拖进来的"], 150, 30, "raw"),
        # 两个框之间（距 raw 10、距 jpg 20）：没有卡片区时按最近距离归 raw
        ([r"E:\间隙"], ox + 400, oy + 70, "raw"),
        # 一次拖两个：落在 raw 框上也必须两边都置空，交给页面按内容分
        ([r"E:\相册A", r"D:\相册B"], ox + 150, oy + 30, [None, None]),
    ]
    bad = 0

    def run(paths, x, y, want):
        nonlocal bad
        layer.take_drops()
        user32.SendMessageW(layer.hwnd, WM_DROPFILES, make_hdrop(paths, x, y), 0)
        got = layer.take_drops()
        want_list = want if isinstance(want, list) else [want] * len(paths)
        slots = [g["slot"] for g in got]
        paths_got = [g["path"] for g in got]
        good = (slots == want_list) and (paths_got == paths)
        bad += 0 if good else 1
        print("%s %-36s @(%4d,%4d) -> 框位=%-16s 路径=%s"
              % ("PASS" if good else "FAIL", " + ".join(paths)[:36], x, y,
                 slots if isinstance(want, list) else slots, paths_got))
        return got

    for paths, x, y, want in cases:
        run(paths, x, y, want)

    # 卡片接收区生效后：卡片内、两个小框之外 -> 交给页面按内容自动分（不再硬归最近的框）
    layer.set_card((0, 0, 700, 500), [(10, 10, 300, 60)])
    pump(0.5)
    run([r"E:\卡片空白处"], ox + 400, oy + 70, None)

    user32.DestroyWindow(parent)
    print("\n%s" % ("全部通过" if bad == 0 else "有 %d 条不符" % bad))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
