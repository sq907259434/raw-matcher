# -*- coding: utf-8 -*-
"""
RAW 选片工具 · Windows 桌面版外壳
--------------------------------------------------
作用：把 raw-matcher.html 装进一个免安装的 exe。
页面仍然用 Edge/Chrome 的 --app 窗口渲染（界面与网页版完全一致），
但"选文件夹 / 读文件 / 复制文件"全部改走本机程序：

  选文件夹 → 系统原生文件夹对话框，返回真实绝对路径
  读文件   → 只按需取文件头几百 KB 给前端解析 EXIF
  复制     → kernel32!CopyFileW，与资源管理器同一条通道

浏览器关掉后心跳中断，本进程自动退出，不留后台。
"""

import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import droptarget

APP_TITLE = "RAW 选片工具"
DEFAULT_PORT = 47311
MAX_HEAD_BYTES = 16 * 1024 * 1024

# ---------------------------------------------------------------- win32 绑定

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_ole32 = ctypes.WinDLL("ole32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)

_CopyFileW = _k32.CopyFileW
_CopyFileW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.BOOL)
_CopyFileW.restype = wintypes.BOOL

# 判断临时浏览器配置目录是否还被另一个正在运行的实例占用
_OpenProcess = _k32.OpenProcess
_OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_OpenProcess.restype = wintypes.HANDLE
_CloseHandle = _k32.CloseHandle
_CloseHandle.argtypes = (wintypes.HANDLE,)
_CloseHandle.restype = wintypes.BOOL


class _BROWSEINFOW(ctypes.Structure):
    _fields_ = [
        ("hwndOwner", wintypes.HWND),
        ("pidlRoot", ctypes.c_void_p),
        ("pszDisplayName", wintypes.LPWSTR),
        ("lpszTitle", wintypes.LPCWSTR),
        ("ulFlags", wintypes.UINT),
        ("lpfn", ctypes.c_void_p),
        ("lParam", wintypes.LPARAM),
        ("iImage", ctypes.c_int),
    ]


class _OPENFILENAMEW(ctypes.Structure):
    _fields_ = [
        ("lStructSize", wintypes.DWORD),
        ("hwndOwner", wintypes.HWND),
        ("hInstance", wintypes.HINSTANCE),
        ("lpstrFilter", wintypes.LPCWSTR),
        ("lpstrCustomFilter", wintypes.LPWSTR),
        ("nMaxCustFilter", wintypes.DWORD),
        ("nFilterIndex", wintypes.DWORD),
        ("lpstrFile", wintypes.LPWSTR),
        ("nMaxFile", wintypes.DWORD),
        ("lpstrFileTitle", wintypes.LPWSTR),
        ("nMaxFileTitle", wintypes.DWORD),
        ("lpstrInitialDir", wintypes.LPCWSTR),
        ("lpstrTitle", wintypes.LPCWSTR),
        ("Flags", wintypes.DWORD),
        ("nFileOffset", wintypes.WORD),
        ("nFileExtension", wintypes.WORD),
        ("lpstrDefExt", wintypes.LPCWSTR),
        ("lCustData", wintypes.LPARAM),
        ("lpfnHook", ctypes.c_void_p),
        ("lpTemplateName", wintypes.LPCWSTR),
    ]


def long_path(p):
    """给系统调用用的 \\\\?\\ 长路径，避免超过 260 字符时失败。"""
    try:
        p = os.path.abspath(p)
    except Exception:
        return p
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


_pick_queue = []


