"""GDI printing through the normal Windows print dialog.

The device context origin is the top-left of the *printable* area, not of the
paper. PHYSICALOFFSETX/Y is the hardware margin. Drawing the full-page image
at (-offset) puts millimetre (0, 0) on the physical corner of the sheet, so
answers land on the preprinted form. A user correction is added on top of that.
"""

from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes
from dataclasses import dataclass

import win32con
import win32print
import win32ui
from PIL import Image, ImageWin

# Common-dialog flags. The dialog is the usual printer window, with Einstellungen.
_PD_NOSELECTION = 0x00000004
_PD_NOPAGENUMS = 0x00000008
_PD_RETURNDC = 0x00000100
_PD_RETURNDEFAULT = 0x00000400
_PD_USEDEVMODECOPIESANDCOLLATE = 0x00040000
_PD_HIDEPRINTTOFILE = 0x00100000
_DM_COPIES = 0x00000100
_GMEM_MOVEABLE = 0x0002
_MM_TEXT = 1


def list_printers() -> list[str]:
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    return sorted(p[2] for p in win32print.EnumPrinters(flags))


def default_printer() -> str:
    try:
        return win32print.GetDefaultPrinter()
    except Exception:
        return ""


def printer_metrics(printer_name: str) -> dict:
    pdc = win32ui.CreateDC()
    pdc.CreatePrinterDC(printer_name)
    try:
        dpi_x = pdc.GetDeviceCaps(win32con.LOGPIXELSX)
        dpi_y = pdc.GetDeviceCaps(win32con.LOGPIXELSY)
        off_x = pdc.GetDeviceCaps(win32con.PHYSICALOFFSETX)
        off_y = pdc.GetDeviceCaps(win32con.PHYSICALOFFSETY)
        phys_w = pdc.GetDeviceCaps(win32con.PHYSICALWIDTH)
        phys_h = pdc.GetDeviceCaps(win32con.PHYSICALHEIGHT)
        print_w = pdc.GetDeviceCaps(win32con.HORZRES)
        print_h = pdc.GetDeviceCaps(win32con.VERTRES)
    finally:
        pdc.DeleteDC()

    def mm(px, dpi):
        return px / dpi * 25.4

    return {
        "dpi_x": dpi_x,
        "dpi_y": dpi_y,
        "off_x": off_x,
        "off_y": off_y,
        "phys_w": phys_w,
        "phys_h": phys_h,
        "paper_mm": (mm(phys_w, dpi_x), mm(phys_h, dpi_y)),
        "margin_mm": (
            mm(off_x, dpi_x),
            mm(off_y, dpi_y),
            mm(phys_w - print_w - off_x, dpi_x),
            mm(phys_h - print_h - off_y, dpi_y),
        ),
    }


@dataclass
class PrintChoice:
    """What the user confirmed in the print dialog."""

    printer_name: str
    devmode: bytes
    page_loops: int
    reported_copies: int


class _PRINTDLGW(ctypes.Structure):
    _fields_ = [
        ("lStructSize", wintypes.DWORD),
        ("hwndOwner", wintypes.HWND),
        ("hDevMode", wintypes.HGLOBAL),
        ("hDevNames", wintypes.HGLOBAL),
        ("hDC", wintypes.HDC),
        ("Flags", wintypes.DWORD),
        ("nFromPage", wintypes.WORD),
        ("nToPage", wintypes.WORD),
        ("nMinPage", wintypes.WORD),
        ("nMaxPage", wintypes.WORD),
        ("nCopies", wintypes.WORD),
        ("hInstance", wintypes.HINSTANCE),
        ("lCustData", wintypes.LPARAM),
        ("lpfnPrintHook", ctypes.c_void_p),
        ("lpfnSetupHook", ctypes.c_void_p),
        ("lpPrintTemplateName", wintypes.LPCWSTR),
        ("lpSetupTemplateName", wintypes.LPCWSTR),
        ("hPrintTemplate", wintypes.HGLOBAL),
        ("hSetupTemplate", wintypes.HGLOBAL),
    ]


