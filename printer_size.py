"""
Label Printer PDF Cropper & Printer (silent, no external viewer)
-------------------------------------------------------------------
Pick a printer + a PDF, set copy count, click Print: the top-left
region of the PDF matching the printer's real printable area is
rendered and sent directly to the printer via the Windows GDI
printing API. No external PDF viewer or extra window is opened -
printing happens immediately and silently.

Requirements (run on the Windows machine with the printer installed):
    pip install pywin32 pypdfium2 pillow
"""

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import pypdfium2 as pdfium
import win32print
import win32ui
import win32con
from PIL import Image, ImageWin


def list_printers() -> list[str]:
    printers = win32print.EnumPrinters(
        win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    )
    return sorted(p[2] for p in printers)


LABEL_PRINTER_KEYWORDS = [
    "label", "zebra", "dymo", "brother ql", "tsc", "godex", "sato",
    "citizen", "intermec", "nslp", "lp-", "lp ",
]

_STANDARD_SIZES_MM = [
    (210, 297), (297, 210),
    (216, 279), (279, 216),
    (297, 420), (420, 297),
    (148, 210), (210, 148),
]


def get_physical_size_mm(printer_name: str) -> tuple[float, float]:
    pdc = win32ui.CreateDC()
    pdc.CreatePrinterDC(printer_name)
    dpi_x = pdc.GetDeviceCaps(win32con.LOGPIXELSX)
    dpi_y = pdc.GetDeviceCaps(win32con.LOGPIXELSY)
    phys_w = pdc.GetDeviceCaps(win32con.PHYSICALWIDTH)
    phys_h = pdc.GetDeviceCaps(win32con.PHYSICALHEIGHT)
    pdc.DeleteDC()
    return (phys_w / dpi_x * 25.4, phys_h / dpi_y * 25.4)


def get_driver_name(printer_name: str) -> str:
    try:
        handle = win32print.OpenPrinter(printer_name)
        try:
            info = win32print.GetPrinter(handle, 2)
            return (info.get("pDriverName") or "").lower()
        finally:
            win32print.ClosePrinter(handle)
    except Exception:
        return ""


def is_probable_label_printer(printer_name: str) -> bool:
    text = f"{printer_name} {get_driver_name(printer_name)}".lower()
    if any(k in text for k in LABEL_PRINTER_KEYWORDS):
        return True
    try:
        w_mm, h_mm = get_physical_size_mm(printer_name)
        is_standard = any(
            abs(w_mm - sw) < 5 and abs(h_mm - sh) < 5 for sw, sh in _STANDARD_SIZES_MM
        )
        if not is_standard and max(w_mm, h_mm) < 150:
            return True
    except Exception:
        pass
    return False


def list_label_printers() -> list[str]:
    return [p for p in list_printers() if is_probable_label_printer(p)]


def get_printer_geometry(printer_name: str) -> dict:
    """Returns printable size in mm and pixels, plus the printer's DPI."""
    pdc = win32ui.CreateDC()
    pdc.CreatePrinterDC(printer_name)
    dpi_x = pdc.GetDeviceCaps(win32con.LOGPIXELSX)
    dpi_y = pdc.GetDeviceCaps(win32con.LOGPIXELSY)
    printable_w_px = pdc.GetDeviceCaps(win32con.HORZRES)
    printable_h_px = pdc.GetDeviceCaps(win32con.VERTRES)
    pdc.DeleteDC()
    return {
        "dpi": (dpi_x, dpi_y),
        "printable_px": (printable_w_px, printable_h_px),
        "printable_mm": (printable_w_px / dpi_x * 25.4, printable_h_px / dpi_y * 25.4),
    }


def render_pdf_top_left(pdf_path: str, width_mm: float, height_mm: float, dpi_x: int, dpi_y: int) -> Image.Image:
    """Rasterize the top-left width_mm x height_mm region of a PDF's
    first page at the printer's native DPI."""
    target_w = max(1, round(width_mm / 25.4 * dpi_x))
    target_h = max(1, round(height_mm / 25.4 * dpi_y))

    pdf = pdfium.PdfDocument(pdf_path)
    try:
        page = pdf[0]
        bitmap = page.render(scale=dpi_x / 72.0)
        full = bitmap.to_pil().convert("RGB")
    finally:
        pdf.close()

    crop_w = min(target_w, full.width)
    crop_h = min(round(height_mm / 25.4 * dpi_x), full.height)
    img = full.crop((0, 0, crop_w, crop_h))

    if img.size != (target_w, target_h):
        img = img.resize((target_w, target_h), Image.LANCZOS)
    return img