def pick_folder(title):
    """系统文件夹选择框。返回绝对路径，取消返回 None。"""
    forced = os.environ.get("RAWM_TEST_PICK")
    if forced:
        global _pick_queue
        if not _pick_queue:
            _pick_queue = [x for x in forced.split("||") if x]
        return os.path.normpath(_pick_queue.pop(0)) if _pick_queue else None
    owner = _user32.GetForegroundWindow()
    _ole32.CoInitialize(None)
    try:
        name_buf = ctypes.create_unicode_buffer(1024)
        bi = _BROWSEINFOW()
        bi.hwndOwner = owner
        bi.pidlRoot = None
        bi.pszDisplayName = ctypes.cast(name_buf, wintypes.LPWSTR)
        bi.lpszTitle = title
        # RETURNONLYFSDIRS | EDITBOX | NEWDIALOGSTYLE（可调整大小、可新建文件夹）
        bi.ulFlags = 0x00000001 | 0x00000010 | 0x00000040
        bi.lpfn = None
        bi.lParam = 0
        bi.iImage = 0
        _shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(_BROWSEINFOW)]
        _shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
        pidl = _shell32.SHBrowseForFolderW(ctypes.byref(bi))
        if not pidl:
            return None
        out = ctypes.create_unicode_buffer(32768)
        _shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
        _shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
        ok = _shell32.SHGetPathFromIDListW(ctypes.c_void_p(pidl), out)
        _ole32.CoTaskMemFree(ctypes.c_void_p(pidl))
        return (out.value or None) if ok else None
    finally:
        try:
            _ole32.CoUninitialize()
        except Exception:
            pass


def save_as(title, filename):
    """系统另存为对话框。返回路径，取消返回 None。"""
    owner = _user32.GetForegroundWindow()
    _ole32.CoInitialize(None)
    try:
        buf = ctypes.create_unicode_buffer(32768)
        buf.value = filename
        ofn = _OPENFILENAMEW()
        ofn.lStructSize = ctypes.sizeof(_OPENFILENAMEW)
        ofn.hwndOwner = owner
        ofn.lpstrFilter = "文本文件 (*.txt;*.csv)\0*.txt;*.csv\0所有文件 (*.*)\0*.*\0\0"
        ofn.nFilterIndex = 1
        ofn.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
        ofn.nMaxFile = 32768
        ofn.lpstrTitle = title
        # OVERWRITEPROMPT | PATHMUSTEXIST | NOCHANGEDIR
        ofn.Flags = 0x00000002 | 0x00000800 | 0x00000008
        _comdlg32 = ctypes.WinDLL("comdlg32", use_last_error=True)
        _comdlg32.GetSaveFileNameW.argtypes = [ctypes.POINTER(_OPENFILENAMEW)]
        _comdlg32.GetSaveFileNameW.restype = wintypes.BOOL
        if not _comdlg32.GetSaveFileNameW(ctypes.byref(ofn)):
            return None
        return buf.value or None
    finally:
        try:
            _ole32.CoUninitialize()
        except Exception:
            pass


def add_suffix(name, n):
    base, ext = os.path.splitext(name)
    return "%s_%d%s" % (base, n, ext)


# ---------------------------------------------------------------- 拖拽文件夹定位

RAW_EXT = {
    "cr2", "cr3", "crw", "nef", "nrw", "arw", "srf", "sr2", "raf", "orf", "rw2",
    "rwl", "dng", "pef", "raw", "3fr", "erf", "kdc", "mrw", "x3f", "srw", "iiq",
    "dcr", "fff", "gpr", "mef", "mos", "cap",
}
JPG_EXT = {"jpg", "jpeg", "jpe", "jfif"}

# 找目录时直接跳过的系统目录，省时间也避免误判
SKIP_DIRS = {
    "windows", "program files", "program files (x86)", "programdata", "$recycle.bin",
    "system volume information", "appdata", "node_modules", ".git", ".svn",
    "recovery", "msocache", "perflogs", "intel", "amd", "nvidia", "drivers",
    "packages", "temp", "tmp", "cache", "system32", "syswow64", "winsxs",
}


def ext_of(name):
    i = name.rfind(".")
    return name[i + 1:].lower() if i > 0 else ""


def classify_folder(path, max_depth=2, cap=200000):
    """数一数这个文件夹里有多少 RAW / JPG，不返回文件清单。"""
    raw = jpg = other = 0
    for dirpath, dirnames, filenames in os.walk(path):
        rel = os.path.relpath(dirpath, path)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        if depth >= max_depth:
            dirnames[:] = []
        for fn in filenames:
            if fn.startswith("."):
                continue
            e = ext_of(fn)
            if e in RAW_EXT:
                raw += 1
            elif e in JPG_EXT:
                jpg += 1
            else:
                other += 1
        if raw + jpg > cap:
            break
    return raw, jpg, other


