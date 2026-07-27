#!/usr/bin/env python3
"""
Recolector de datos climáticos de las capitales del mundo usando la API de Open-Meteo.

Pensado para ejecutarse cada hora (por ejemplo, desde GitHub Actions) y acumular
el histórico de clima "actual" (current weather) de cada capital en una base de
datos SQLite (weather.db).

Uso:
    python fetch_weather.py

Variables de entorno opcionales:
    DB_PATH        Ruta al archivo SQLite (por defecto: weather.db junto al script)
    CAPITALS_PATH  Ruta al JSON de capitales (por defecto: capitals.json junto al script)
    CHUNK_SIZE     Cantidad de capitales por request a Open-Meteo (por defecto: 50)
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# --------------------------------------------------------------------------
# Configuración
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("DB_PATH", BASE_DIR / "weather.db"))
CAPITALS_PATH = Path(os.environ.get("CAPITALS_PATH", BASE_DIR / "capitals.json"))
CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", "50"))

API_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT = 30       # segundos por request HTTP
MAX_RETRIES = 3            # reintentos por bloque de capitales
RETRY_BACKOFF_SECONDS = 5  # espera base entre reintentos (se multiplica por el intento)

# Variables de "current weather" que soporta Open-Meteo (ver /en/docs, sección
# "Current Weather"). Todas se obtienen en una sola llamada por capital.
CURRENT_VARS = [
    "temperature_2m",
    "relative_humidity_2m",
    "apparent_temperature",
    "is_day",
    "precipitation",
    "rain",
    "showers",
    "snowfall",
    "weather_code",
    "cloud_cover",
    "pressure_msl",
    "surface_pressure",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS capitals (
    id INTEGER PRIMARY KEY,
    country   TEXT NOT NULL,
    capital   TEXT NOT NULL,
    latitude  REAL NOT NULL,
    longitude REAL NOT NULL,
    UNIQUE(country, capital)
);

CREATE TABLE IF NOT EXISTS weather_readings (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    capital_id        INTEGER NOT NULL REFERENCES capitals(id),
    fetched_at        TEXT NOT NULL,   -- momento (UTC) en que corrió el script
    observation_time  TEXT NOT NULL,   -- momento (UTC) que reporta Open-Meteo
    timezone          TEXT,
    temperature_2m        REAL,
    relative_humidity_2m  REAL,
    apparent_temperature  REAL,
    is_day                INTEGER,
    precipitation         REAL,
    rain                  REAL,
    showers               REAL,
    snowfall              REAL,
    weather_code          INTEGER,
    cloud_cover           REAL,
    pressure_msl          REAL,
    surface_pressure      REAL,
    wind_speed_10m        REAL,
    wind_direction_10m    REAL,
    wind_gusts_10m        REAL,
    UNIQUE(capital_id, observation_time)
);

CREATE INDEX IF NOT EXISTS idx_weather_capital_time
    ON weather_readings (capital_id, observation_time);
"""


def load_capitals() -> list[dict]:
    if not CAPITALS_PATH.exists():
        sys.exit(f"No se encontró el archivo de capitales: {CAPITALS_PATH}")
    with open(CAPITALS_PATH, encoding="utf-8") as f:
        capitals = json.load(f)
    if not capitals:
        sys.exit("El archivo de capitales está vacío.")
    return capitals