def _kernel32():
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    library.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    library.GlobalAlloc.restype = wintypes.HGLOBAL
    library.GlobalLock.argtypes = [wintypes.HGLOBAL]
    library.GlobalLock.restype = ctypes.c_void_p
    library.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    library.GlobalUnlock.restype = wintypes.BOOL
    library.GlobalFree.argtypes = [wintypes.HGLOBAL]
    library.GlobalFree.restype = wintypes.HGLOBAL
    library.GlobalSize.argtypes = [wintypes.HGLOBAL]
    library.GlobalSize.restype = ctypes.c_size_t
    return library


def _gdi32():
    library = ctypes.WinDLL("gdi32", use_last_error=True)
    library.CreateDCW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p]
    library.CreateDCW.restype = wintypes.HDC
    library.DeleteDC.argtypes = [wintypes.HDC]
    library.DeleteDC.restype = wintypes.BOOL
    library.GetDeviceCaps.argtypes = [wintypes.HDC, ctypes.c_int]
    library.GetDeviceCaps.restype = ctypes.c_int
    library.SetMapMode.argtypes = [wintypes.HDC, ctypes.c_int]
    library.SetMapMode.restype = ctypes.c_int
    library.StartDocW.restype = ctypes.c_int
    library.StartPage.argtypes = [wintypes.HDC]
    library.StartPage.restype = ctypes.c_int
    library.EndPage.argtypes = [wintypes.HDC]
    library.EndPage.restype = ctypes.c_int
    library.EndDoc.argtypes = [wintypes.HDC]
    library.EndDoc.restype = ctypes.c_int
    return library


def _comdlg32():
    library = ctypes.WinDLL("comdlg32", use_last_error=True)
    library.PrintDlgW.argtypes = [ctypes.POINTER(_PRINTDLGW)]
    library.PrintDlgW.restype = wintypes.BOOL
    library.CommDlgExtendedError.restype = wintypes.DWORD
    return library


def _global_copy(data: bytes):
    kernel = _kernel32()
    handle = kernel.GlobalAlloc(_GMEM_MOVEABLE, len(data))
    if not handle:
        raise OSError("Der Druckdialog konnte den Druckspeicher nicht anlegen.")
    pointer = kernel.GlobalLock(handle)
    if not pointer:
        kernel.GlobalFree(handle)
        raise OSError("Der Druckdialog konnte den Druckspeicher nicht lesen.")
    ctypes.memmove(pointer, data, len(data))
    kernel.GlobalUnlock(handle)
    return handle


def _global_bytes(handle) -> bytes:
    if not handle:
        return b""
    kernel = _kernel32()
    pointer = kernel.GlobalLock(handle)
    if not pointer:
        return b""
    try:
        return ctypes.string_at(pointer, kernel.GlobalSize(handle))
    finally:
        kernel.GlobalUnlock(handle)


def _devmode_bytes(printer_name: str, copies: int) -> bytes:
    winspool = ctypes.WinDLL("winspool.drv", use_last_error=True)
    winspool.DocumentPropertiesW.argtypes = [
        wintypes.HWND, wintypes.HANDLE, wintypes.LPCWSTR,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
    ]
    winspool.DocumentPropertiesW.restype = ctypes.c_long
    handle = win32print.OpenPrinter(printer_name)
    try:
        printer = int(handle)
        needed = winspool.DocumentPropertiesW(None, printer, printer_name, None, None, 0)
        if needed <= 0:
            raise OSError("Die Druckereinstellungen konnten nicht gelesen werden.")
        buffer = ctypes.create_string_buffer(needed)
        written = winspool.DocumentPropertiesW(None, printer, printer_name, buffer, None, 2)
        if written < 0:
            raise OSError("Die Druckereinstellungen konnten nicht gelesen werden.")
    finally:
        win32print.ClosePrinter(handle)
    data = bytearray(buffer.raw[:needed])
    if len(data) < 88:
        return bytes(data)
    fields = struct.unpack_from("<I", data, 72)[0] | _DM_COPIES
    struct.pack_into("<I", data, 72, fields)
    struct.pack_into("<h", data, 86, max(1, copies))
    return bytes(data)


def _devnames_bytes(driver: str, device: str, port: str) -> bytes:
    driver = driver or ""
    device = device or ""
    port = port or ""
    driver_at = 4
    device_at = driver_at + len(driver) + 1
    port_at = device_at + len(device) + 1
    header = struct.pack("<HHHH", driver_at, device_at, port_at, 0)
    text = f"{driver}\0{device}\0{port}\0".encode("utf-16-le")
    return header + text


