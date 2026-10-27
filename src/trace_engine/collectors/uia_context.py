"""Contexto de pantalla vía UI Automation (UIA) para providers sin visión.

Alternativa barata al OCR: recorre el árbol UIA de la ventana en foco
(foreground window) y recolecta elementos útiles (botones, edits, items de
lista, etc.) como líneas "Tipo: nombre", con pruning para no inundar el
prompt del LLM local.

No usa el wrapper `uiautomation`: va directo con comtypes contra el COM
UIAutomationClient (UIAutomationCore.dll). El módulo de tipos se genera con
comtypes.client.GetModule en el primer uso (lazy) — si la generación falla,
todo devuelve '' sin romper el turno.

Hilos: UIA exige COM inicializado en el hilo llamante. `get_uia_text()`
hace CoInitialize/CoUninitialize internamente, así que es segura desde un
QRunnable / worker thread cualquiera.

Presupuesto: el walk corta por max_items, max_chars o un límite de tiempo
(~350ms) para no demorar el turno.
"""

import logging
import time

from trace_engine.collectors.screen_ocr import ocr_image, screen_capture_scope, is_excluded_window

logger = logging.getLogger(__name__)


_TIME_BUDGET_S = 0.35
_NAME_MAX_CHARS = 120

# ControlTypeIds -> etiqueta corta legible para el prompt.
_CONTROL_TYPES = {
    50020: "Texto",
    50004: "Campo",
    50030: "Documento",
    50000: "Botón",
    50005: "Enlace",
    50011: "MenúItem",
    50019: "PestañaItem",
    50007: "ListaItem",
    50024: "ÁrbolItem",
    50003: "Combo",
    50002: "Check",
    50013: "Radio",
    50035: "Encabezado",
    50029: "Dato",
    50037: "BarraTítulo",
    50038: "BarraEstado",
    50039: "Tooltip",
    50036: "Grupo",
    50010: "BarraMenú",
    50018: "Pestaña",
}

# Cache del módulo de tipos generado (lazy; None = no intentado, False = falló).
_uia_module = None


def _get_uia():
    """Instancia IUIAutomation (None si COM/UIA no está disponible)."""
    global _uia_module
    if _uia_module is False:
        return None
    try:
        import comtypes.client

        if _uia_module is None:
            _uia_module = comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA

        return comtypes.client.CreateObject(
            UIA.CUIAutomation, interface=UIA.IUIAutomation)
    except Exception as e:
        logger.warning("⚠️ [UIA] No se pudo inicializar UI Automation: %s", e)
        _uia_module = False
        return None


def get_uia_text(max_chars: int = 2000, max_items: int = 120) -> str:
    """Texto estructurado del árbol UIA de la ventana en foco ('' si falla).

    Una línea por elemento útil: "Tipo: nombre". Dedupe de nombres
    preservando orden, nombres capados a ~120 chars, walk limitado por
    max_items / max_chars / presupuesto de ~350ms. Defensivo: cualquier
    COMError (la ventana cambia o cierra) devuelve lo acumulado o ''.
    """
    import comtypes
    import win32gui

    comtypes.CoInitialize()
    try:
        uia = _get_uia()
        if uia is None:
            return ""
        try:
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd or is_excluded_window(hwnd):
                return ""
            root = uia.ElementFromHandle(hwnd)
        except Exception as e:
            logger.debug("[UIA] Sin raíz para la ventana en foco: %s", e)
            return ""

        deadline = time.monotonic() + _TIME_BUDGET_S
        lines: list[str] = []
        seen: set[str] = set()
        total = 0

        def add_element(el) -> bool:
            """Procesa un elemento; False = hay que cortar el walk."""
            nonlocal total
            try:
                if el.CurrentIsOffscreen:
                    return True
                ctype = el.CurrentControlType
                label = _CONTROL_TYPES.get(ctype)
                if label is None:
                    return True
                name = " ".join((el.CurrentName or "").split())
            except Exception:
                return True  # elemento desapareció; seguir con el resto
            if not name or name in seen:
                return True
            seen.add(name)
            if len(name) > _NAME_MAX_CHARS:
                name = name[: _NAME_MAX_CHARS - 1] + "…"
            line = f"{label}: {name}"
            lines.append(line)
            total += len(line) + 1
            return len(lines) < max_items and total < max_chars

        _walk_tree(root, uia, add_element, deadline)
        text = "\n".join(lines)
        return text[:max_chars]
    except Exception as e:
        logger.debug("[UIA] Error general: %s", e)
        return ""
    finally:
        comtypes.CoUninitialize()


