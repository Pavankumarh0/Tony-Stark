"""Farm sensor readings from the Arduino UNO Q sensor device.

The UNO Q's App Lab app (unoq/smart-krishi-sensors) POSTs one reading every
5 s to /api/krishi/sensors. The latest reading per device is kept in memory
and in data/sensors_latest.json (written atomically), so a restart keeps it.

Nothing here invents a value: a measurement the device could not take is
null, the soil percentage stays null until the device is calibrated, and the
MQ-135 value is only ever a raw ADC number (never ppm or AQI).

Status, from the age of the newest reading (server clock):
    online   younger than SENSOR_STALE_S   (default 30 s = 6 missed samples)
    stale    younger than SENSOR_OFFLINE_S (default 120 s)
    offline  older than that
    no_data  nothing received yet
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

import agro
from campus_time import IST

_lock = threading.Lock()
_latest: dict[str, dict] = {}
_loaded = False

# Questions that should see the live readings.
SENSOR_WORDS = re.compile(
    r"\b(soil|moisture|water(?:ing)?|irrigat\w*|dry|wet|sensors?|readings?|humidity|humid|temperature|hot|"
    r"cold|weather|air|gas|mq|farm|field|conditions?|climate)\b", re.I)


# Questions about the farm's *current* state: with no reading at all, these get
# "no sensor data yet" instead of general advice that sounds like an assessment.
LIVE_INTENT = re.compile(
    r"\b(today|right now|now|currently|current|at the moment|sensors?|readings?|"
    r"how(?:'s| is) (?:my|the) (?:farm|field|soil|crop|plants?)|conditions?|status)\b", re.I)


def _store() -> Path:
    return Path(os.getenv("SENSOR_STORE", Path(__file__).resolve().parent / "data" / "sensors_latest.json"))


def _load():
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        data = json.loads(_store().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _latest.update({k: v for k, v in data.items() if isinstance(v, dict) and "received_at" in v})
    except (OSError, ValueError):
        pass


def _persist():
    path = _store()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(_latest, indent=2), encoding="utf-8")
        os.replace(tmp, path)                      # never a half-written file
    except OSError:
        pass


def reset():
    """Tests only."""
    global _loaded
    with _lock:
        _latest.clear()
        _loaded = True


def save(reading: dict, now: datetime | None = None) -> dict:
    rec = {**reading, "received_at": (now or datetime.now(timezone.utc)).isoformat()}
    with _lock:
        _load()
        _latest[rec["device_id"]] = rec
        _persist()
    return rec


def latest(device_id: str | None = None) -> dict | None:
    with _lock:
        _load()
        if device_id:
            return _latest.get(device_id)
        rows = list(_latest.values())
    return max(rows, key=lambda r: r["received_at"]) if rows else None


def age_seconds(rec: dict, now: datetime | None = None) -> float:
    at = datetime.fromisoformat(rec["received_at"])
    return max(0.0, ((now or datetime.now(timezone.utc)) - at).total_seconds())


def status(rec: dict | None, now: datetime | None = None) -> str:
    if rec is None:
        return "no_data"
    age = age_seconds(rec, now)
    if age < float(os.getenv("SENSOR_STALE_S", "30")):
        return "online"
    if age < float(os.getenv("SENSOR_OFFLINE_S", "120")):
        return "stale"
    return "offline"


def validity(rec: dict) -> dict:
    """Which measurements can be used as real values."""
    return {
        "temperature": rec.get("temperature_c") is not None,
        "humidity": rec.get("humidity_percent") is not None,
        "soil_moisture": bool(rec.get("soil_sensor_valid")) and rec.get("soil_moisture_percent") is not None,
        "soil_raw": rec.get("soil_moisture_raw") is not None,
        "mq135": rec.get("mq135_raw") is not None and rec.get("mq135_valid", True),
        "water_level": bool(rec.get("water_level_valid")) and rec.get("water_level_percent") is not None,
    }


def summary(device_id: str | None = None, now: datetime | None = None) -> dict:
    """The GET /sensors/latest body."""
    now = now or datetime.now(timezone.utc)
    rec = latest(device_id)
    out = {"server_time": now.isoformat(), "status": status(rec, now), "reading": rec}
    if rec:
        out.update(device_id=rec["device_id"], received_at=rec["received_at"],
                   age_seconds=round(age_seconds(rec, now), 1), valid=validity(rec),
                   is_test_data=bool(rec.get("is_test_data")))
    return out


def _num(v: float) -> str:
    return f"{v:g}"


def as_context(rec: dict, now: datetime | None = None) -> str:
    """The reading as plain sentences for Gemma, in its own prompt section.
    Every number here is copied from the device; the wording is what the
    grounding check compares the answer against."""
    now = now or datetime.now(timezone.utc)
    at = datetime.fromisoformat(rec["received_at"]).astimezone(IST)
    st = status(rec, now)
    age = age_seconds(rec, now)
    lines = []
    if rec.get("is_test_data"):
        lines.append("TEST DATA: this reading was sent by a test script, not measured on the farm. Say so.")
    when = f"taken at {at:%H:%M} on {at:%d %B %Y} India time, {int(age // 60)} minutes ago" if age >= 120 \
        else f"taken at {at:%H:%M} on {at:%d %B %Y} India time, {int(age)} seconds ago"
    lines.append(f"Sensor device {rec['device_id']}: reading {when}.")
    if st == "stale":
        lines.append("STALE: the sensor has not reported recently, so this reading may be out of date.")
    elif st == "offline":
        lines.append("OFFLINE: the sensor device is not reporting. This is the last known reading and it is out of date.")
    if rec.get("crop"):
        lines.append(f"Crop: {rec['crop']}.")
    t, h = rec.get("temperature_c"), rec.get("humidity_percent")
    lines.append(f"Air temperature: {_num(t)} degrees Celsius." if t is not None
                 else "Air temperature: not available, the temperature sensor gave no valid reading.")
    lines.append(f"Air humidity: {_num(h)} percent." if h is not None
                 else "Air humidity: not available, the humidity sensor gave no valid reading.")
    if rec.get("soil_sensor_valid") and rec.get("soil_moisture_percent") is not None:
        lines.append(f"Soil moisture: {_num(rec['soil_moisture_percent'])} percent (calibrated sensor reading).")
    else:
        lines.append("Soil moisture: the soil moisture sensor is not calibrated yet, so the soil water level "
                     "cannot be assessed reliably from it.")
    if rec.get("water_level_valid") and rec.get("water_level_percent") is not None:
        wl = rec["water_level_percent"]
        lines.append(f"Water level sensor: {_num(wl)} percent of the sensor strip is under water, "
                     f"which is {agro.water_band(wl)}.")
    if rec.get("mq135_raw") is not None:
        lines.append("Air quality sensor MQ-135: only an uncalibrated raw value is available, which is not a gas "
                     "level, ppm or air quality index, so air quality cannot be judged from it.")
    faults = [FAULT_TEXT.get(f, f.replace("_", " ")) for f in rec.get("faults") or [] if f]
    if faults:
        lines.append("Sensor device warnings: " + "; ".join(faults) + ".")
    return "\n".join(lines)


# "What is the temperature?" asks for a value, not advice: answered straight from
# the reading, with no model in between (instant, and nothing to get wrong).
READING_ASK = re.compile(r"\b(temperature|temp|hot|cold|humidity|humid|moisture|readings?|sensors?|water level|"
                         r"tank|water sensor|"
                         r"farm conditions?|field conditions?|how is my (?:farm|field|soil))\b", re.I)
ADVICE_ASK = re.compile(r"\b(should|need|how much|how many|when|why|what (?:to|should) do|irrigat\w*|"
                        r"fertili[sz]\w*|spray|sow|plant(?:ing)?|harvest|disease|pest|price|rate|weather|"
                        r"forecast|rain\w*)\b", re.I)


def wants_reading(question: str) -> bool:
    return bool(READING_ASK.search(question)) and not ADVICE_ASK.search(question)


def reading_answer(rec: dict, question: str = "", now: datetime | None = None) -> str:
    """The latest reading as a short spoken answer: only the values asked for
    (all of them for a general question), with when it was taken."""
    now = now or datetime.now(timezone.utc)
    at = datetime.fromisoformat(rec["received_at"]).astimezone(IST)
    st, age = status(rec, now), age_seconds(rec, now)
    q = question.lower()
    want_t = bool(re.search(r"temp|hot|cold", q))
    want_h = "humid" in q
    want_s = bool(re.search(r"moisture|soil", q))
    want_w = bool(re.search(r"water level|tank|water sensor", q))
    if not (want_t or want_h or want_s or want_w):
        want_t = want_h = want_s = want_w = True
    t, h = rec.get("temperature_c"), rec.get("humidity_percent")
    parts = []
    if want_t:
        parts.append(f"the air temperature is {_num(t)} degrees Celsius" if t is not None
                     else "the temperature sensor gave no valid reading")
    if want_h:
        parts.append(f"the air humidity is {_num(h)} percent" if h is not None
                     else "the humidity sensor gave no valid reading")
    if want_s:
        parts.append(f"the soil moisture is {_num(rec['soil_moisture_percent'])} percent"
                     if rec.get("soil_sensor_valid") and rec.get("soil_moisture_percent") is not None
                     else "the soil moisture sensor is not calibrated yet, so I cannot tell the soil water level")
    wl = rec.get("water_level_percent") if rec.get("water_level_valid") else None
    if want_w and (wl is not None or parts == []):
        parts.append(f"the water level sensor shows {_num(wl)} percent, which is {agro.water_band(wl)}" if wl is not None
                     else "the water level sensor gave no valid reading")
    said = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    if st == "online":
        lead = "Right now on your farm, " if age < 120 else f"On your farm, at {at:%I:%M %p}, "
    else:
        lead = f"The sensor is {st}, so this is the last reading, from {at:%I:%M %p} on {at:%d %B}: "
    text = lead + said + "."
    # what the reading means for the crop asked about, or the farm's own crop
    crop = agro.find_crop(question) or agro.find_crop(rec.get("crop") or "")
    if want_t and t is not None:
        text += " " + agro.temperature_advice(t, crop)
    if want_s and rec.get("soil_sensor_valid") and rec.get("soil_moisture_percent") is not None:
        text += " " + agro.moisture_advice(rec["soil_moisture_percent"], crop)
    if want_w and wl is not None:
        text += " " + agro.water_advice(wl)
    if st != "online":
        text += " Check the field yourself, because conditions may have changed since this reading."
    if rec.get("is_test_data"):
        text += " Note: this is test data, not a real farm measurement."
    return text[0].upper() + text[1:]


# How the device's fault codes are said to a farmer.
FAULT_TEXT = {
    "dht_read_failed": "the temperature and humidity sensor did not answer",
    "dht_out_of_range": "the temperature and humidity sensor gave an impossible value",
    "soil_saturated_high": "the soil probe reading is stuck at the top of its range, so the probe and its wiring should be checked",
    "soil_near_zero": "the soil probe reading is near zero, so the probe may be disconnected or shorted",
    "soil_noisy": "the soil probe readings are jumping around too much to trust",
    "water_noisy": "the water level sensor readings are jumping around too much to trust",
    "mq135_saturated_high": "the air quality sensor reading is stuck at the top of its range",
    "mq135_near_zero": "the air quality sensor reading is near zero, so it may be disconnected",
    "mq135_warming_up": "the air quality sensor is still warming up",
    "mq135_noisy": "the air quality sensor readings are jumping around too much to trust",
}
