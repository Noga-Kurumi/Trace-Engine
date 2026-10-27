"""Contexto en forma de timeline persistido en SQLite local (Fase A).

Guarda eventos pasivos del entorno del usuario (cambios de ventana, OCR de
pantalla, portapapeles; audio_in/audio_out reservados para fases futuras) en
una base SQLite con índice full-text FTS5 para recuperación por keywords.

Decisiones de diseño:
- Archivo: data/timeline.db (raíz del repo; la carpeta data/ está gitignored).
- Concurrencia: UNA conexión compartida con check_same_thread=False +
  threading.Lock (los recolectores escriben desde sus hilos y las consultas
  pueden venir del hilo del LLM). SQLite serializa escrituras de todas formas;
  WAL + busy_timeout permiten lecturas mientras se escribe.
- FTS5 "external content" (content=timeline): la tabla virtual no duplica el
  texto; los triggers la mantienen sincronizada en INSERT/UPDATE/DELETE.
- Base circular: cleanup_old_records() borra por antigüedad (lo llama el
  coordinator al arranque y cada hora); los triggers limpian la FTS sola.

API:
    db = TimelineDB()                       # data/timeline.db por defecto
    db.insert("ocr", "Chrome", "Título", "texto...")
    db.cleanup_old_records(retention_hours=72)
    rows = db.search_by_keywords("factura luz", limit=20)   # timestamp DESC
    rows = db.get_by_time_range(t0, t1, limit=200)          # timestamp ASC
    texto = db.format_results(rows, max_chars=200)          # compacto p/ prompt
"""

import logging
import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from typing import List, Optional, Tuple

from platformdirs import user_data_dir

logger = logging.getLogger(__name__)

APP_DATA_DIR = user_data_dir("TRACE", "TRACE")
DEFAULT_DB_PATH = os.path.join(APP_DATA_DIR, "timeline.db")

SOURCES = ("app_change", "ocr", "audio_in", "audio_out", "clipboard")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS timeline (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('app_change','ocr','audio_in','audio_out','clipboard')),
    app_name TEXT,
    window_title TEXT,
    content TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_timeline_timestamp ON timeline(timestamp);
CREATE INDEX IF NOT EXISTS idx_timeline_source_time ON timeline(source, timestamp, id);

CREATE TABLE IF NOT EXISTS timeline_observations (
    event_id INTEGER PRIMARY KEY,
    processed_at REAL NOT NULL,
    window_id TEXT NOT NULL,
    monitor_json TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS timeline_observation_delete AFTER DELETE ON timeline BEGIN
    DELETE FROM timeline_observations WHERE event_id=old.id;
END;

CREATE TABLE IF NOT EXISTS window_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    ended_at REAL,
    app_name TEXT NOT NULL,
    window_title TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_window_sessions_time
    ON window_sessions(started_at, ended_at);
CREATE INDEX IF NOT EXISTS idx_window_sessions_lookup
    ON window_sessions(app_name, window_title, started_at);

CREATE TABLE IF NOT EXISTS meeting_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    ended_at REAL,
    app_name TEXT NOT NULL,
    channel_name TEXT,
    participants TEXT
);
CREATE INDEX IF NOT EXISTS idx_meeting_sessions_time
    ON meeting_sessions(started_at, ended_at);
CREATE INDEX IF NOT EXISTS idx_meeting_sessions_lookup
    ON meeting_sessions(app_name, started_at);

CREATE VIRTUAL TABLE IF NOT EXISTS timeline_fts USING fts5(
    content, app_name, window_title,
    content='timeline', content_rowid='id'
);

-- Mantenimiento de la FTS (esquema estándar FTS5 external content).
CREATE TRIGGER IF NOT EXISTS timeline_ai AFTER INSERT ON timeline BEGIN
    INSERT INTO timeline_fts(rowid, content, app_name, window_title)
    VALUES (new.id, new.content, new.app_name, new.window_title);
END;
CREATE TRIGGER IF NOT EXISTS timeline_ad AFTER DELETE ON timeline BEGIN
    INSERT INTO timeline_fts(timeline_fts, rowid, content, app_name, window_title)
    VALUES ('delete', old.id, old.content, old.app_name, old.window_title);
END;
CREATE TRIGGER IF NOT EXISTS timeline_au AFTER UPDATE ON timeline BEGIN
    INSERT INTO timeline_fts(timeline_fts, rowid, content, app_name, window_title)
    VALUES ('delete', old.id, old.content, old.app_name, old.window_title);
    INSERT INTO timeline_fts(rowid, content, app_name, window_title)
    VALUES (new.id, new.content, new.app_name, new.window_title);
