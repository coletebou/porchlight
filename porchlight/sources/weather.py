"""Weather for the header: Open-Meteo, the US National Weather Service, wttr.in.

Providers are tried in order and the first answer wins. With latitude and
longitude set the default order is open-meteo → nws → wttr; without them,
wttr.in locates you from your IP. Drop a provider from `providers` to disable
it (or set [sources.weather] enabled = false to turn weather off entirely).
"""
from __future__ import annotations

import re
import urllib.parse

from .. import core
from ..registry import source

WMO = {0: ("☀️", "clear"), 1: ("🌤️", "mostly clear"), 2: ("⛅", "partly cloudy"), 3: ("☁️", "overcast"),
       45: ("🌫️", "fog"), 48: ("🌫️", "rime fog"), 51: ("🌦️", "light drizzle"), 53: ("🌦️", "drizzle"),
       55: ("🌦️", "heavy drizzle"), 56: ("🌧️", "freezing drizzle"), 57: ("🌧️", "freezing drizzle"),
       61: ("🌧️", "light rain"), 63: ("🌧️", "rain"), 65: ("🌧️", "heavy rain"), 66: ("🌧️", "freezing rain"),
       67: ("🌧️", "freezing rain"), 71: ("🌨️", "light snow"), 73: ("🌨️", "snow"), 75: ("❄️", "heavy snow"),
       77: ("🌨️", "snow grains"), 80: ("🌦️", "showers"), 81: ("🌧️", "showers"), 82: ("⛈️", "violent showers"),
       85: ("🌨️", "snow showers"), 86: ("🌨️", "snow showers"), 95: ("⛈️", "thunderstorm"),
       96: ("⛈️", "thunderstorm, hail"), 99: ("⛈️", "thunderstorm, hail")}

_NWS_ICONS = (("thunder", "⛈️"), ("snow", "🌨️"), ("sleet", "🌨️"), ("rain", "🌧️"), ("shower", "🌦️"),
              ("drizzle", "🌦️"), ("fog", "🌫️"), ("haze", "🌫️"), ("smoke", "🌫️"), ("overcast", "☁️"),
              ("mostly cloudy", "☁️"), ("cloud", "⛅"), ("clear", "☀️"), ("sunny", "☀️"), ("fair", "☀️"),
              ("wind", "🌬️"))


def _imperial(cfg) -> bool:
    return str(cfg.get("units", "imperial")).lower() in ("imperial", "us", "f", "fahrenheit")


def open_meteo(ctx, lat, lon):
    imp = _imperial(ctx.cfg)
    q = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset",
        "temperature_unit": "fahrenheit" if imp else "celsius", "wind_speed_unit": "mph" if imp else "kmh",
        "timezone": ctx.cfg.get("timezone") or "auto", "forecast_days": 1})
    r = ctx.http("https://api.open-meteo.com/v1/forecast?" + q, timeout=5)
    if not (r.ok and isinstance(r.body, dict)):
        raise RuntimeError("open-meteo HTTP %s" % r.status)
    cur, day = r.body["current"], r.body["daily"]
    icon, desc = WMO.get(int(cur.get("weather_code", -1)), ("·", "?"))
    return {"icon": icon, "desc": desc, "temp": round(cur["temperature_2m"]),
            "feels": round(cur["apparent_temperature"]), "wind": round(cur.get("wind_speed_10m", 0)),
            "hum": cur.get("relative_humidity_2m"), "hi": round(day["temperature_2m_max"][0]),
            "lo": round(day["temperature_2m_min"][0]), "rain": day["precipitation_probability_max"][0],
            "sunset": (day.get("sunset") or [""])[0][-5:], "sunrise": (day.get("sunrise") or [""])[0][-5:]}