def chunked(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def upsert_capitals(conn: sqlite3.Connection, capitals: list[dict]) -> dict:
    """Inserta/actualiza las capitales y devuelve un dict {(país, capital): id}."""
    cur = conn.cursor()
    ids = {}
    for c in capitals:
        cur.execute(
            """
            INSERT INTO capitals (country, capital, latitude, longitude)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(country, capital) DO UPDATE SET
                latitude = excluded.latitude,
                longitude = excluded.longitude
            """,
            (c["country"], c["capital"], c["lat"], c["lon"]),
        )
        cur.execute(
            "SELECT id FROM capitals WHERE country = ? AND capital = ?",
            (c["country"], c["capital"]),
        )
        ids[(c["country"], c["capital"])] = cur.fetchone()[0]
    conn.commit()
    return ids


def fetch_chunk(chunk: list[dict], session: requests.Session) -> list[dict]:
    """Consulta Open-Meteo para un bloque de capitales. Devuelve siempre una lista
    de resultados en el mismo orden que 'chunk', con reintentos ante fallos de red."""
    params = {
        "latitude": ",".join(str(c["lat"]) for c in chunk),
        "longitude": ",".join(str(c["lon"]) for c in chunk),
        "current": ",".join(CURRENT_VARS),
        "timezone": "UTC",
    }

    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(API_URL, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()

            # Open-Meteo devuelve un único objeto si se pide 1 sola ubicación,
            # y una lista de objetos si se piden varias. Normalizamos a lista.
            if isinstance(data, dict):
                if data.get("error"):
                    raise RuntimeError(f"Open-Meteo devolvió un error: {data.get('reason')}")
                data = [data]

            return data
        except (requests.exceptions.RequestException, RuntimeError, ValueError) as exc:
            last_error = exc
            wait = RETRY_BACKOFF_SECONDS * attempt
            print(
                f"  ! Intento {attempt}/{MAX_RETRIES} falló: {exc}. Reintentando en {wait}s...",
                file=sys.stderr,
            )
            time.sleep(wait)

    raise RuntimeError(f"No se pudo obtener datos tras {MAX_RETRIES} intentos: {last_error}")


def store_readings(
    conn: sqlite3.Connection,
    chunk: list[dict],
    results: list[dict],
    capital_ids: dict,
    fetched_at: str,
) -> int:
    cur = conn.cursor()
    inserted = 0

    if len(results) != len(chunk):
        print(
            f"  ! Aviso: se esperaban {len(chunk)} resultados y llegaron {len(results)}. "
            "Se intentará emparejar por posición.",
            file=sys.stderr,
        )

    for capital, result in zip(chunk, results):
        current = (result or {}).get("current")
        if not current:
            print(
                f"  ! Sin datos 'current' para {capital['capital']} ({capital['country']})",
                file=sys.stderr,
            )
            continue

        capital_id = capital_ids[(capital["country"], capital["capital"])]
        try:
            cur.execute(
                """
                INSERT INTO weather_readings (
                    capital_id, fetched_at, observation_time, timezone,
                    temperature_2m, relative_humidity_2m, apparent_temperature,
                    is_day, precipitation, rain, showers, snowfall, weather_code,
                    cloud_cover, pressure_msl, surface_pressure,
                    wind_speed_10m, wind_direction_10m, wind_gusts_10m
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(capital_id, observation_time) DO NOTHING
                """,
                (
                    capital_id,
                    fetched_at,
                    current.get("time"),
                    result.get("timezone"),
                    current.get("temperature_2m"),
                    current.get("relative_humidity_2m"),
                    current.get("apparent_temperature"),
                    current.get("is_day"),
                    current.get("precipitation"),
                    current.get("rain"),
                    current.get("showers"),
                    current.get("snowfall"),
                    current.get("weather_code"),
                    current.get("cloud_cover"),
                    current.get("pressure_msl"),
                    current.get("surface_pressure"),
                    current.get("wind_speed_10m"),
                    current.get("wind_direction_10m"),
                    current.get("wind_gusts_10m"),
                ),
            )
            if cur.rowcount:
                inserted += 1
        except sqlite3.Error as exc:
            print(f"  ! Error guardando {capital['capital']}: {exc}", file=sys.stderr)

    conn.commit()
    return inserted


def main() -> None:
    capitals = load_capitals()
    print(f"Cargadas {len(capitals)} capitales desde {CAPITALS_PATH.name}")

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    capital_ids = upsert_capitals(conn, capitals)

    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    session = requests.Session()
    session.headers.update({"User-Agent": "weather-capitals-collector/1.0 (+github-actions)"})

    total_inserted = 0
    total_chunks = (len(capitals) + CHUNK_SIZE - 1) // CHUNK_SIZE

    for i, chunk in enumerate(chunked(capitals, CHUNK_SIZE), start=1):
        print(f"Consultando bloque {i}/{total_chunks} ({len(chunk)} capitales)...")
        try:
            results = fetch_chunk(chunk, session)
        except RuntimeError as exc:
            print(f"  ! Bloque {i} descartado tras varios reintentos: {exc}", file=sys.stderr)
            continue

        inserted = store_readings(conn, chunk, results, capital_ids, fetched_at)
        total_inserted += inserted
        print(f"  -> {inserted} registros nuevos guardados")

    conn.close()
    print(f"\nListo. {total_inserted} registros nuevos guardados en {DB_PATH.name} ({fetched_at})")

    if total_inserted == 0:
        print("Advertencia: no se guardó ningún registro nuevo en esta ejecución.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