END;
"""

# Fila devuelta por las consultas: (id, timestamp, source, app_name, window_title, content)
Row = Tuple[int, float, str, Optional[str], Optional[str], str]

_SELECT_COLS = "id, timestamp, source, app_name, window_title, content"


class TimelineDB:
    """Acceso thread-safe a la base del timeline (conexión única + lock)."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        with self._lock:
            from trace_engine.work_context import SCHEMA as WORK_SCHEMA
            from trace_engine.meeting_context import SCHEMA as MEETING_SCHEMA
            had_work_index=self._conn.execute("SELECT 1 FROM sqlite_master WHERE name='work_operations_fts'").fetchone()
            self._conn.executescript(_SCHEMA + WORK_SCHEMA + MEETING_SCHEMA)
            if not had_work_index:
                self._conn.execute("INSERT INTO work_operations_fts(work_operations_fts) VALUES('rebuild')")
            self._conn.commit()
        logger.info("✅ [TIMELINE] Base de datos lista en %s", self.db_path)

    # ------------------------------------------------------------- escritura

    def insert(self, source: str, app_name: Optional[str], window_title: Optional[str],
               content: Optional[str], timestamp: Optional[float] = None, *, observation=None) -> bool:
        """Inserta un evento. Ignora content vacío/None (devuelve False)."""
        if not content or not str(content).strip():
            return False
        if source not in SOURCES:
            raise ValueError(f"source inválido: {source!r} (válidos: {SOURCES})")
        ts = float(timestamp) if timestamp is not None else time.time()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO timeline (timestamp, source, app_name, window_title, content)"
                " VALUES (?, ?, ?, ?, ?)",
                (ts, source, app_name or "", window_title or "", str(content).strip()))
            if observation is not None:
                self._conn.execute('INSERT INTO timeline_observations VALUES (?,?,?,?)',
                    (cursor.lastrowid, float(observation['processed_at']), str(observation['window_id']),
                     json.dumps(observation['monitor'], separators=(',',':'))))
            self._conn.commit()
        return True

    def cleanup_old_records(self, retention_hours: float) -> int:
        """Borra registros más viejos que retention_hours (base circular).

        Los triggers AFTER DELETE limpian la FTS automáticamente.
        Devuelve cuántos registros se borraron.
        """
        cutoff = time.time() - float(retention_hours) * 3600
        with self._lock:
            cur = self._conn.execute("DELETE FROM timeline WHERE timestamp < ?", (cutoff,))
            self._conn.execute(
                "DELETE FROM window_sessions WHERE COALESCE(ended_at, started_at) < ?",
                (cutoff,))
            self._conn.execute(
                "DELETE FROM meeting_sessions WHERE COALESCE(ended_at, started_at) < ?",
                (cutoff,))
            self._conn.execute('DELETE FROM work_tasks WHERE updated < ?', (cutoff,))
            self._conn.execute('DELETE FROM work_documents WHERE NOT EXISTS '
                '(SELECT 1 FROM work_operations WHERE document_ref=work_documents.ref)')
            self._conn.commit()
            deleted = cur.rowcount
        if deleted:
            logger.info("🧹 [TIMELINE] Limpieza: %d registros con más de %.0fh borrados",
                        deleted, retention_hours)
        return deleted

    def clear_all_records(self):
        """Explicit audit deletion also removes task/document associations."""
        with self._lock,self._conn:
            for table in ('timeline','window_sessions','meeting_sessions','work_tasks','work_documents'):
                self._conn.execute('DELETE FROM '+table)

    def start_window_session(self, app_name: str, window_title: str,
                             started_at: Optional[float] = None) -> int:
        ts = float(started_at) if started_at is not None else time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO window_sessions "
                "(started_at, app_name, window_title) VALUES (?, ?, ?)",
                (ts, app_name or "", window_title or ""))
            self._conn.commit()
            return int(cur.lastrowid)

    def end_window_session(self, session_id: int,
                           ended_at: Optional[float] = None) -> None:
        ts = float(ended_at) if ended_at is not None else time.time()
        with self._lock:
            self._conn.execute("UPDATE window_sessions SET ended_at=? WHERE id=?",
                               (ts, int(session_id)))
            self._conn.commit()

    def get_window_sessions(self, start_epoch: float, end_epoch: float,
                            limit: int = 200) -> List[tuple]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT id, started_at, ended_at, app_name, window_title "
                "FROM window_sessions "
                "WHERE started_at <= ? AND COALESCE(ended_at, ?) >= ? "
                "ORDER BY started_at ASC LIMIT ?",
                (float(end_epoch), float(end_epoch), float(start_epoch), int(limit)))
            return cur.fetchall()

    def record_meeting_segment(self, segment, text):
        from trace_engine.meeting_context import insert_segment
        return insert_segment(self,segment,text)

    def update_meeting_state(self,session_id,**values):
        from trace_engine.meeting_context import state
        return state(self,session_id,**values)

    def start_meeting_session(self, app_name: str, channel_name: str = "",
                              participants: str = "",
                              started_at: Optional[float] = None) -> int:
        ts = float(started_at) if started_at is not None else time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO meeting_sessions "
                "(started_at, app_name, channel_name, participants) VALUES (?, ?, ?, ?)",
                (ts, app_name or "", channel_name or "", participants or ""))
            self._conn.commit()
            return int(cur.lastrowid)

    def end_meeting_session(self, session_id: int,
                            ended_at: Optional[float] = None) -> None:
        ts = float(ended_at) if ended_at is not None else time.time()
        with self._lock:
            self._conn.execute("UPDATE meeting_sessions SET ended_at=? WHERE id=?",
                               (ts, int(session_id)))
            self._conn.commit()

    def get_meeting_sessions(self, start_epoch: float, end_epoch: float,
                             limit: int = 50) -> List[tuple]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT id, started_at, ended_at, app_name, channel_name, participants "
                "FROM meeting_sessions "
                "WHERE started_at <= ? AND COALESCE(ended_at, ?) >= ? "
                "ORDER BY started_at ASC LIMIT ?",
                (float(end_epoch), float(end_epoch), float(start_epoch), int(limit)))
            return cur.fetchall()

    # -------------------------------------------------------------- consulta

    def search_by_keywords(self, query: str, limit: int = 20) -> List[Row]:
        """Busca por keywords en la FTS (content, app_name, window_title).

        Cada término se entrecomilla (MATCH seguro, sin inyección de sintaxis
        FTS5) y se unen con OR. Devuelve filas ordenadas por timestamp DESC.
        """
        terms = [t.replace('"', "").strip() for t in (query or "").split()]
        terms = [t for t in terms if t]
        if not terms:
            return []
        match = " OR ".join(f'"{t}"' for t in terms)
        with self._lock:
            cur = self._conn.execute(
                f"SELECT {_SELECT_COLS} FROM timeline"
                " WHERE id IN (SELECT rowid FROM timeline_fts WHERE timeline_fts MATCH ?)"
                " ORDER BY timestamp DESC LIMIT ?",
                (match, int(limit)))
            return cur.fetchall()

    def get_by_time_range(self, start_epoch: float, end_epoch: float,
                          limit: int = 200) -> List[Row]:
        """Filas con timestamp en [start_epoch, end_epoch], orden ASC."""
        with self._lock:
            cur = self._conn.execute(
                f"SELECT {_SELECT_COLS} FROM timeline"
                " WHERE timestamp BETWEEN ? AND ?"
                " ORDER BY timestamp ASC LIMIT ?",
                (float(start_epoch), float(end_epoch), int(limit)))
            return cur.fetchall()

    def get_latest_screen_record(self, start_epoch: float, end_epoch: float) -> Optional[Row]:
        """Return the newest OCR record in the interval, with deterministic ties."""
        with self._lock:
            cursor = self._conn.execute(
                f"SELECT {_SELECT_COLS} FROM timeline"
                " WHERE timestamp BETWEEN ? AND ? AND source = ?"
                " AND length(trim(content)) > 0"
                " ORDER BY timestamp DESC, id DESC LIMIT 1",
                (float(start_epoch), float(end_epoch), "ocr"),
            )
            return cursor.fetchone()

    # --------------------------------------------------------------- formato

    @staticmethod
    def format_results(rows: List[Row], max_chars: int = 200) -> str:
        """Formato compacto (una línea por registro) para minimizar tokens:

            [14:32] ocr/Chrome - Título ventana: contenido truncado…
        """
        lines = []
        for _id, ts, source, app_name, window_title, content in rows:
            hora = datetime.fromtimestamp(ts).strftime("%H:%M")
            texto = " ".join(str(content).split())  # colapsar saltos de línea
            if len(texto) > max_chars:
                texto = texto[:max_chars] + "…"
            origen = source
            if app_name:
                origen += f"/{app_name}"
            cabecera = f"[{hora}] {origen}"
            if window_title:
                cabecera += f" - {window_title}"
            lines.append(f"{cabecera}: {texto}")
        return "\n".join(lines)

    # ---------------------------------------------------------------- cierre

    def close(self) -> None:
        """Cierra la conexión (al salir de la app)."""
        with self._lock:
            self._conn.close()