def fixed_drives():
    drives = []
    try:
        mask = _k32.GetLogicalDrives()
        for i in range(26):
            if mask & (1 << i):
                root = chr(65 + i) + ":\\"
                # 3=FIXED 本地磁盘，2=REMOVABLE 移动硬盘/U盘/读卡器（摄影师常用）
                # 光驱(5)、网络盘(4) 不搜：会卡住或极慢
                if _k32.GetDriveTypeW(ctypes.c_wchar_p(root)) in (2, 3):
                    drives.append(root)
    except Exception:  # noqa: BLE001
        pass
    return drives


def locate_roots():
    """按"最可能放照片"的顺序给出搜索起点，命中就停，避免白扫系统目录。"""
    home = os.path.expanduser("~")
    near = []
    for sub in ("Desktop", "Documents", "Downloads", "Pictures", "Videos", "OneDrive"):
        p = os.path.join(home, sub)
        if os.path.isdir(p):
            near.append(p)
    drives = fixed_drives()
    other = [d for d in drives if not d.upper().startswith("C:")]
    sysd = [d for d in drives if d.upper().startswith("C:")]
    return near, other + sysd


def unlong(p):
    r"""去掉 \\?\ 前缀，界面上要显示成正常人看得懂的路径。"""
    if p.startswith("\\\\?\\UNC\\"):
        return "\\\\" + p[8:]
    if p.startswith("\\\\?\\"):
        return p[4:]
    return p


def count_photos(path, depth=2, cap=30000):
    """数这个文件夹（含下两层子目录）里有多少 RAW / JPG，用来给候选打分。"""
    raw = jpg = 0
    base = path.rstrip("\\/")
    try:
        with os.scandir(base) as it:
            entries = list(it)
    except OSError:
        return 0, 0
    stack = [(base, entries, 0)]
    while stack:
        cur, items, lvl = stack.pop()
        for e in items:
            try:
                if e.is_dir(follow_symlinks=False):
                    if lvl < depth:
                        try:
                            with os.scandir(e.path) as it2:
                                stack.append((e.path, list(it2), lvl + 1))
                        except OSError:
                            pass
                    continue
            except OSError:
                continue
            ex = ext_of(e.name)
            if ex in RAW_EXT:
                raw += 1
            elif ex in JPG_EXT:
                jpg += 1
        if raw + jpg > cap:
            break
    return raw, jpg


def sample_names(path, subs, want, need, limit=200):
    """这一层的文件名不够判断时，再看一层子目录（RAW 目录常按日期分子文件夹）。"""
    got = set()
    for e in subs:
        if len(got) >= limit:
            break
        try:
            with os.scandir(e.path) as it:
                for f in it:
                    try:
                        if not f.is_dir(follow_symlinks=False):
                            got.add(f.name.lower())
                    except OSError:
                        pass
                    if len(got) >= limit:
                        break
        except OSError:
            pass
        if len(want & got) >= need:
            break
    return got


