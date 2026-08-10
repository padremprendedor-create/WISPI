"""Todas las declaraciones ctypes de Win32 en UN solo sitio.

Por que existe este modulo:
    Los bugs clasicos de ctypes en Win64 son de TIPOS, no de logica, y fallan en
    silencio. `SetWindowsHookExW` con el restype por defecto (`c_int`) trunca el
    HHOOK a 32 bits, y luego `UnhookWindowsHookEx` no desengancha nada y no avisa.
    Lo mismo con `CallNextHookEx` (LRESULT = LONG_PTR = 64 bits) y con
    `GetModuleHandleW` (HMODULE = puntero).

    Declarar `argtypes` y `restype` exactamente una vez, aqui, elimina toda esa
    clase de fallos de golpe. Ningun otro modulo debe llamar a `ctypes.windll`.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

# ---------------------------------------------------------------------------
# Tipos base
# ---------------------------------------------------------------------------
# En Win64: LRESULT/LPARAM = LONG_PTR (64 bits con signo), WPARAM/ULONG_PTR = 64 sin signo.
LRESULT = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t
HHOOK = wintypes.HANDLE
HWND = wintypes.HWND
HANDLE = wintypes.HANDLE

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
WH_KEYBOARD_LL = 13

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012

# Flags de KBDLLHOOKSTRUCT.flags
LLKHF_EXTENDED = 0x01
LLKHF_LOWER_IL_INJECTED = 0x02
LLKHF_INJECTED = 0x10
LLKHF_ALTDOWN = 0x20
LLKHF_UP = 0x80

# Virtual-Key codes que usamos
VK_BACK = 0x08          # Retroceso
VK_TAB = 0x09
VK_RETURN = 0x0D        # Enter principal (el del numerico SI es extendido)
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_PRIOR = 0x21         # Re Pag
VK_NEXT = 0x22          # Av Pag
VK_END = 0x23
VK_HOME = 0x24
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_INSERT = 0x2D
VK_DELETE = 0x2E        # Supr
VK_A = 0x41
VK_C = 0x43
VK_X = 0x58
VK_Y = 0x59
VK_Z = 0x5A

# Teclas con prefijo E0 en el scancode. Sin KEYEVENTF_EXTENDEDKEY, Windows mapea
# el VK al scancode del teclado NUMERICO: con Bloq Num puesto, "flecha izquierda"
# escribe un 4 y "Supr" escribe una coma. Es el mismo fallo que hacia que
# Shift+Insert escribiera un 0 en vez de pegar.
EXTENDED_VKS = frozenset({
    VK_PRIOR, VK_NEXT, VK_END, VK_HOME,
    VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN,
    VK_INSERT, VK_DELETE,
})
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_V = 0x56
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4
VK_RMENU = 0xA5  # AltGr en teclado espanol: NUNCA usar como hotkey
VK_PACKET = 0xE7
# 0xE8 esta SIN ASIGNAR en Windows: no hace nada en ninguna aplicacion.
# Lo usamos como "tecla intermedia" para que Windows no abra el menu Inicio
# al soltar Win. Ver inject/sendinput.py::suppress_start_menu.
VK_UNASSIGNED_E8 = 0xE8

# SendInput
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
INPUT_HARDWARE = 2

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

# Firma propia en dwExtraInfo. Todo input que emitimos va marcado con esto para
# que nuestro propio hook lo ignore y no se auto-dispare. "WISP" en ASCII.
WISPI_EXTRAINFO = 0x57495350

# Portapapeles
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

# Procesos
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# Estilos extendidos de ventana, para el boton flotante.
GWL_EXSTYLE = -20
# WS_EX_NOACTIVATE es LA clave del boton: hace que pulsarlo NO cambie la ventana
# activa. Sin el, al hacer clic el foreground pasa a ser el propio boton y el
# dictado se pegaria dentro de WISPI en vez de en lo que el usuario estaba
# escribiendo. No es cosmetica: es lo que hace que el boton sea usable.
WS_EX_NOACTIVATE = 0x08000000
# Fuera de Alt-Tab y de la barra de tareas: es un accesorio, no una app.
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_LAYERED = 0x00080000

# ShowWindow: mostrar SIN activar. WS_EX_NOACTIVATE impide que un CLIC active la
# ventana, pero no impide que ShowWindow/deiconify lo haga al mapearla.
SW_SHOWNOACTIVATE = 4

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = -1

# ---------------------------------------------------------------------------
# Structs
# ---------------------------------------------------------------------------


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]


# Sanity check en import: en x64 sizeof(INPUT) tiene que ser 40 (4 de type +
# 4 de padding + 32 de la union). Si esto falla, SendInput devolveria 0 siempre.
assert ctypes.sizeof(INPUT) in (28, 40), f"sizeof(INPUT) inesperado: {ctypes.sizeof(INPUT)}"

# Prototipo del callback del hook. WINFUNCTYPE = __stdcall (correcto en x86;
# en x64 solo hay una convencion, pero dejarlo explicito es gratis).
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

# ---------------------------------------------------------------------------
# Prototipos: hooks y bucle de mensajes
# ---------------------------------------------------------------------------
user32.SetWindowsHookExW.restype = HHOOK
user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD)

user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.UnhookWindowsHookEx.argtypes = (HHOOK,)

user32.CallNextHookEx.restype = LRESULT
user32.CallNextHookEx.argtypes = (HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

# GetMessageW devuelve -1 en error, por eso c_int y no BOOL sin signo.
user32.GetMessageW.restype = ctypes.c_int
user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), HWND, wintypes.UINT, wintypes.UINT)

user32.TranslateMessage.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)

user32.DispatchMessageW.restype = LRESULT
user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)

user32.PostThreadMessageW.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)

kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.GetCurrentThreadId.argtypes = ()

# ---------------------------------------------------------------------------
# Prototipos: input sintetico
# ---------------------------------------------------------------------------
user32.SendInput.restype = wintypes.UINT
user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)

user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)

user32.GetKeyState.restype = ctypes.c_short
user32.GetKeyState.argtypes = (ctypes.c_int,)

# ---------------------------------------------------------------------------
# Prototipos: ventana en foco y proceso
# ---------------------------------------------------------------------------
user32.GetForegroundWindow.restype = HWND
user32.GetForegroundWindow.argtypes = ()

user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowThreadProcessId.argtypes = (HWND, ctypes.POINTER(wintypes.DWORD))

user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = (HWND, wintypes.LPWSTR, ctypes.c_int)

# GetWindowLongPtrW solo existe en Win64; en Win32 el nombre es GetWindowLongW y
# devuelve LONG. Se resuelve una vez, aqui, en vez de en cada llamada.
if hasattr(user32, "GetWindowLongPtrW"):
    _GetWindowLong = user32.GetWindowLongPtrW
    _SetWindowLong = user32.SetWindowLongPtrW
    _LONG_PTR = ctypes.c_ssize_t
else:  # pragma: no cover - solo en Python de 32 bits
    _GetWindowLong = user32.GetWindowLongW
    _SetWindowLong = user32.SetWindowLongW
    _LONG_PTR = wintypes.LONG

_GetWindowLong.restype = _LONG_PTR
_GetWindowLong.argtypes = (HWND, ctypes.c_int)
_SetWindowLong.restype = _LONG_PTR
_SetWindowLong.argtypes = (HWND, ctypes.c_int, _LONG_PTR)

user32.GetParent.restype = HWND
user32.GetParent.argtypes = (HWND,)

user32.ShowWindow.restype = wintypes.BOOL
user32.ShowWindow.argtypes = (HWND, ctypes.c_int)

user32.SetForegroundWindow.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = (HWND,)

user32.IsWindow.restype = wintypes.BOOL
user32.IsWindow.argtypes = (HWND,)

user32.SetWindowPos.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = (HWND, HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT)

kernel32.OpenProcess.restype = HANDLE
kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)

kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = (
    HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
)

kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = (HANDLE,)

# ---------------------------------------------------------------------------
# Prototipos: portapapeles
# ---------------------------------------------------------------------------
user32.OpenClipboard.restype = wintypes.BOOL
user32.OpenClipboard.argtypes = (HWND,)

user32.CloseClipboard.restype = wintypes.BOOL
user32.CloseClipboard.argtypes = ()

user32.EmptyClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.argtypes = ()

user32.GetClipboardData.restype = HANDLE
user32.GetClipboardData.argtypes = (wintypes.UINT,)

user32.SetClipboardData.restype = HANDLE
user32.SetClipboardData.argtypes = (wintypes.UINT, HANDLE)

user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
user32.IsClipboardFormatAvailable.argtypes = (wintypes.UINT,)

user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
user32.GetClipboardSequenceNumber.argtypes = ()

kernel32.GlobalAlloc.restype = HANDLE
kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)

kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = (HANDLE,)

kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalUnlock.argtypes = (HANDLE,)

kernel32.GlobalFree.restype = HANDLE
kernel32.GlobalFree.argtypes = (HANDLE,)

kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.GlobalSize.argtypes = (HANDLE,)


# ---------------------------------------------------------------------------
# Helpers de alto nivel (sin estado, seguros de llamar desde cualquier hilo)
# ---------------------------------------------------------------------------
def foreground_hwnd() -> int:
    """HWND de la ventana en primer plano, o 0."""
    return user32.GetForegroundWindow() or 0


def pid_of_hwnd(hwnd: int) -> int:
    """PID dueno de una ventana, o 0."""
    if not hwnd:
        return 0
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def exe_of_pid(pid: int) -> str:
    """Nombre del ejecutable (basename, minusculas) de un PID. '' si no se puede.

    Usa PROCESS_QUERY_LIMITED_INFORMATION (0x1000) a proposito: es el unico
    derecho que funciona contra procesos elevados desde un proceso normal.
    Esto evita depender de psutil.
    """
    if not pid:
        return ""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ""
        full = buf.value
    finally:
        kernel32.CloseHandle(h)
    return full.rsplit("\\", 1)[-1].lower() if full else ""


def window_title(hwnd: int, maxlen: int = 256) -> str:
    """Titulo de la ventana. Solo para diagnostico; nunca se registra en logs."""
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(maxlen)
    user32.GetWindowTextW(hwnd, buf, maxlen)
    return buf.value


def is_physically_down(vk: int) -> bool:
    """True si la tecla esta fisicamente pulsada AHORA (bit alto de GetAsyncKeyState)."""
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def make_click_through_overlay(hwnd: int) -> bool:
    """Convierte una ventana en accesorio flotante que NO roba el foco.

    Es lo que hace usable el boton flotante: sin WS_EX_NOACTIVATE, pulsarlo
    convierte al propio boton en la ventana activa, y entonces el dictado se
    pegaria dentro de WISPI en vez de en el documento del usuario.

    Anade ademas WS_EX_TOOLWINDOW para que no aparezca en Alt-Tab ni en la barra
    de tareas: es un accesorio, no una aplicacion que se elige.
    """
    if not hwnd:
        return False
    style = _GetWindowLong(hwnd, GWL_EXSTYLE)
    nuevo = style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    if nuevo == style:
        return True
    ctypes.set_last_error(0)
    res = _SetWindowLong(hwnd, GWL_EXSTYLE, nuevo)
    # SetWindowLong devuelve el valor ANTERIOR; un 0 legitimo es indistinguible
    # de un fallo, asi que hay que mirar GetLastError.
    return bool(res) or ctypes.get_last_error() == 0


def show_without_activating(hwnd: int) -> bool:
    """Hace visible la ventana sin darle el primer plano."""
    if not hwnd:
        return False
    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
    return bool(user32.SetWindowPos(
        hwnd, ctypes.cast(HWND_TOPMOST, HWND), 0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW))


def restore_foreground(hwnd: int) -> bool:
    """Devuelve el primer plano a `hwnd`.

    Red de seguridad para cuando mostrar el boton se lleva el foco de todas
    formas. Windows solo deja regalar el foreground al proceso que ya lo tiene,
    que es justo nuestro caso en ese instante.
    """
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    return bool(user32.SetForegroundWindow(hwnd))


def keep_on_top(hwnd: int) -> bool:
    """Reafirma el topmost sin activar la ventana ni moverla."""
    if not hwnd:
        return False
    return bool(user32.SetWindowPos(
        hwnd, ctypes.cast(HWND_TOPMOST, HWND), 0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE))
