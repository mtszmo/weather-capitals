# weather-capitals

Recolecta el clima **actual** de las 193 capitales de los Estados miembro de la ONU
usando la [API de Open-Meteo](https://open-meteo.com/) y lo guarda en una base de
datos **SQLite** (`weather.db`). Pensado para correr **cada hora en GitHub Actions**,
de forma gratuita y sin necesidad de un servidor propio.

## Cómo funciona

1. `capitals.json` contiene la lista de capitales (país, capital, latitud, longitud).
2. `fetch_weather.py`:
   - Crea (si no existen) las tablas `capitals` y `weather_readings` en `weather.db`.
   - Divide las 193 capitales en bloques de 50 (configurable) y le pide a Open-Meteo
     el clima actual de cada bloque en **una sola llamada HTTP** (Open-Meteo soporta
     hasta 1000 coordenadas por request separadas por comas).
   - Guarda cada lectura evitando duplicados (si el workflow corriera dos veces para
     la misma hora, no se insertan filas repetidas).
3. El workflow `.github/workflows/hourly-weather.yml` ejecuta el script cada hora y
   commitea `weather.db` de vuelta al repositorio si hubo cambios.

Con 193 capitales y 15 variables por capital, cada corrida hace solo **4 llamadas**
a la API (193 ÷ 50 ≈ 4 bloques) y 24 corridas/día = **~96 llamadas/día**, muy por
debajo del límite gratuito no-comercial de Open-Meteo (10.000 llamadas/día).

## Uso local

```bash
pip install -r requirements.txt
python fetch_weather.py
```

Esto crea/actualiza `weather.db` en la misma carpeta. Variables de entorno opcionales:

| Variable        | Default                  | Descripción                                   |
|-----------------|---------------------------|------------------------------------------------|
| `DB_PATH`       | `weather.db`               | Ruta al archivo SQLite                         |
| `CAPITALS_PATH` | `capitals.json`             | Ruta al JSON de capitales                      |
| `CHUNK_SIZE`    | `50`                        | Capitales por request a Open-Meteo             |

## Esquema de la base de datos

```
capitals
├── id          INTEGER PRIMARY KEY
├── country     TEXT
├── capital     TEXT
├── latitude    REAL
└── longitude   REAL

weather_readings
├── id                    INTEGER PRIMARY KEY
├── capital_id            → capitals.id
├── fetched_at            TEXT   (UTC, cuándo corrió el script)
├── observation_time      TEXT   (UTC, momento que reporta Open-Meteo)
├── timezone
├── temperature_2m        REAL   (°C)
├── relative_humidity_2m  REAL   (%)
├── apparent_temperature  REAL   (°C, sensación térmica)
├── is_day                INTEGER (1/0)
├── precipitation         REAL   (mm)
├── rain                  REAL   (mm)
├── showers               REAL   (mm)
├── snowfall              REAL   (cm)
├── weather_code          INTEGER (código WMO, ver docs de Open-Meteo)
├── cloud_cover            REAL   (%)
├── pressure_msl          REAL   (hPa)
├── surface_pressure      REAL   (hPa)
├── wind_speed_10m        REAL   (km/h)
├── wind_direction_10m    REAL   (°)
└── wind_gusts_10m        REAL   (km/h)
```

## Sobre la lista de capitales

Se usó como criterio los **193 Estados miembro de la ONU**, para tener una lista
objetiva y sin ambigüedad. Casos con más de una "capital" (p. ej. Bolivia, Sudáfrica,
Sri Lanka, Costa de Marfil) se resolvieron eligiendo la sede efectiva de gobierno más
usada en fuentes de referencia. `capitals.json` es un archivo plano fácil de editar:
podés agregar o quitar entradas (Taiwán, Kosovo, Ciudad del Vaticano, Palestina,
territorios, etc.) según lo que se necesite — solo se debe agregar un objeto
`{"country": "...", "capital": "...", "lat": ..., "lon": ...}`.