def locate_candidates(target_name, names, count_hint=0, max_depth=6, deadline=None, limit=10):
    """
    浏览器拖拽不给绝对路径，这里靠"文件夹名 + 里面有哪些文件"反查回来。

    关键：**不能命中即停**。同一批照片经常在不同盘上各有一份拷贝，
    谁先被扫到就选谁，会把用户拖的 E 盘错认成桌面上的副本，
    所以这里把所有同名候选都收上来，交给界面让用户挑。
    """
    want = set(n.lower() for n in names if n)
    tn = (target_name or "").strip().lower()
    if not tn and not want:
        return []
    need = max(1, min(len(want), 3))
    budget = deadline or (time.time() + 8.0)
    hits = []
    seen = set()

    def walk(path, depth):
        if time.time() > budget or len(hits) >= limit:
            return
        key = unlong(path).lower()
        if key in seen:
            return
        seen.add(key)
        try:
            with os.scandir(long_path(path)) as it:
                entries = list(it)
        except OSError:
            return
        files = set()
        subs = []
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    subs.append(e)
                elif e.name and not e.name.startswith("."):
                    files.add(e.name.lower())
            except OSError:
                pass
        base = os.path.basename(unlong(path)).lower()
        # 只认名字对得上的目录；有文件名指纹时再核对内容，没指纹就只认名字
        if base and base == tn:
            match = len(want & files)
            if want and match < need and subs:
                match = len(want & (files | sample_names(unlong(path), subs, want, need)))
            if not want or match >= need:
                raw, jpg = count_photos(unlong(path))
                hits.append({
                    "path": unlong(path),
                    "match": match,
                    "files": len(files),
                    "raw": raw,
                    "jpg": jpg,
                })
        if depth <= 0:
            return
        for e in subs:
            low = e.name.lower()
            if low in SKIP_DIRS or low.startswith(".") or low.startswith("$"):
                continue
            walk(e.path, depth - 1)

    near, drives = locate_roots()
    for p in near:
        walk(p, 4)
    for d in drives:
        walk(d, max_depth)

    if not hits:
        return []
    # 有照片的优先，其次文件名匹配得多的，再次路径短的（短路径更像用户直接拖的那层）
    # 排序决定了"自动选哪一个"，所以规则要保证 RAW 和 JPG 落到同一处：
    # 有照片的优先 → 文件名匹配多的 → 目录层级浅的 → 路径短的 → 照片多的
    # （层级浅/路径短能让同一批照片的多份拷贝里，始终挑同一盘的同一层）
    hits.sort(key=lambda h: (-(1 if (h["raw"] + h["jpg"]) > 0 else 0),
                             -h["match"],
                             unlong(h["path"]).count(os.sep),
                             len(h["path"]),
                             -(h["raw"] + h["jpg"])))
    withphoto = [h for h in hits if (h["raw"] + h["jpg"]) > 0]
    return withphoto or hits


def locate_folder(target_name, names, count_hint=0, max_depth=6, deadline=None):
    return [h["path"] for h in locate_candidates(target_name, names, count_hint, max_depth, deadline)]



# ---------------------------------------------------------------- 复制任务