def _device_from_devnames(blob: bytes) -> str:
    if len(blob) < 8:
        return ""
    _driver_at, device_at, _port_at, _default = struct.unpack_from("<HHHH", blob, 0)
    text = blob.decode("utf-16-le", errors="ignore")
    if device_at < 0 or device_at >= len(text):
        return ""
    return text[device_at:].split("\0", 1)[0]


def _devmode_used(blob: bytes) -> bytes:
    if len(blob) < 72:
        return blob
    size, extra = struct.unpack_from("<HH", blob, 68)
    used = size + extra
    if used <= 0 or used > len(blob):
        return blob
    return blob[:used]


def ask_print(owner_hwnd: int, printer_name: str, copies: int) -> PrintChoice | None:
    """Show the standard print dialog. None means the user cancelled."""
    dialog = _PRINTDLGW()
    dialog.lStructSize = ctypes.sizeof(_PRINTDLGW)
    dialog.hwndOwner = int(owner_hwnd or 0)
    dialog.Flags = (
        _PD_NOSELECTION
        | _PD_NOPAGENUMS
        | _PD_RETURNDC
        | _PD_USEDEVMODECOPIESANDCOLLATE
        | _PD_HIDEPRINTTOFILE
    )
    dialog.nCopies = max(1, min(20, int(copies or 1)))
    if printer_name:
        try:
            handle = win32print.OpenPrinter(printer_name)
            try:
                info = win32print.GetPrinter(handle, 2)
            finally:
                win32print.ClosePrinter(handle)
            dialog.hDevMode = _global_copy(_devmode_bytes(printer_name, dialog.nCopies))
            dialog.hDevNames = _global_copy(
                _devnames_bytes(info.get("pDriverName") or "", printer_name, info.get("pPortName") or "")
            )
        except OSError:
            if dialog.hDevMode:
                _kernel32().GlobalFree(dialog.hDevMode)
            if dialog.hDevNames:
                _kernel32().GlobalFree(dialog.hDevNames)
            dialog.hDevMode = 0
            dialog.hDevNames = 0

    comdlg = _comdlg32()
    kernel = _kernel32()
    gdi = _gdi32()
    try:
        if not comdlg.PrintDlgW(ctypes.byref(dialog)):
            if comdlg.CommDlgExtendedError():
                raise OSError("Der Druckdialog konnte nicht geöffnet werden.")
            return None
        devmode = _devmode_used(_global_bytes(dialog.hDevMode))
        chosen = _device_from_devnames(_global_bytes(dialog.hDevNames)) or printer_name
        if not devmode or not chosen:
            raise OSError("Der Druckdialog hat keine Druckereinstellung zurückgegeben.")
        if dialog.Flags & _PD_USEDEVMODECOPIESANDCOLLATE:
            reported = max(1, struct.unpack_from("<h", devmode, 86)[0]) if len(devmode) >= 88 else 1
            loops = 1
        else:
            loops = max(1, int(dialog.nCopies or 1))
            reported = loops
        return PrintChoice(chosen, devmode, loops, reported)
    finally:
        if dialog.hDC:
            gdi.DeleteDC(dialog.hDC)
        if dialog.hDevMode:
            kernel.GlobalFree(dialog.hDevMode)
        if dialog.hDevNames:
            kernel.GlobalFree(dialog.hDevNames)


def create_printer_dc(printer_name: str, devmode: bytes | None):
    """DC for this printer. A devmode from the print dialog keeps tray and private print."""
    gdi = _gdi32()
    mode = None
    if devmode:
        buffer = ctypes.create_string_buffer(devmode, len(devmode))
        mode = ctypes.cast(buffer, ctypes.c_void_p)
        hdc = gdi.CreateDCW("WINSPOOL", printer_name, None, mode)
        # Keep the buffer alive for the call; CreateDC copies the DEVMODE.
        del buffer
    else:
        hdc = gdi.CreateDCW("WINSPOOL", printer_name, None, None)
    if not hdc:
        raise OSError(f"Drucker „{printer_name}“ konnte nicht geöffnet werden.")
    return hdc


