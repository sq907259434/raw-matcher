# -*- coding: utf-8 -*-
"""
原生拖拽接收层。

背景：Chromium 内核（Edge / Chrome / WebView2）对所有网页都隐瞒拖进来文件夹的真实
路径，只给"名字 + 里面的文件"。同一批照片在本机有几分拷贝时，按名字 / 大小 / 时间
反查永远分不清，只能猜，猜就是错的。

办法：在网页窗口的客户区上盖一层"几乎透明"的顶层窗口（归属应用窗口，跟着移动、
最小化），只在那两个路径框的位置不透明。
拖拽落在那两块上时走的是 Windows 原生拖拽通道（DragAcceptFiles / WM_DROPFILES），
拿到的是操作系统给的绝对路径——从哪个盘拖的，就是哪个盘，不经过浏览器。
其余位置 alpha=0，鼠标点击直接穿透到下面的网页，不影响任何操作。

注意：必须是**顶层**窗口。Chromium 用 DirectComposition 合成画面，跨进程的
WS_CHILD 子窗口画不出来（实测 UpdateLayeredWindow 返回成功但屏幕上看不见）。
"""

import ctypes
import ctypes.wintypes as wt
import os
import struct
import threading
import time

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WM_DROPFILES = 0x0233
WM_TIMER = 0x0113
WM_MOUSEWHEEL = 0x020A
WM_APP_REPAINT = 0x8000 + 1
ULW_ALPHA = 2
AC_SRC_OVER = 0
AC_SRC_ALPHA = 1
DIB_RGB_COLORS = 0
SW_SHOWNOACTIVATE = 4
HWND_TOP = 0
GWLP_HWNDPARENT = -8

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
_CB = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

_DEBUG = bool(os.environ.get("RAWM_DROP_DEBUG"))


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.RegisterClassExW.restype = wt.ATOM
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.CreateWindowExW.restype = wt.HWND
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetClientRect.restype = wt.BOOL
user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
user32.ClientToScreen.restype = wt.BOOL
user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
user32.FindWindowW.restype = wt.HWND
user32.FindWindowExW.argtypes = [wt.HWND, wt.HWND, wt.LPCWSTR, wt.LPCWSTR]
user32.FindWindowExW.restype = wt.HWND
user32.IsWindow.argtypes = [wt.HWND]
user32.IsWindow.restype = wt.BOOL
user32.WindowFromPoint.argtypes = [wt.POINT]
user32.WindowFromPoint.restype = wt.HWND
user32.DestroyWindow.argtypes = [wt.HWND]
user32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT),
                                       ctypes.POINTER(wt.SIZE), wt.HDC, ctypes.POINTER(wt.POINT),
                                       wt.DWORD, ctypes.POINTER(BLENDFUNCTION), wt.DWORD]
user32.UpdateLayeredWindow.restype = wt.BOOL
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.restype = ctypes.c_ssize_t
user32.SetTimer.argtypes = [wt.HWND, wt.UINT, wt.UINT, wt.LPVOID]
user32.SetTimer.restype = wt.UINT
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.UINT]
user32.SetWindowPos.restype = wt.BOOL
user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.EnumWindows.argtypes = [ctypes.c_void_p, wt.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetWindowRect.restype = wt.BOOL
user32.GetClassNameW.restype = ctypes.c_int
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CloseHandle.restype = wt.BOOL
kernel32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
kernel32.GetExitCodeProcess.restype = wt.BOOL
kernel32.GetProcessTimes.argtypes = [wt.HANDLE, ctypes.POINTER(wt.FILETIME),
                                     ctypes.POINTER(wt.FILETIME),
                                     ctypes.POINTER(wt.FILETIME),
                                     ctypes.POINTER(wt.FILETIME)]
kernel32.GetProcessTimes.restype = wt.BOOL
kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wt.HGLOBAL
kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SendMessageW.restype = ctypes.c_ssize_t

gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.POINTER(BITMAPINFOHEADER), wt.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wt.HDC]