class CopyTask(object):
    def __init__(self, dst, items, concurrency=4):
        self.dst = dst
        self.items = items
        self.concurrency = max(1, min(int(concurrency or 4), 16))
        self.lock = threading.Lock()
        self.total = 0
        self.done = 0
        self.ok = 0
        self.skip = 0
        self.fails = []
        self.doneBytes = 0
        self.okBytes = 0
        self.totalBytes = 0
        self.cur = ""
        self.finished = False
        self.cancelled = False
        self.started = time.time()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    # -- 计划：一次扫描目标目录，定好每个文件的落点
    def _build_plan(self):
        os.makedirs(self.dst, exist_ok=True)
        exist = {}
        try:
            with os.scandir(self.dst) as it:
                for e in it:
                    try:
                        if e.is_file(follow_symlinks=False):
                            exist[e.name.lower()] = e.stat().st_size
                    except OSError:
                        pass
        except OSError:
            pass
        reserved = set(exist.keys())
        plan = []
        skip = 0
        for it in self.items:
            name = it.get("name") or os.path.basename(it.get("src") or "")
            if not name:
                continue
            size = it.get("size") or 0
            if exist.get(name.lower()) == size and size > 0:
                skip += 1
                continue
            final = name
            n = 0
            while final.lower() in reserved:
                n += 1
                final = add_suffix(name, n)
            reserved.add(final.lower())
            plan.append((it.get("src") or "", final))
        return plan, skip

    def _run(self):
        try:
            plan, skip = self._build_plan()
        except Exception as e:  # noqa: BLE001
            with self.lock:
                self.fails.append("准备目标文件夹失败：%s" % e)
                self.finished = True
            return
        with self.lock:
            self.total = len(plan)
            self.skip = skip
            self.totalBytes = 0
            for it, _ in plan:
                try:
                    self.totalBytes += os.path.getsize(long_path(it))
                except OSError:
                    pass
        if not plan:
            with self.lock:
                self.finished = True
            return

        cursor = [0]

        def worker():
            while True:
                with self.lock:
                    if self.cancelled:
                        return
                    i = cursor[0]
                    cursor[0] += 1
                if i >= len(plan):
                    return
                src, name = plan[i]
                out = os.path.join(self.dst, name)
                with self.lock:
                    self.cur = name
                size = 0
                try:
                    size = os.path.getsize(long_path(src))
                except OSError:
                    size = 0
                ok = False
                try:
                    if not os.path.isfile(long_path(src)):
                        raise FileNotFoundError("源文件不见了")
                    if not _CopyFileW(long_path(src), long_path(out), False):
                        err = ctypes.get_last_error()
                        raise OSError(err, "系统复制失败（错误码 %d）" % err)
                    ok = True
                except Exception as e:  # noqa: BLE001
                    with self.lock:
                        self.fails.append("%s：%s" % (name, e))
                with self.lock:
                    if ok:
                        self.ok += 1
                        self.okBytes += size
                    self.done += 1
                    self.doneBytes += size

        threads = [
            threading.Thread(target=worker, daemon=True)
            for _ in range(min(self.concurrency, len(plan)))
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with self.lock:
            self.finished = True
            self.cur = ""

    def status(self):
        with self.lock:
            return {
                "total": self.total,
                "done": self.done,
                "ok": self.ok,
                "skip": self.skip,
                "fails": list(self.fails),
                "doneBytes": self.doneBytes,
                "okBytes": self.okBytes,
                "totalBytes": self.totalBytes,
                "cur": self.cur,
                "finished": self.finished,
                "cancelled": self.cancelled,
                "elapsed": round(time.time() - self.started, 2),
            }

    def cancel(self):
        with self.lock:
            self.cancelled = True


# ---------------------------------------------------------------- 资源与状态


def res_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def load_html():
    with open(res_path("ui.html"), "r", encoding="utf-8") as f:
        raw = f.read()
    # 注入桌面版标记：页面里的适配层只有看到这个标记才启用
    marker = "<script>window.__RAW_NATIVE__=1;</script>\n"
    if "<head>" in raw:
        raw = raw.replace("<head>", "<head>\n" + marker, 1)
    else:
        raw = marker + raw
    return raw.encode("utf-8")


HTML_BYTES = b""
COPY = {"task": None}
SERVER_LOG = []


def srv_note(text):
    try:
        server_log = SERVER_LOG
        server_log.append(time.strftime("%H:%M:%S") + " " + str(text))
        if len(server_log) > 30:
            del server_log[:-30]
    except Exception:  # noqa: BLE001
        pass


LAST_BEAT = [time.time()]


# ---------------------------------------------------------------- HTTP 服务


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "RawMatcherDesktop"

    def log_message(self, *args):
        pass

    # -- 小工具
    def _send(self, code, body, ctype="application/octet-stream", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > 64 * 1024 * 1024:
            raise ValueError("请求过大")
        return json.loads(self.rfile.read(n).decode("utf-8"))

    # -- GET
    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        path = u.path
        if path in ("/", "/index.html"):
            return self._send(200, HTML_BYTES, "text/html; charset=utf-8")
        if path == "/api/copy/status":
            task = COPY.get("task")
            if not task:
                return self._json({"total": 0, "done": 0, "ok": 0, "skip": 0, "fails": [],
                                   "doneBytes": 0, "okBytes": 0, "totalBytes": 0,
                                   "cur": "", "finished": True, "cancelled": False})
            return self._json(task.status())
        if path == "/api/head":
            q = urllib.parse.parse_qs(u.query)
            p = q.get("p", [""])[0]
            try:
                s = max(0, int(q.get("s", ["0"])[0]))
                e = max(0, int(q.get("e", ["0"])[0]))
            except ValueError:
                return self._send(400, "bad range", "text/plain")
            if not p or e <= s:
                return self._send(400, "bad request", "text/plain")
            e = min(e, s + MAX_HEAD_BYTES)
            try:
                with open(long_path(p), "rb") as f:
                    f.seek(s)
                    data = f.read(e - s)
            except OSError as ex:
                return self._send(404, str(ex), "text/plain; charset=utf-8")
            return self._send(200, data)
        if path == "/api/dropinfo":
            try:
                info = droptarget.debug_info()
                info["server_log"] = list(SERVER_LOG)
                return self._json(info)
            except Exception as e:  # noqa: BLE001
                return self._json({"error": str(e)})
        if path == "/favicon.ico":
            return self._send(204, b"")
        return self._send(404, "not found", "text/plain")

    # -- POST
    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        path = u.path
        try:
            body = self._read_json()
        except Exception as e:  # noqa: BLE001
            return self._json({"error": "参数错误：%s" % e}, 400)

        LAST_BEAT[0] = time.time()

        if path == "/api/heartbeat":
            return self._json({"ok": True})

        if path == "/api/pick":
            title = body.get("title") or "选择文件夹"
            try:
                p = pick_folder(title)
            except Exception as e:  # noqa: BLE001
                return self._json({"error": "打开对话框失败：%s" % e}, 500)
            return self._json({"path": p or ""})

        if path == "/api/classify":
            p = os.path.normpath(body.get("path") or "")
            if not p or not os.path.isdir(long_path(p)):
                return self._json({"ok": False, "error": "不是文件夹，或没有访问权限"})
            try:
                raw, jpg, other = classify_folder(p)
            except OSError as e:
                return self._json({"ok": False, "error": "读取失败：%s" % e})
            return self._json({"ok": True, "raw": raw, "jpg": jpg, "other": other,
                               "name": os.path.basename(p.rstrip("\\/")) or p})

        if path == "/api/locate":
            name = body.get("name") or ""
            names = body.get("names") or []
            count_hint = int(body.get("count") or 0)
            try:
                cands = locate_candidates(name, names, count_hint)
            except Exception as e:  # noqa: BLE001
                return self._json({"paths": [], "cands": [], "error": str(e)})
            return self._json({"paths": [c["path"] for c in cands], "cands": cands})

        if path == "/api/_testdrop" and os.environ.get("RAWM_SELFTEST"):
            # 自测专用：在本进程里造一个真 HDROP 投给自己，验证整条链路
            ok = droptarget.inject_drop(body.get("path") or "", body.get("slot"))
            srv_note("注入自测 path=%r ok=%s" % (body.get("path"), ok))
            return self._json({"ok": bool(ok)})

        if path == "/api/zones":
            rects = body.get("rects") or {}
            clean = {}
            for k in ("raw", "jpg"):
                v = rects.get(k)
                if isinstance(v, list) and len(v) == 4:
                    clean[k] = v
            card = body.get("card")
            if not (isinstance(card, list) and len(card) == 4):
                card = None
            holes = []
            for h in (body.get("holes") or []):
                if isinstance(h, list) and len(h) == 4:
                    holes.append(h)
            try:
                droptarget.set_zones(clean)
                droptarget.set_card(card, holes)
                droptarget.set_viewport(body.get("vh"), body.get("dpr"))
            except Exception as exc:  # noqa: BLE001
                # 别再静默吞：接口写错时这里必须留下痕迹，否则页面看起来"没反应"
                srv_note("zones 处理失败：%r" % (exc,))
            return self._json({"ok": True})

        if path == "/api/dropped":
            out = []
            for it in droptarget.take_drops():
                p = unlong(it.get("path") or "")
                if not p:
                    continue
                if not os.path.isdir(long_path(p)):
                    # 拖的是文件夹里的文件：退到它所在的文件夹
                    p = os.path.dirname(p.rstrip("\\/"))
                if p and os.path.isdir(long_path(p)):
                    out.append({"path": p, "slot": it.get("slot")})
                else:
                    srv_note("dropped 丢弃：不是文件夹 %r" % p)
            if out:
                srv_note("dropped 交出 %s" % [o["path"] for o in out])
            return self._json({"items": out})

        if path == "/api/scan":
            root = os.path.normpath(body.get("path") or "")
            recursive = bool(body.get("recursive"))
            if not root or not os.path.isdir(long_path(root)):
                return self._json({"error": "文件夹不存在或没有权限：%s" % root}, 400)
            files = []
            try:
                for dirpath, dirnames, filenames in os.walk(root):
                    rel = os.path.relpath(dirpath, root)
                    if rel == ".":
                        rel = ""
                    depth = 0 if not rel else rel.count(os.sep) + 1
                    dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                    if not recursive and depth > 0:
                        dirnames[:] = []
                        continue
                    for fn in filenames:
                        if fn.startswith("."):
                            continue
                        full = os.path.join(dirpath, fn)
                        try:
                            sz = os.path.getsize(long_path(full))
                        except OSError:
                            continue
                        relp = (rel.replace(os.sep, "/") + "/" if rel else "") + fn
                        files.append({"name": fn, "rel": relp, "abs": full, "size": sz})
            except OSError as e:
                return self._json({"error": "读取文件夹失败：%s" % e}, 500)
            return self._json({"files": files})

        if path == "/api/copy":
            dst = os.path.normpath(body.get("dst") or "")
            items = body.get("items") or []
            if not dst:
                return self._json({"error": "缺少目标文件夹"}, 400)
            if not items:
                return self._json({"error": "没有要复制的文件"}, 400)
            prev = COPY.get("task")
            if prev and not prev.finished:
                return self._json({"error": "上一批还在复制中，请先等它结束或点停止。"}, 409)
            try:
                os.makedirs(dst, exist_ok=True)
            except OSError as e:
                return self._json({"error": "无法创建目标文件夹：%s" % e}, 400)
            COPY["task"] = CopyTask(dst, items, body.get("concurrency") or 4)
            return self._json({"ok": True})

        if path == "/api/copy/cancel":
            task = COPY.get("task")
            if task:
                task.cancel()
            return self._json({"ok": True})

        if path == "/api/save":
            name = body.get("name") or "导出.txt"
            text = body.get("text") or ""
            try:
                target = save_as("保存 %s" % name, name)
            except Exception as e:  # noqa: BLE001
                return self._json({"error": "打开保存对话框失败：%s" % e}, 500)
            if not target:
                return self._json({"path": ""})
            try:
                with open(long_path(target), "w", encoding="utf-8-sig", newline="") as f:
                    f.write(text)
            except OSError as e:
                return self._json({"error": "写入失败：%s" % e}, 500)
            return self._json({"path": target})

        if path == "/api/open":
            p = body.get("path") or ""
            if p and os.path.isdir(long_path(p)):
                try:
                    os.startfile(p)  # noqa: S606
                except OSError:
                    pass
            return self._json({"ok": True})

        if path == "/api/quit":
            threading.Timer(0.3, lambda: os._exit(0)).start()
            return self._json({"ok": True})

        return self._json({"error": "unknown api"}, 404)


# ---------------------------------------------------------------- 启动流程


def port_in_use(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except OSError:
        return False


def find_browser():
    env = os.environ
    cands = [
        os.path.join(env.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                     r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(env.get("ProgramFiles", r"C:\Program Files"),
                     r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(env.get("LOCALAPPDATA", ""), r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(env.get("ProgramFiles", r"C:\Program Files"),
                     r"Google\Chrome\Application\chrome.exe"),
        os.path.join(env.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                     r"Google\Chrome\Application\chrome.exe"),
        os.path.join(env.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
    ]
    for c in cands:
        try:
            if c and os.path.isfile(c):
                return c
        except OSError:
            pass
    try:
        import winreg
        for hive, sub in (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
        ):
            try:
                with winreg.OpenKey(hive, sub) as k:
                    p = winreg.QueryValue(k, None)
                if p and os.path.isfile(p):
                    return p
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        pass
    return None


def _pid_alive(pid):
    try:
        h = _OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        _CloseHandle(h)
        return True
    except Exception:  # noqa: BLE001
        return False


def cleanup_profiles(keep=None):
    """清掉历史遗留的浏览器临时配置目录，但保留其他活实例正在用的目录。

    多开时若按旧逻辑一律删光，后启动的实例会把先启动实例的 profile 删掉，
    先开的窗口可能直接崩（2026-11-05 修多开时一并处理）。
    """
    tmp = tempfile.gettempdir()
    try:
        for name in os.listdir(tmp):
            if not name.startswith("rawmatcher_profile_") or name == keep:
                continue
            try:
                owner = int(name.rsplit("_", 1)[1])
                if _pid_alive(owner):
                    continue
            except (ValueError, IndexError):
                pass
            shutil.rmtree(os.path.join(tmp, name), ignore_errors=True)
    except OSError:
        pass


def open_window(url):
    if os.environ.get("RAWM_NO_WINDOW"):
        return None
    exe = find_browser()
    profile = os.path.join(tempfile.gettempdir(), "rawmatcher_profile_%d" % os.getpid())
    cleanup_profiles(keep=os.path.basename(profile))
    if not exe:
        webbrowser.open(url)
        return None
    args = [
        exe,
        "--app=" + url,
        "--window-size=1180,980",
        "--user-data-dir=" + profile,
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=TranslateUI,msEdgeSplitScreen",
        "--disable-sync",
        "--disable-background-mode",
    ]
    try:
        flags = 0
        if sys.platform == "win32":
            flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        return subprocess.Popen(args, creationflags=flags, close_fds=True)
    except OSError:
        webbrowser.open(url)
        return None


def main():
    global HTML_BYTES
    # 本进程按物理像素算坐标，透明拖拽层才能精确盖在网页里那两个路径框上
    try:
        _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:  # noqa: BLE001
            pass
    HTML_BYTES = load_html()

    port = int(os.environ.get("RAWM_PORT") or DEFAULT_PORT)
    if port_in_use(port) and not os.environ.get("RAWM_PORT"):
        # 已有实例占着 47311：绝不能只 open_window 就 return。
        # 那样新窗口连的是旧服务（旧页面），而且 droptarget.start() 在下面被跳过，
        # 窗口没有原生拖拽层 —— 拖进去一个文件夹都认不到（2026-11-05 实测根因）。
        # 改为让系统分配一个空闲端口，起一套完整独立的 服务 + 窗口 + 拖拽层。
        port = 0

    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    httpd.daemon_threads = True

    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    url = "http://127.0.0.1:%d/?v=%d" % (port, int(time.time()))
    proc = open_window(url)
    if not os.environ.get("RAWM_NO_WINDOW"):
        # 盖在路径框上的原生拖拽层：路径由 Windows 直接给，不再靠猜
        try:
            droptarget.start(APP_TITLE, proc.pid if proc else None)
        except Exception:  # noqa: BLE001
            pass
    grace = time.time() + 120  # 页面加载期给足宽限

    def shutdown():
        # 顺手把自己拉起来的浏览器收掉：否则它自己不会退，桌面上会留一个
        # 打着「RAW 选片工具」标题、里面却是死页面的幽灵窗口，
        # 下次开新实例时拖拽层还可能认到它头上。
        try:
            if proc is not None and proc.poll() is None:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               creationflags=0x08000000, capture_output=True)
        except Exception:  # noqa: BLE001
            pass
        cleanup_profiles(keep=None)
        os._exit(0)

    def watchdog():
        while True:
            time.sleep(1.0)
            now = time.time()
            if proc is not None and proc.poll() is not None and not _has_client():
                # 窗口进程已退出且没有新连接
                time.sleep(0.4)
                if not _has_client():
                    shutdown()
            if now > grace and now - LAST_BEAT[0] > 90:  # 兜底：窗口被强杀时靠心跳判断
                shutdown()

    threading.Thread(target=watchdog, daemon=True).start()

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def _has_client():
    return (time.time() - LAST_BEAT[0]) < 8


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        # 桌面版没有控制台，出错时弹个框，别让窗口一闪就没
        try:
            ctypes.windll.user32.MessageBoxW(None, "启动失败：\n%s" % exc, APP_TITLE, 0x10)
        except Exception:
            pass
        raise