def metrics_from_hdc(hdc) -> dict:
    gdi = _gdi32()

    def caps(index: int) -> int:
        return int(gdi.GetDeviceCaps(hdc, index))

    dpi_x = caps(win32con.LOGPIXELSX)
    dpi_y = caps(win32con.LOGPIXELSY)
    off_x = caps(win32con.PHYSICALOFFSETX)
    off_y = caps(win32con.PHYSICALOFFSETY)
    phys_w = caps(win32con.PHYSICALWIDTH)
    phys_h = caps(win32con.PHYSICALHEIGHT)
    print_w = caps(win32con.HORZRES)
    print_h = caps(win32con.VERTRES)

    def mm(px, dpi):
        return px / dpi * 25.4

    return {
        "dpi_x": dpi_x,
        "dpi_y": dpi_y,
        "off_x": off_x,
        "off_y": off_y,
        "phys_w": phys_w,
        "phys_h": phys_h,
        "paper_mm": (mm(phys_w, dpi_x), mm(phys_h, dpi_y)),
        "margin_mm": (
            mm(off_x, dpi_x),
            mm(off_y, dpi_y),
            mm(phys_w - print_w - off_x, dpi_x),
            mm(phys_h - print_h - off_y, dpi_y),
        ),
    }


class _DOCINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_int),
        ("lpszDocName", wintypes.LPCWSTR),
        ("lpszOutput", wintypes.LPCWSTR),
        ("lpszDatatype", wintypes.LPCWSTR),
        ("fwType", wintypes.DWORD),
    ]


def draw_on_dc(
    hdc,
    page: Image.Image,
    copies: int,
    shift_x_mm: float,
    shift_y_mm: float,
    doc_name: str,
):
    metrics = metrics_from_hdc(hdc)
    image = page.convert("RGB")
    gdi = _gdi32()
    gdi.StartDocW.argtypes = [wintypes.HDC, ctypes.POINTER(_DOCINFOW)]
    gdi.SetMapMode(hdc, _MM_TEXT)
    dib = ImageWin.Dib(image)
    dx = -metrics["off_x"] + int(round(shift_x_mm / 25.4 * metrics["dpi_x"]))
    dy = -metrics["off_y"] + int(round(shift_y_mm / 25.4 * metrics["dpi_y"]))
    dest = (dx, dy, dx + image.width, dy + image.height)
    info = _DOCINFOW(ctypes.sizeof(_DOCINFOW), doc_name, None, None, 0)
    if gdi.StartDocW(hdc, ctypes.byref(info)) <= 0:
        raise OSError("Der Druckauftrag wurde vom Drucker nicht angenommen.")
    try:
        for _ in range(max(1, copies)):
            if gdi.StartPage(hdc) <= 0:
                raise OSError("Der Druckauftrag wurde vom Drucker nicht angenommen.")
            dib.draw(int(hdc), dest)
            if gdi.EndPage(hdc) < 0:
                raise OSError("Der Druckauftrag wurde vom Drucker nicht abgeschlossen.")
    finally:
        gdi.EndDoc(hdc)


def close_dc(hdc):
    if hdc:
        _gdi32().DeleteDC(hdc)


def print_page(
    page: Image.Image,
    printer_name: str,
    copies: int,
    shift_x_mm: float = 0.0,
    shift_y_mm: float = 0.0,
    doc_name: str = "Konsil Vordruck",
):
    """Send one full-page image. Pixel (0, 0) of `page` is the paper's top-left.

    `page` must be rendered at the printer's DPI and sized to the physical page
    (or to the form, pasted at the paper origin before this call).
    """
    metrics = printer_metrics(printer_name)
    image = page.convert("RGB")

    pdc = win32ui.CreateDC()
    pdc.CreatePrinterDC(printer_name)
    try:
        pdc.SetMapMode(win32con.MM_TEXT)
        dib = ImageWin.Dib(image)
        dx = -metrics["off_x"] + int(round(shift_x_mm / 25.4 * metrics["dpi_x"]))
        dy = -metrics["off_y"] + int(round(shift_y_mm / 25.4 * metrics["dpi_y"]))
        dest = (dx, dy, dx + image.width, dy + image.height)
        pdc.StartDoc(doc_name)
        try:
            for _ in range(max(1, copies)):
                pdc.StartPage()
                dib.draw(pdc.GetHandleOutput(), dest)
                pdc.EndPage()
        finally:
            pdc.EndDoc()
    finally:
        pdc.DeleteDC()