shell32.DragAcceptFiles.argtypes = [wt.HWND, wt.BOOL]
shell32.DragQueryFileW.argtypes = [wt.HANDLE, wt.UINT, wt.LPWSTR, wt.UINT]
shell32.DragQueryFileW.restype = wt.UINT
shell32.DragQueryPoint.argtypes = [wt.HANDLE, ctypes.POINTER(wt.POINT)]
shell32.DragFinish.argtypes = [wt.HANDLE]


class DropLayer(object):
    """盖在网页窗口客户区上的透明拖拽接收层。"""

    CLASS_NAME = "RawMatcherDropLayer"

    def __init__(self, title, pid=None):
        self.title = title
        self.pid = int(pid) if pid else 0
        self.hwnd = None
        self.parent = None
        self.error = None
        self._lock = threading.Lock()
        self._zones = {}
        self._drops = []
        self._wndproc = WNDPROC(self._on_message)
        self._sig = None
        self._rect = None
        self._vp = (0.0, 0.0)
        self._card = None
        self._holes = []
        self._log = []
        self._class_ok = False

    # ---------- 对外 ----------
    def start(self):
        threading.Thread(target=self._run, name="droplayer", daemon=True).start()

    def set_viewport(self, vh, dpr):
        """页面自报的视口信息：innerHeight(CSS) 与 devicePixelRatio。"""
        try:
            vh = float(vh or 0); dpr = float(dpr or 0)
        except (TypeError, ValueError):
            return
        if vh <= 0 or dpr <= 0:
            return
        with self._lock:
            if (round(vh, 2), round(dpr, 3)) == self._vp:
                return
            self._vp = (round(vh, 2), round(dpr, 3))
        self._post_repaint()

    def set_zones(self, zones):
        new = {}
        pad = 16          # 物理像素：比输入框外扩一点，手抖拖偏一点也能接住
        for k, r in (zones or {}).items():
            if r:
                try:
                    l, t, rr, b = (int(v) for v in r)
                except (TypeError, ValueError):
                    continue
                new[k] = (l - pad, t - pad, rr + pad, b + pad)
        with self._lock:
            if new == self._zones:
                return
            self._zones = new
        self._post_repaint()

    def set_card(self, card, holes):
        """整张卡片都当接收区：拖到卡片任何地方，都由系统直接给出真实路径，
        不再需要按名字去磁盘上找。再把「选择」按钮的位置挖空，让点击照常穿透。"""
        base = None
        if card:
            try:
                base = tuple(int(v) for v in card)
            except (TypeError, ValueError):
                base = None
        hs = []
        for h in (holes or []):
            try:
                hs.append(tuple(int(v) for v in h))
            except (TypeError, ValueError):
                continue
        with self._lock:
            if base == self._card and hs == self._holes:
                return
            self._card = base
            self._holes = hs
        self._note("card=%s holes=%s" % (base, hs))
        self._post_repaint()

    def take_drops(self):
        with self._lock:
            out = self._drops
            self._drops = []
        return out

    def probe(self):
        """诊断：强制重绘一次，再从本进程内部做命中测试（不受外部 DPI 影响）。"""
        out = {"dbg": _DEBUG}
        try:
            self._paint(force=True)
        except Exception as e:  # noqa: BLE001
            out["paint_error"] = repr(e)
        out["sig"] = list(self._sig) if self._sig else None
        if not (self.hwnd and user32.IsWindow(self.hwnd)):
            return out
        r = wt.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(r))
        out["win"] = [r.left, r.top, r.right, r.bottom]
        out["visible"] = bool(user32.IsWindowVisible(self.hwnd))

        def cls(h):
            b = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(h, b, 64)
            return b.value

        hits = {}
        for k, (l, t, rr, b) in self._zones.items():
            mx = r.left + (l + rr) // 2
            my = r.top + (t + b) // 2
            hits[k] = cls(user32.WindowFromPoint(wt.POINT(mx, my)))
        out["hits"] = hits
        out["hit_corner"] = cls(user32.WindowFromPoint(wt.POINT(r.left + 3, r.top + 3)))
        if self._card:
            l, t, rr, b = self._card
            # 卡片内、但不落在两个小框上的点（就是"随手一丢"的落点）
            out["hit_card_gap"] = cls(user32.WindowFromPoint(
                wt.POINT(r.left + (l + rr) // 2, r.top + t + 8)))
        out["hit_holes"] = [cls(user32.WindowFromPoint(
            wt.POINT(r.left + (h[0] + h[2]) // 2, r.top + (h[1] + h[3]) // 2))) for h in self._holes]
        return out

    # ---------- 存活维护 ----------
    def _alive(self):
        if not (self.hwnd and user32.IsWindow(self.hwnd)
                and self.parent and user32.IsWindow(self.parent)):
            return False
        # 挂错窗口（比如认到了同机开着的旧实例）：本进程还在，就重新认
        if self.pid and self._pid_alive(self.pid):
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(self.parent, ctypes.byref(pid))
            if pid.value != self.pid:
                return False
        return True

    def _run(self):
        while True:
            try:
                if not self._alive():
                    # 应用窗口没了（或挂错了）就把这层也收掉，别让它浮在桌面上
                    if self.hwnd and user32.IsWindow(self.hwnd):
                        try:
                            user32.DestroyWindow(self.hwnd)
                        except Exception:  # noqa: BLE001
                            pass
                    self.hwnd = None
                    self.parent = None
                    self._sig = None
                    if not self._attach(timeout=10.0):
                        time.sleep(1.2)
                        continue
                self._pump(0.5)
            except Exception as exc:  # noqa: BLE001
                self.error = exc
                self.hwnd = None
                self.parent = None
                time.sleep(0.6)

    def _pump(self, seconds):
        msg = wt.MSG()
        end = time.time() + seconds
        while time.time() < end:
            if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(0.01)

    # ---------- 挂载 ----------
    def _ensure_class(self, hinst):
        if self._class_ok:
            return
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = self.CLASS_NAME
        user32.RegisterClassExW(ctypes.byref(wc))
        self._class_ok = True

    def _attach(self, timeout=10.0):
        hinst = kernel32.GetModuleHandleW(None)
        self._ensure_class(hinst)

        deadline = time.time() + timeout
        parent = None
        while time.time() < deadline:
            parent = self._find_app_window()
            if parent:
                break
            time.sleep(0.2)
        if not parent:
            return False
        self.parent = parent
        rect = self._render_rect()
        if not rect:
            return False
        x, y, cx, cy = rect

        hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            self.CLASS_NAME, "drop-layer", WS_POPUP,
            x, y, cx, cy, None, None, hinst, None)
        if not hwnd:
            self.error = "CreateWindowExW 失败 %d" % ctypes.get_last_error()
            return False
        # 归属应用窗口：跟着它移动/最小化，并且始终在它上面
        user32.SetWindowLongPtrW(hwnd, GWLP_HWNDPARENT, parent)
        self.hwnd = hwnd
        self._sig = None
        shell32.DragAcceptFiles(hwnd, True)
        user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        self._paint(force=True)
        user32.SetTimer(hwnd, 1, 300, None)
        return True

    def _find_app_window(self):
        """认窗口的顺序：

        1) 按进程 id —— 本实例自己拉起来的那个 Edge 进程。同机开着旧版窗口时，
           按标题找会认到旧窗口上去（旧窗口标题已经在了，新窗口标题还没加载出来），
           所以只要进程还活着，就只认它，认不到宁可继续等。
        2) 进程已经退出（有些启动器会把浏览器另开进程）才退回到按标题找，
           并且挑"创建时间最新"的那个，同样避开旧窗口。
        """
        if self.pid and self._pid_alive(self.pid):
            hit = []

            def cb(hwnd, _lp):
                try:
                    if not user32.IsWindowVisible(hwnd):
                        return True
                    if not self._is_chrome_window(hwnd):
                        return True
                    pid = wt.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    if pid.value == self.pid:
                        r = wt.RECT()
                        user32.GetWindowRect(hwnd, ctypes.byref(r))
                        if r.right - r.left > 200 and r.bottom - r.top > 200:
                            hit.append(hwnd)
                            return False
                except Exception:  # noqa: BLE001
                    pass
                return True

            cb_ref = _CB(cb)
            user32.EnumWindows(ctypes.cast(cb_ref, ctypes.c_void_p), 0)
            return hit[0] if hit else None

        # 退路：标题匹配，多个候选时取进程创建时间最新的
        cands = []
        h = None
        while True:
            h = user32.FindWindowExW(None, h, "Chrome_WidgetWin_1", self.title)
            if not h:
                break
            if user32.IsWindowVisible(h):
                cands.append(h)
        if not cands:
            h = user32.FindWindowW(None, self.title)
            return h or None
        best = None
        for h in cands:
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
            t = self._proc_start(pid.value)
            if best is None or t > best[0]:
                best = (t, h)
        return best[1]

    @staticmethod
    def _pid_alive(pid):
        try:
            h = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
            if not h:
                return False
            try:
                code = wt.DWORD()
                if not kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return True
                return code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(h)
        except Exception:  # noqa: BLE001
            return True

    @staticmethod
    def _proc_start(pid):
        """进程创建时间（FILETIME 合成整数）；拿不到就给 0。"""
        try:
            h = kernel32.OpenProcess(0x1000, False, int(pid))
            if not h:
                return 0
            try:
                c = FILETIME(); e = FILETIME(); k = FILETIME(); u = FILETIME()
                if not kernel32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                                                ctypes.byref(k), ctypes.byref(u)):
                    return 0
                return (c.dwHighDateTime << 32) | c.dwLowDateTime
            finally:
                kernel32.CloseHandle(h)
        except Exception:  # noqa: BLE001
            return 0

    @staticmethod
    def _is_chrome_window(hwnd):
        buf = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hwnd, buf, 128)
        return buf.value.startswith("Chrome_WidgetWin")

    # ---------- 绘制 ----------
    def _render_rect(self):
        """网页视口在屏幕上的位置和尺寸（物理像素）。

        注意不能用客户区：Edge 的 --app 窗口顶部还有一条它自己画的标题栏，
        网页视口是从标题栏下面开始的。页面报上来的坐标是相对视口的，
        所以必须对准"渲染窗口"这一块，否则会整体上移一条标题栏的高度。"""
        if not self.parent or not user32.IsWindow(self.parent):
            return None
        best = None
        h = user32.FindWindowExW(self.parent, None, "Chrome_RenderWidgetHostHWND", None)
        while h:
            r = wt.RECT()
            if user32.GetWindowRect(h, ctypes.byref(r)):
                area = (r.right - r.left) * (r.bottom - r.top)
                if area > 0 and (best is None or area > best[0]):
                    best = (area, r.left, r.top, r.right, r.bottom)
            h = user32.FindWindowExW(self.parent, h, "Chrome_RenderWidgetHostHWND", None)
        if best:
            _a, l, t, rr, b = best
            if rr - l > 0 and b - t > 0:
                return l, t, rr - l, b - t

        # 退路：整个客户区，再按页面自报的视口高度扣掉顶部那条标题栏
        r = wt.RECT()
        if not user32.GetClientRect(self.parent, ctypes.byref(r)):
            return None
        cx, cy = r.right - r.left, r.bottom - r.top
        if cx <= 0 or cy <= 0:
            return None
        pt = wt.POINT(0, 0)
        if not user32.ClientToScreen(self.parent, ctypes.byref(pt)):
            return None
        with self._lock:
            vh, dpr = self._vp
        if vh > 0 and dpr > 0:
            view_cy = int(round(vh * dpr))
            off = cy - view_cy
            if 0 <= off < 300 and view_cy > 100:
                return pt.x, pt.y + off, cx, view_cy
        return pt.x, pt.y, cx, cy

    def _post_repaint(self):
        hwnd = self.hwnd
        if hwnd:
            user32.PostMessageW(hwnd, WM_APP_REPAINT, 0, 0)

    def _paint(self, force=False):
        if not self.hwnd:
            return
        rect = self._render_rect()
        if not rect:
            return
        x, y, cx, cy = rect
        self._rect = rect
        with self._lock:
            zones = tuple(sorted(self._zones.items()))
            card = self._card
            holes = tuple(self._holes)

        sig = (x, y, cx, cy, zones, card, holes)
        if not force and sig == self._sig:
            return
        self._sig = sig

        buf = bytearray(cx * cy * 4)
        # alpha=1：肉眼看不见，但系统认为这块不透明，于是参与命中测试、能接住拖放。
        # alpha=0：彻底透明，鼠标点击和拖放都穿透到下面的网页。
        HIT = bytes([0x40, 0x20, 0x20, 0xC8]) if _DEBUG else bytes([0, 0, 0, 1])
        CLEAR = bytes([0, 0, 0, 0])

        def fill_rect(rc, color):
            l, t, r, b = rc
            l = max(0, min(cx, l)); r = max(0, min(cx, r))
            t = max(0, min(cy, t)); b = max(0, min(cy, b))
            if r <= l or b <= t:
                return
            step = color * (r - l)
            for yy in range(t, b):
                row0 = (yy * cx + l) * 4
                buf[row0:row0 + (r - l) * 4] = step

        if card:
            fill_rect(card, HIT)
        for rc in holes:
            fill_rect(rc, CLEAR)
        for (_k, rc) in zones:
            fill_rect(rc, HIT)

        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = cx
        bmi.biHeight = -cy          # 负数 = 自上而下，与页面坐标同向
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0

        screen_dc = user32.GetDC(None)
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        bits = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(screen_dc, ctypes.byref(bmi), DIB_RGB_COLORS,
                                      ctypes.byref(bits), None, 0)
        try:
            if not hbmp or not bits:
                return
            ctypes.memmove(bits, bytes(buf), len(buf))
            old = gdi32.SelectObject(mem_dc, hbmp)
            dst = wt.POINT(x, y)
            size = wt.SIZE(cx, cy)
            src = wt.POINT(0, 0)
            blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
            ok = user32.UpdateLayeredWindow(self.hwnd, None, ctypes.byref(dst),
                                            ctypes.byref(size), mem_dc, ctypes.byref(src),
                                            0, ctypes.byref(blend), ULW_ALPHA)
            if not ok:
                self.error = "UpdateLayeredWindow 失败 %d" % ctypes.get_last_error()
            gdi32.SelectObject(mem_dc, old)
        finally:
            if hbmp:
                gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(None, screen_dc)

    # ---------- 消息 ----------
    def _note(self, text):
        """记一行诊断信息（最近 20 条），出问题时能看清拖拽到底走到哪一步。"""
        try:
            stamp = time.strftime("%H:%M:%S")
            with self._lock:
                self._log.append(stamp + " " + text)
                if len(self._log) > 20:
                    del self._log[:-20]
        except Exception:  # noqa: BLE001
            pass

    def inject_drop(self, path, slot=None):
        """自测用：在本进程里造一个真 HDROP 投给拖拽层，走完整消息路径。"""
        if not self.hwnd:
            return False
        with self._lock:
            rect = self._rect
            zones = dict(self._zones)
        if not rect:
            return False
        key = slot if slot in zones else ("raw" if "raw" in zones else next(iter(zones), None))
        if not key:
            return False
        l, t, r, b = zones[key]
        sx = rect[0] + (l + r) // 2
        sy = rect[1] + (t + b) // 2
        data = struct.pack("<IiiII", 20, sx, sy, 0, 1)
        data += str(path).encode("utf-16-le") + b"\x00\x00" + b"\x00\x00"
        h = kernel32.GlobalAlloc(0x2040, len(data))
        if not h:
            return False
        ptr = kernel32.GlobalLock(h)
        try:
            ctypes.memmove(ptr, data, len(data))
        finally:
            kernel32.GlobalUnlock(h)
        user32.SendMessageW(self.hwnd, WM_DROPFILES, h, 0)
        return True

    def _slot_at(self, x, y):
        with self._lock:
            zones = dict(self._zones)
        for k, (l, t, r, b) in zones.items():
            if l <= x <= r and t <= y <= b:
                return k
        return None

    def _nearest_slot(self, x, y):
        """落点不在任何一个框的正中间时（拖到框上边缘、标签上、两框之间），
        按竖直距离归到最近的那个框——比回去按"数到几个 RAW/JPG"猜可靠得多。
        离得太远（超过 160 物理像素）就不硬认，交回给页面自己判断。"""
        with self._lock:
            zones = dict(self._zones)
        best = None
        for k, (l, t, r, b) in zones.items():
            dx = 0 if l <= x <= r else min(abs(x - l), abs(x - r))
            dy = 0 if t <= y <= b else min(abs(y - t), abs(y - b))
            if max(dx, dy) > 160:
                continue
            d = dy * 10 + dx          # 竖直方向优先
            if best is None or d < best[0]:
                best = (d, k)
        return best[1] if best else None

    def _on_message(self, hwnd, msg, wparam, lparam):
        if msg == WM_DROPFILES:
            self._handle_drop(wparam)
            return 0
        if msg == WM_MOUSEWHEEL:
            # 接收区把滚轮消息吃掉了，转发给网页，页面才能照常上下滚
            try:
                if self.parent and user32.IsWindow(self.parent):
                    user32.SendMessageW(self.parent, msg, wparam, lparam)
            except Exception:  # noqa: BLE001
                pass
            return 0
        if msg == WM_APP_REPAINT:
            self._paint()
            return 0
        if msg == WM_TIMER:
            self._paint()     # 位置/尺寸/形状都没变就什么都不做
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _handle_drop(self, hdrop):
        try:
            pt = wt.POINT()
            shell32.DragQueryPoint(hdrop, ctypes.byref(pt))
            # DragQueryPoint 给的是**屏幕**坐标，而 zone 是相对视口的坐标，
            # 不换算的话永远判不出落在哪个框，只能退回去猜。
            with self._lock:
                rect = self._rect
            if rect:
                lx, ly = pt.x - rect[0], pt.y - rect[1]
            else:
                lx, ly = pt.x, pt.y
            slot = self._slot_at(lx, ly)
            if slot is None:
                with self._lock:
                    has_card = bool(self._card)
                if has_card:
                    self._note("落在卡片上但不在小框内 -> 交给页面按内容自动分")
                else:
                    slot = self._nearest_slot(lx, ly)
                    self._note("落点不在框内，按最近距离归到 %s" % slot)
            self._note("drop_recv pt=%s local=%s slot=%s rect=%s"
                       % ((pt.x, pt.y), (lx, ly), slot, rect))

            n = int(shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0) or 0)
            paths = []
            for i in range(min(n, 8)):
                sz = int(shell32.DragQueryFileW(hdrop, i, None, 0) or 0)
                if not sz:
                    continue
                b = ctypes.create_unicode_buffer(sz + 2)
                shell32.DragQueryFileW(hdrop, i, b, sz + 1)
                if b.value:
                    paths.append(b.value)
            if len(paths) > 1 and slot is not None:
                # 一次拖进来好几个：别都塞进同一个框，交给页面按内容分
                self._note("一次拖 %d 个，slot 置空由页面自动分类" % len(paths))
                slot = None
            with self._lock:
                for p in paths:
                    self._drops.append({"path": p, "slot": slot})
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                shell32.DragFinish(hdrop)
            except Exception:  # noqa: BLE001
                pass


_LAYER = None


def start(title, pid=None):
    global _LAYER
    if _LAYER is None:
        _LAYER = DropLayer(title, pid)
        _LAYER.start()
    return _LAYER


def set_zones(zones):
    if _LAYER:
        _LAYER.set_zones(zones)


def set_card(card, holes):
    if _LAYER:
        _LAYER.set_card(card, holes)


def set_viewport(vh, dpr):
    if _LAYER:
        _LAYER.set_viewport(vh, dpr)


def take_drops():
    return _LAYER.take_drops() if _LAYER else []


def inject_drop(path, slot=None):
    return _LAYER.inject_drop(path, slot) if _LAYER else False


def debug_info():
    if not _LAYER:
        return {"layer": False}
    info = {"layer": True, "hwnd": _LAYER.hwnd, "parent": _LAYER.parent,
            "zones": {k: list(v) for k, v in _LAYER._zones.items()},
            "card": list(_LAYER._card) if _LAYER._card else None,
            "holes": [list(h) for h in _LAYER._holes],
            "viewport": list(_LAYER._vp),
            "rect": list(_LAYER._rect) if _LAYER._rect else None,
            "error": repr(_LAYER.error) if _LAYER.error else ""}
    info.update(_LAYER.probe())
    info["log"] = list(_LAYER._log)
    return info