def nws(ctx, lat, lon):
    """US only. The latest observation from the configured or nearest station.
    NWS wants a User-Agent with contact details; set [sources.weather] user_agent."""
    ua = {"User-Agent": ctx.cfg.get("user_agent") or core.USER_AGENT, "Accept": "application/geo+json"}
    station = ctx.cfg.get("nws_station") or (ctx.prev or {}).get("nws_station")
    if not station:
        if lat is None:
            raise RuntimeError("nws needs latitude/longitude or nws_station")
        r = ctx.http("https://api.weather.gov/points/%.4f,%.4f" % (float(lat), float(lon)), headers=ua, timeout=5)
        stations_url = ((r.body or {}).get("properties") or {}).get("observationStations") if isinstance(r.body, dict) else None
        if not stations_url:
            raise RuntimeError("nws points HTTP %s" % r.status)
        r = ctx.http(stations_url, headers=ua, timeout=5)
        feats = (r.body or {}).get("features") if isinstance(r.body, dict) else None
        if not feats:
            raise RuntimeError("nws stations HTTP %s" % r.status)
        station = ((feats[0].get("properties") or {}).get("stationIdentifier"))
    r = ctx.http("https://api.weather.gov/stations/%s/observations/latest" % urllib.parse.quote(str(station)),
                 headers=ua, timeout=5)
    p = (r.body or {}).get("properties") if isinstance(r.body, dict) else None
    if not p or (p.get("temperature") or {}).get("value") is None:
        raise RuntimeError("nws observation HTTP %s" % r.status)
    c = float(p["temperature"]["value"])
    temp = round(c * 9 / 5 + 32) if _imperial(ctx.cfg) else round(c)
    desc = str(p.get("textDescription") or "").lower() or "?"
    icon = next((ic for k, ic in _NWS_ICONS if k in desc), "·")
    hum = (p.get("relativeHumidity") or {}).get("value")
    return {"icon": icon, "desc": desc, "temp": temp, "hum": round(hum) if hum is not None else None,
            "nws_station": station}


def wttr(ctx, lat, lon):
    loc = ctx.cfg.get("wttr_location") or ("%s,%s" % (lat, lon) if lat is not None else "")
    imp = _imperial(ctx.cfg)
    url = "https://wttr.in/%s?format=%%c|%%t|%%C&%s" % (urllib.parse.quote(str(loc)), "u" if imp else "m")
    r = ctx.http(url, timeout=5, headers={"Accept": "text/plain"})
    body = r.body if isinstance(r.body, str) else ""
    if not (r.ok and body.count("|") == 2) or "unknown location" in body.lower():
        raise RuntimeError("wttr HTTP %s" % r.status)
    icon, temp, desc = [x.strip() for x in body.split("|")]
    return {"icon": icon, "desc": desc.lower(), "temp": int(re.sub(r"[^\d-]", "", temp) or 0)}


PROVIDERS = {"open-meteo": open_meteo, "nws": nws, "wttr": wttr}


@source("weather", ttl=1800, timeout=15, scope="global", defaults={
    "latitude": None, "longitude": None, "place": "", "units": "imperial", "providers": [],
    "wttr_location": "", "nws_station": "", "user_agent": "", "timezone": "auto"})
def weather(ctx):
    """Current conditions and today's range for the header."""
    lat, lon = ctx.cfg.get("latitude"), ctx.cfg.get("longitude")
    order = ctx.cfg.get("providers") or (["open-meteo", "nws", "wttr"] if lat is not None and lon is not None else ["wttr"])
    errors = []
    for name in order:
        fn = PROVIDERS.get(name)
        if not fn or (name == "open-meteo" and (lat is None or lon is None)):
            continue
        try:
            d = fn(ctx, lat, lon)
        except Exception as e:  # noqa: BLE001 — fall through to the next provider
            errors.append("%s: %s" % (name, str(e)[:60] or type(e).__name__))
            continue
        d.update(src=name, place=ctx.cfg.get("place") or "", unit="°F" if _imperial(ctx.cfg) else "°C")
        return d
    raise RuntimeError("every weather provider failed (%s)" % "; ".join(errors or ["none configured"]))