def print_image_silent(img: Image.Image, printer_name: str, copies: int):
    """Send an already-sized image straight to the printer via GDI.
    No dialogs, no external apps, no extra windows."""
    pdc = win32ui.CreateDC()
    pdc.CreatePrinterDC(printer_name)
    dib = ImageWin.Dib(img)

    pdc.StartDoc("Label print")
    for _ in range(copies):
        pdc.StartPage()
        dib.draw(pdc.GetHandleOutput(), (0, 0, img.width, img.height))
        pdc.EndPage()
    pdc.EndDoc()
    pdc.DeleteDC()


class LabelPrinterApp(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.pdf_path = tk.StringVar()
        self.printer = tk.StringVar()
        self.copies = tk.IntVar(value=1)
        self.detected_size = tk.StringVar(value="Not detected yet")
        self._build_ui()
        self.refresh_printer_list()

    def _build_ui(self):
        ttk.Label(self, text="Printer:").pack(anchor="w", padx=10, pady=(10, 0))
        self.printer_combo = ttk.Combobox(self, textvariable=self.printer, width=50)
        self.printer_combo.pack(padx=10, fill="x")
        self.printer_combo.bind("<<ComboboxSelected>>", lambda e: self.detect_area())

        self.show_all = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            self, text="Show all printers (not just detected label printers)",
            variable=self.show_all, command=self.refresh_printer_list,
        ).pack(anchor="w", padx=10, pady=(2, 0))

        ttk.Label(self, text="PDF file:").pack(anchor="w", padx=10, pady=(10, 0))
        file_frame = ttk.Frame(self)
        file_frame.pack(fill="x", padx=10)
        ttk.Entry(file_frame, textvariable=self.pdf_path).pack(side="left", fill="x", expand=True)
        ttk.Button(file_frame, text="Browse...", command=self.browse_pdf).pack(side="left", padx=(5, 0))

        ttk.Label(self, text="Copies:").pack(anchor="w", padx=10, pady=(10, 0))
        ttk.Spinbox(self, from_=1, to=999, textvariable=self.copies, width=6).pack(anchor="w", padx=10)

        ttk.Label(self, textvariable=self.detected_size, foreground="blue").pack(padx=10, pady=(15, 0), anchor="w")

        ttk.Button(self, text="Print", command=self.crop_and_print).pack(pady=15)

    def refresh_printer_list(self):
        printers = list_printers() if self.show_all.get() else list_label_printers()
        self.printer_combo["values"] = printers
        if printers and self.printer.get() not in printers:
            self.printer.set(printers[0])
            self.detect_area()
        elif not printers:
            self.printer.set("")
            self.detected_size.set("No label printers detected - try 'Show all printers'")

    def browse_pdf(self):
        path = filedialog.askopenfilename(filetypes=[("PDF files", "*.pdf")])
        if path:
            self.pdf_path.set(path)

    def detect_area(self):
        if not self.printer.get():
            return
        try:
            geom = get_printer_geometry(self.printer.get())
            w, h = geom["printable_mm"]
            self.detected_size.set(f"Printable area: {w:.1f} mm x {h:.1f} mm")
        except Exception as e:
            self.detected_size.set(f"Could not detect area: {e}")

    def crop_and_print(self):
        if not self.printer.get() or not self.pdf_path.get():
            messagebox.showwarning("Missing info", "Please select a printer and a PDF file.")
            return
        try:
            geom = get_printer_geometry(self.printer.get())
            w_mm, h_mm = geom["printable_mm"]
            dpi_x, dpi_y = geom["dpi"]

            img = render_pdf_top_left(self.pdf_path.get(), w_mm, h_mm, dpi_x, dpi_y)
            print_image_silent(img, self.printer.get(), self.copies.get())

            messagebox.showinfo("Done", f"Sent {self.copies.get()} copy(ies) to {self.printer.get()}.")
        except Exception as e:
            messagebox.showerror("Error", str(e))


def create_label_printer_ui(parent):
    ui = LabelPrinterApp(parent)
    ui.pack(fill=tk.BOTH, expand=True)
    return ui


if __name__ == "__main__":
    root = tk.Tk()
    root.title("Label PDF Cropper & Printer")
    root.geometry("480x300")
    create_label_printer_ui(root)
    root.mainloop()