def _active_window_info() -> str:
    """'app.exe — título' de la ventana en foco ('' si falla)."""
    try:
        import win32gui
        import win32process

        hwnd = win32gui.GetForegroundWindow()
        if not hwnd or is_excluded_window(hwnd):
            return ""
        title = win32gui.GetWindowText(hwnd).strip()
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if not pid or pid <= 0:
            return ""
        import psutil
        app = psutil.Process(pid).name()
        return f"{app} — {title}" if title else app
    except Exception as e:
        logger.debug("[UIA] No se pudo obtener ventana activa: %s", e)
        return ""


def _ocr_screen_text(max_chars: int = 800) -> str:
    """OCR WinRT del monitor principal a resolución nativa ('' si falla).

    Optimizado: sanitiza espacios múltiples y acota a max_chars (800) para no
    inflar el prefill del LLM.
    """
    from PIL import Image
    try:
        from mss import mss
        with mss() as sct:
            real = sct.monitors[1:]
            monitor = next(
                (m for m in real if m["left"] == 0 and m["top"] == 0), real[0])
            with screen_capture_scope():
                sct_img = sct.grab(monitor)
            img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
        raw_text = ocr_image(img)
        # Sanitizar: remover líneas/espacios repetidos
        clean_text = " ".join(raw_text.split())
        return clean_text[:max_chars]
    except Exception as e:
        logger.debug("[SCREEN] Fallo captura de pantalla para OCR: %s", e)
        return ""


def get_screen_context_text() -> str:
    """Contexto de pantalla compacto para la tool get_screen_context.

    Formato: "Ventana en foco: app — título" + contenido. UIA primero
    (estructura de la interfaz, barata); si viene pobre (<80 chars — apps
    Electron/Chromium exponen poco), fallback a OCR WinRT. Devuelve '' si no
    se pudo obtener nada (la tool responde un mensaje explícito en ese caso).
    """
    window_info = _active_window_info()
    uia_text = get_uia_text()
    if len(uia_text) >= 80:
        logger.info("👁️ [SCREEN] Contexto de pantalla vía UIA (%d chars)",
                    len(uia_text))
        contenido = ("Estructura de la interfaz (UIA, 'Tipo: nombre'):\n"
                     + uia_text)
    else:
        logger.info("👁️ [SCREEN] UIA pobre (%d chars); fallback a OCR",
                    len(uia_text))
        try:
            ocr_text = _ocr_screen_text()
        except Exception as e:
            logger.error("❌ [SCREEN] Error en OCR de pantalla: %s", e,
                         exc_info=True)
            ocr_text = ""
        contenido = ("Texto en pantalla (OCR plano, sin orden visual):\n"
                     + ocr_text) if ocr_text else ""

    # Historical OCR is explicitly dated and never attributed to the live window.
    if not contenido:
        db = None
        try:
            from datetime import datetime
            from trace_engine.storage.timeline_db import TimelineDB

            db = TimelineDB()
            now = time.time()
            row = db.get_latest_screen_record(now - 300, now)
            if row is None:
                row = db.get_latest_screen_record(now - 3600, now)
            if row is not None:
                _, timestamp, _, app, title, content = row
                recorded_at = datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")
                source_window = f"{app} — {title}" if title else (app or "ventana")
                contenido = (
                    f"Registro histórico OCR ({recorded_at}), ventana: {source_window}. "
                    "No confirma el contenido actual de pantalla:\n"
                    + content[:500]
                )
        except Exception as exc:
            logger.debug("Historical screen fallback failed: %s", exc)
        finally:
            if db is not None:
                db.close()

    if not window_info and not contenido:
        return ""
    out = ""
    if window_info:
        out += "Ventana en foco: " + window_info
    if contenido:
        out += "\n" + contenido
    return out.strip()


def _walk_tree(root, uia, add_element, deadline) -> None:
    """DFS iterativo con el walker de ControlView (salta IsOffscreen)."""
    try:
        walker = uia.ControlViewWalker
        # Stack de (elemento, depth); se apilan hijos en orden inverso.
        stack = [(root, 0)]
        while stack:
            if time.monotonic() > deadline:
                break
            el, depth = stack.pop()
            if not add_element(el):
                break
            if depth >= 12:
                continue
            children = []
            try:
                child = walker.GetFirstChildElement(el)
                while child is not None:
                    children.append(child)
                    child = walker.GetNextSiblingElement(child)
            except Exception:
                pass
            for child in reversed(children):
                stack.append((child, depth + 1))
    except Exception as e:
        logger.debug("[UIA] Walk interrumpido: %s", e)
