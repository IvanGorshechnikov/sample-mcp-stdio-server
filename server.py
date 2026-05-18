#!/usr/bin/env python3
"""
DBeaver MCP Data Server
=======================
An MCP (Model Context Protocol) server that fetches real-world data from free
public APIs and can convert it to SQL INSERT statements.

Useful in DBeaver AI workflows for:
  • Seeding tables with realistic data (users, countries, currencies)
  • Getting current reference data (exchange rates, crypto prices, timezones)
  • Instantly converting any JSON array into ready-to-run SQL

All APIs are free and require no API keys.
No database connection required – the server only returns data.

Tools
-----
  echo              Connectivity / round-trip check
  server_info       Runtime configuration
  weather           Current weather for any city           (wttr.in)
  exchange_rates    Live FX rates, EUR base                (frankfurter.app)
  random_users      Realistic fake user records            (randomuser.me)
  countries         Country reference data (196 countries) (restcountries.com)
  crypto_prices     Crypto market prices                   (coingecko.com)
  time_now          Current time in any IANA timezone      (worldtimeapi.org)
  http_get          Generic JSON GET – any public API
  to_sql_inserts    Convert a JSON array → SQL INSERT statements

CLI arguments
-------------
  --timeout SECS    HTTP request timeout in seconds (default: 10)
  --offline         Return stub data instead of real HTTP requests (for tests)
  --log-level LEVEL quiet | normal | verbose  (default: normal)

Environment variables
---------------------
  MCP_TIMEOUT       Equivalent to --timeout
  MCP_OFFLINE       Set to '1' to enable offline/stub mode
  MCP_LOG_LEVEL     Equivalent to --log-level

Protocol
--------
  MCP protocol version : 2024-11-05
  Transport            : stdio (JSON-RPC 2.0, one message per line)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import textwrap
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ssl._create_default_https_context = ssl._create_unverified_context  # noqa: SLF001
_SSL_CTX = ssl._create_unverified_context()  # noqa: SLF001

PROTOCOL_VERSION = "2024-11-05"
SERVER_VERSION   = "3.1.0"
SERVER_NAME      = "dbeaver-data-mcp"

# ---------------------------------------------------------------------------
# Global runtime config
# Priority: CLI args > env vars > mcp-config.json in CWD > built-in defaults
# ---------------------------------------------------------------------------
CONFIG: dict[str, Any] = {
    "timeout":        10,
    "offline":        False,
    "log_level":      "normal",
    "cwd":            str(Path.cwd()),
    "config_file":    None,   # path to mcp-config.json if found/loaded
    "config_from_file": {},   # raw dict loaded from that file
}

# ---------------------------------------------------------------------------
# Transport + logging
# ---------------------------------------------------------------------------

def _lvl(level: str) -> int:
    return {"quiet": 0, "normal": 1, "verbose": 2}.get(level.lower(), 1)


def log(text: str, level: str = "normal") -> None:
    if _lvl(level) <= _lvl(CONFIG["log_level"]):
        sys.stderr.write(f"[{SERVER_NAME}] {text}\n")
        sys.stderr.flush()


def write_message(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def result_response(rid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def error_response(rid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def ok(text: str, is_error: bool = False) -> dict:
    r: dict = {"content": [{"type": "text", "text": text}]}
    if is_error:
        r["isError"] = True
    return r


def err(text: str) -> dict:
    return ok(text, is_error=True)


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _http_get(url: str, extra_headers: dict[str, str] | None = None) -> Any:
    """Fetch URL, return parsed JSON. Raises on error."""
    log(f"GET {url}", "verbose")
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", f"{SERVER_NAME}/{SERVER_VERSION}")
    for k, v in (extra_headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=CONFIG["timeout"], context=_SSL_CTX) as resp:
        return json.loads(resp.read().decode())


# ---------------------------------------------------------------------------
# SQL INSERT generator (pure Python, no deps)
# ---------------------------------------------------------------------------

def _sql_value(v: Any, dialect: str) -> str:
    """Convert a Python value to a SQL literal."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        if dialect in ("mysql",):
            return "1" if v else "0"
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (dict, list)):
        escaped = json.dumps(v, ensure_ascii=False).replace("'", "''")
        return f"'{escaped}'"
    # string
    escaped = str(v).replace("'", "''")
    return f"'{escaped}'"


def _quote_ident(name: str, dialect: str) -> str:
    if dialect == "mysql":
        return f"`{name}`"
    if dialect in ("postgresql", "sqlite", "generic"):
        return f'"{name}"'
    return name


def build_insert_statements(
    table: str,
    rows: list[dict],
    dialect: str = "generic",
    batch_size: int = 1,
) -> str:
    if not rows:
        return "-- No rows to insert"
    columns = list(rows[0].keys())
    q_table = _quote_ident(table, dialect)
    q_cols  = ", ".join(_quote_ident(c, dialect) for c in columns)

    def row_values(row: dict) -> str:
        return "(" + ", ".join(_sql_value(row.get(c), dialect) for c in columns) + ")"

    lines: list[str] = []
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        vals  = ",\n       ".join(row_values(r) for r in batch)
        lines.append(f"INSERT INTO {q_table} ({q_cols})\nVALUES {vals};")

    header = (
        f"-- Table  : {table}\n"
        f"-- Rows   : {len(rows)}\n"
        f"-- Dialect: {dialect}\n"
    )
    return header + "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Offline stubs
# ---------------------------------------------------------------------------

_STUBS: dict[str, Any] = {
    "weather": {
        "city": "Berlin",
        "condition": "Partly cloudy",
        "temp_c": 14,
        "temp_f": 57,
        "humidity_pct": 68,
        "wind_kmh": 22,
        "feels_like_c": 12,
        "uv_index": 3,
        "observation_time": "12:00 PM",
    },
    "exchange_rates": {
        "base": "EUR",
        "date": "2026-05-18",
        "rates": {"USD": 1.08, "GBP": 0.85, "JPY": 163.4, "CHF": 0.97, "CNY": 7.84},
    },
    "random_users": [
        {"first_name": "Alice", "last_name": "Mueller", "email": "alice.mueller@example.com",
         "gender": "female", "age": 32, "country": "Germany", "city": "Berlin",
         "phone": "+49 30 111222", "username": "alice_m"},
        {"first_name": "Bob", "last_name": "Smith", "email": "bob.smith@example.com",
         "gender": "male", "age": 28, "country": "USA", "city": "New York",
         "phone": "+1 212 555 0100", "username": "bob_s"},
    ],
    "countries": [
        {"name": "Germany", "capital": "Berlin", "region": "Europe",
         "population": 83200000, "area_km2": 357114, "currency_code": "EUR",
         "currency_name": "Euro", "language": "German"},
        {"name": "Japan", "capital": "Tokyo", "region": "Asia",
         "population": 125700000, "area_km2": 377930, "currency_code": "JPY",
         "currency_name": "Japanese yen", "language": "Japanese"},
    ],
    "crypto_prices": [
        {"symbol": "BTC", "name": "Bitcoin",  "price_usd": 65420.0, "change_24h_pct": 1.23},
        {"symbol": "ETH", "name": "Ethereum", "price_usd": 3120.5,  "change_24h_pct": -0.54},
    ],
    "time_now": {
        "timezone": "Europe/Berlin",
        "datetime": "2026-05-18T14:00:00+02:00",
        "utc_offset": "+02:00",
        "day_of_week": "Monday",
        "week_number": 21,
    },
}

# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def tool_echo(args: dict) -> dict:
    return ok(str(args.get("text", "")))


def tool_server_info(_args: dict) -> dict:
    cfg_file_line = (
        f"  config_file : {CONFIG['config_file']}"
        if CONFIG["config_file"]
        else "  config_file : (mcp-config.json not found in CWD)"
    )
    cfg_values = CONFIG["config_from_file"]
    cfg_detail = (
        "\n".join(f"    {k}: {v}" for k, v in cfg_values.items())
        if cfg_values else "    (empty)"
    )
    lines = [
        f"Server    : {SERVER_NAME} v{SERVER_VERSION}",
        f"Protocol  : {PROTOCOL_VERSION}",
        f"CWD       : {CONFIG['cwd']}",
        "",
        "── Configuration ─────────────────────────────────────",
        f"  timeout   : {CONFIG['timeout']} s",
        f"  offline   : {CONFIG['offline']}",
        f"  log_level : {CONFIG['log_level']}",
        cfg_file_line,
        "  mcp-config.json values applied:",
        cfg_detail,
        "",
        "── Env vars (MCP_* recognised) ───────────────────────",
        f"  MCP_TIMEOUT  = {os.environ.get('MCP_TIMEOUT',  '(not set)')}",
        f"  MCP_OFFLINE  = {os.environ.get('MCP_OFFLINE',  '(not set)')}",
        f"  MCP_LOG_LEVEL= {os.environ.get('MCP_LOG_LEVEL','(not set)')}",
        "",
        "── CLI flags ─────────────────────────────────────────",
        "  --timeout SECS   --offline   --log-level LEVEL",
        "",
        "── Tools ─────────────────────────────────────────────",
        "  echo, server_info, list_cwd, weather, exchange_rates,",
        "  random_users, countries, crypto_prices, time_now,",
        "  http_get, to_sql_inserts",
    ]
    return ok("\n".join(lines))


def tool_list_cwd(_args: dict) -> dict:
    """List files and directories in the server's working directory."""
    cwd = Path(CONFIG["cwd"])
    try:
        entries = sorted(cwd.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except PermissionError as exc:
        return err(f"Cannot read CWD '{cwd}': {exc}")

    lines = [f"Working directory: {cwd}", ""]
    dirs  = [e for e in entries if e.is_dir()]
    files = [e for e in entries if e.is_file()]

    if dirs:
        lines.append("Directories:")
        for d in dirs:
            lines.append(f"  [dir]  {d.name}/")
    if files:
        lines.append("Files:")
        for f in files:
            size     = f.stat().st_size
            size_str = f"{size:,} B" if size < 1024 else f"{size // 1024:,} KB"
            marker   = "  <-- active config" if f.name == "mcp-config.json" else ""
            lines.append(f"  [file] {f.name:<38} {size_str}{marker}")

    if not dirs and not files:
        lines.append("  (empty directory)")

    lines += [
        "",
        f"mcp-config.json: {'loaded from ' + str(CONFIG['config_file']) if CONFIG['config_file'] else 'not found in CWD'}",
    ]
    return ok("\n".join(lines))


# -- Data-fetching tools -----------------------------------------------------

def tool_weather(args: dict) -> dict:
    city = args.get("city", "").strip()
    if not city:
        return err("Parameter 'city' is required.")

    if CONFIG["offline"]:
        data = {**_STUBS["weather"], "city": city}
    else:
        try:
            raw = _http_get(f"https://wttr.in/{urllib.request.quote(city)}?format=j1")
            cur = raw["current_condition"][0]
            data = {
                "city":             city,
                "condition":        cur["weatherDesc"][0]["value"],
                "temp_c":           int(cur["temp_C"]),
                "temp_f":           int(cur["temp_F"]),
                "humidity_pct":     int(cur["humidity"]),
                "wind_kmh":         int(cur["windspeedKmph"]),
                "feels_like_c":     int(cur["FeelsLikeC"]),
                "uv_index":         int(cur.get("uvIndex", 0)),
                "observation_time": cur.get("observation_time", ""),
            }
        except Exception as exc:
            return err(f"Could not fetch weather for '{city}': {exc}")

    lines = [
        f"Weather in {data['city']}",
        f"  Condition   : {data['condition']}",
        f"  Temperature : {data['temp_c']} °C  /  {data['temp_f']} °F",
        f"  Feels like  : {data['feels_like_c']} °C",
        f"  Humidity    : {data['humidity_pct']} %",
        f"  Wind        : {data['wind_kmh']} km/h",
        f"  UV index    : {data['uv_index']}",
        f"  Observed at : {data['observation_time']}",
    ]
    return ok("\n".join(lines))


def tool_exchange_rates(args: dict) -> dict:
    base     = (args.get("base", "EUR") or "EUR").upper()
    symbols_raw = args.get("symbols", "") or ""
    if isinstance(symbols_raw, list):
        symbols = [s.upper() for s in symbols_raw]
    else:
        symbols = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]

    if CONFIG["offline"]:
        payload = _STUBS["exchange_rates"].copy()
        payload["base"] = base
    else:
        try:
            url = f"https://api.frankfurter.app/latest?base={base}"
            if symbols:
                url += "&symbols=" + ",".join(symbols)
            payload = _http_get(url)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                # Fetch supported currencies to show in the error
                try:
                    supported = _http_get("https://api.frankfurter.app/currencies")
                    codes = ", ".join(sorted(supported.keys()))
                    return err(
                        f"Currency '{base}' is not supported by frankfurter.app.\n\n"
                        f"Supported currencies ({len(supported)}):\n  {codes}"
                    )
                except Exception:
                    pass
            return err(f"Could not fetch exchange rates: {exc}")
        except Exception as exc:
            return err(f"Could not fetch exchange rates: {exc}")

    rates = payload.get("rates", {})
    if symbols:
        rates = {k: v for k, v in rates.items() if k in symbols}

    lines = [
        f"Exchange rates  (base: {payload.get('base', base)}, date: {payload.get('date', 'n/a')})",
        "─" * 40,
    ]
    for cur, rate in sorted(rates.items()):
        lines.append(f"  1 {base} = {rate:>12.4f} {cur}")

    if args.get("as_sql"):
        rows = [{"base": base, "quote": c, "rate": r, "date": payload.get("date")}
                for c, r in rates.items()]
        lines += ["", "── SQL ──────────────────────────────────────────────",
                  build_insert_statements("exchange_rates", rows,
                                          args.get("dialect", "generic"))]
    return ok("\n".join(lines))


def tool_random_users(args: dict) -> dict:
    count = min(max(int(args.get("count", 5)), 1), 50)

    if CONFIG["offline"]:
        rows = (_STUBS["random_users"] * ((count // 2) + 1))[:count]
    else:
        try:
            raw  = _http_get(f"https://randomuser.me/api/?results={count}&nat=us,gb,de,fr,jp")
            rows = []
            for u in raw["results"]:
                rows.append({
                    "first_name": u["name"]["first"],
                    "last_name":  u["name"]["last"],
                    "email":      u["email"],
                    "gender":     u["gender"],
                    "age":        u["dob"]["age"],
                    "country":    u["location"]["country"],
                    "city":       u["location"]["city"],
                    "phone":      u["phone"],
                    "username":   u["login"]["username"],
                })
        except Exception as exc:
            return err(f"Could not fetch random users: {exc}")

    if args.get("as_sql"):
        table   = args.get("table", "users")
        dialect = args.get("dialect", "generic")
        return ok(build_insert_statements(table, rows, dialect))

    # Table view
    hdr  = f"  {'Name':<25} {'Email':<35} {'Country':<15} Age"
    sep  = "  " + "─" * 80
    body = "\n".join(
        f"  {r['first_name'] + ' ' + r['last_name']:<25} "
        f"{r['email']:<35} {r['country']:<15} {r['age']}"
        for r in rows
    )
    return ok(f"Random users ({len(rows)}):\n{hdr}\n{sep}\n{body}")


def tool_countries(args: dict) -> dict:
    region  = (args.get("region", "") or "").strip().lower()
    limit   = min(max(int(args.get("limit", 20)), 1), 250)

    if CONFIG["offline"]:
        rows = _STUBS["countries"][:limit]
    else:
        try:
            fields = "name,capital,region,subregion,population,area,currencies,languages"
            raw    = _http_get(f"https://restcountries.com/v3.1/all?fields={fields}")
            rows   = []
            for c in raw:
                r = c.get("region", "")
                if region and region not in r.lower():
                    continue
                cur_code  = next(iter(c.get("currencies", {})), "")
                cur_name  = (c.get("currencies") or {}).get(cur_code, {}).get("name", "")
                lang      = next(iter((c.get("languages") or {}).values()), "")
                cap_list  = c.get("capital") or [""]
                rows.append({
                    "name":          c["name"]["common"],
                    "capital":       cap_list[0] if cap_list else "",
                    "region":        r,
                    "subregion":     c.get("subregion", ""),
                    "population":    c.get("population", 0),
                    "area_km2":      c.get("area", 0),
                    "currency_code": cur_code,
                    "currency_name": cur_name,
                    "language":      lang,
                })
            rows.sort(key=lambda x: x["name"])
            rows = rows[:limit]
        except Exception as exc:
            return err(f"Could not fetch countries: {exc}")

    if args.get("as_sql"):
        table   = args.get("table", "countries")
        dialect = args.get("dialect", "generic")
        return ok(build_insert_statements(table, rows, dialect))

    # Table view
    hdr  = f"  {'Country':<25} {'Capital':<18} {'Region':<14} {'Population':>12}  Currency"
    sep  = "  " + "─" * 82
    body = "\n".join(
        f"  {r['name']:<25} {r['capital']:<18} {r['region']:<14} "
        f"{r['population']:>12,}  {r['currency_code']}"
        for r in rows
    )
    return ok(f"Countries ({len(rows)}):\n{hdr}\n{sep}\n{body}")


def tool_crypto_prices(args: dict) -> dict:
    coins_raw = args.get("coins", "bitcoin,ethereum,litecoin") or "bitcoin,ethereum,litecoin"
    if isinstance(coins_raw, list):
        coins = [c.strip() for c in coins_raw if c.strip()]
    else:
        coins = [c.strip() for c in coins_raw.split(",") if c.strip()]

    if CONFIG["offline"]:
        rows = _STUBS["crypto_prices"]
    else:
        try:
            ids  = ",".join(coins)
            raw  = _http_get(
                f"https://api.coingecko.com/api/v3/coins/markets"
                f"?vs_currency=usd&ids={ids}&order=market_cap_desc"
                f"&per_page={len(coins)}&page=1&sparkline=false"
            )
            rows = [
                {
                    "symbol":        r["symbol"].upper(),
                    "name":          r["name"],
                    "price_usd":     r["current_price"],
                    "change_24h_pct": round(r.get("price_change_percentage_24h") or 0, 2),
                    "market_cap_usd": r.get("market_cap", 0),
                    "volume_24h_usd": r.get("total_volume", 0),
                }
                for r in raw
            ]
        except Exception as exc:
            return err(f"Could not fetch crypto prices: {exc}")

    if args.get("as_sql"):
        table   = args.get("table", "crypto_prices")
        dialect = args.get("dialect", "generic")
        return ok(build_insert_statements(table, rows, dialect))

    lines = [f"Crypto prices (USD):", "  " + "─" * 60]
    for r in rows:
        chg   = r.get("change_24h_pct", 0) or 0
        arrow = "▲" if chg >= 0 else "▼"
        lines.append(
            f"  {r['symbol']:<6} {r['name']:<14}  "
            f"${r['price_usd']:>14,.2f}   {arrow} {abs(chg):.2f} % (24h)"
        )
    return ok("\n".join(lines))


def tool_time_now(args: dict) -> dict:
    tz = (args.get("timezone", "UTC") or "UTC").strip()

    if CONFIG["offline"]:
        data = {**_STUBS["time_now"], "timezone": tz}
    else:
        try:
            data = _http_get(f"https://worldtimeapi.org/api/timezone/{tz}")
        except Exception as exc:
            # Fallback: list available timezones hint
            if "404" in str(exc) or "Not Found" in str(exc):
                try:
                    available = _http_get("https://worldtimeapi.org/api/timezone")
                    matches   = [t for t in available if tz.lower() in t.lower()][:10]
                    hint      = "\n  ".join(matches) if matches else "(no matches)"
                    return err(f"Timezone '{tz}' not found.\nDid you mean:\n  {hint}")
                except Exception:
                    pass
            return err(f"Could not fetch time for '{tz}': {exc}")

    lines = [
        f"Timezone  : {data.get('timezone', tz)}",
        f"Date/Time : {data.get('datetime', '?')}",
        f"UTC offset: {data.get('utc_offset', '?')}",
        f"Day       : {data.get('day_of_week', '?')}  (week {data.get('week_number', '?')})",
    ]
    dst = data.get("dst")
    if dst is not None:
        lines.append(f"DST active: {dst}")
    return ok("\n".join(lines))


def tool_http_get(args: dict) -> dict:
    url      = (args.get("url") or "").strip()
    if not url:
        return err("Parameter 'url' is required.")
    if not url.startswith(("http://", "https://")):
        return err("URL must start with http:// or https://")

    headers = args.get("headers") or {}
    max_len = min(int(args.get("max_length", 4000)), 16000)

    if CONFIG["offline"]:
        return ok(f"[offline mode] Would GET: {url}")

    try:
        data = _http_get(url, extra_headers=headers)
    except urllib.error.HTTPError as exc:
        return err(f"HTTP {exc.code}: {exc.reason}  ({url})")
    except Exception as exc:
        return err(f"Request failed: {exc}")

    as_sql = args.get("as_sql")
    if as_sql:
        table   = args.get("table") or "data"
        dialect = args.get("dialect", "generic")
        rows    = data if isinstance(data, list) else ([data] if isinstance(data, dict) else None)
        if rows is None:
            return err("Response is not a JSON object or array – cannot convert to SQL.")
        return ok(build_insert_statements(table, rows, dialect))

    text = json.dumps(data, indent=2, ensure_ascii=False)
    if len(text) > max_len:
        text = text[:max_len] + f"\n... (truncated at {max_len} chars)"
    return ok(text)


def tool_to_sql_inserts(args: dict) -> dict:
    raw_data = args.get("data")
    table    = (args.get("table") or "data").strip()
    dialect  = (args.get("dialect") or "generic").lower()
    batch    = max(1, min(int(args.get("batch_size", 1)), 500))

    if raw_data is None:
        return err("Parameter 'data' is required (JSON array or JSON object).")
    if isinstance(raw_data, str):
        try:
            raw_data = json.loads(raw_data)
        except json.JSONDecodeError as exc:
            return err(f"'data' is not valid JSON: {exc}")

    rows: list[dict]
    if isinstance(raw_data, dict):
        rows = [raw_data]
    elif isinstance(raw_data, list):
        rows = raw_data
    else:
        return err("'data' must be a JSON object or array of objects.")

    if not rows:
        return ok("-- Empty array – nothing to insert.")

    # Flatten top-level – skip non-dict items
    rows = [r for r in rows if isinstance(r, dict)]
    if not rows:
        return err("Array contains no JSON objects.")

    return ok(build_insert_statements(table, rows, dialect, batch))


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

TOOLS: dict[str, dict] = {
    "echo": {
        "handler": tool_echo,
        "definition": {
            "name": "echo",
            "title": "Echo / Ping",
            "description": "Echoes text back. Useful for connectivity checks.",
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    },
    "server_info": {
        "handler": tool_server_info,
        "definition": {
            "name": "server_info",
            "title": "Server Information",
            "description": "Returns server version, active configuration and list of available tools.",
            "inputSchema": {"type": "object", "properties": {}},
        },
    },
    "list_cwd": {
        "handler": tool_list_cwd,
        "definition": {
            "name": "list_cwd",
            "title": "List Working Directory",
            "description": (
                "Lists files and directories in the server's working directory. "
                "Also shows whether mcp-config.json was found and loaded. "
                "Useful for verifying that the correct workingDirectory is configured."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
    },
    "weather": {
        "handler": tool_weather,
        "definition": {
            "name": "weather",
            "title": "Current Weather",
            "description": "Returns current weather conditions for any city. Source: wttr.in (no API key required).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "City name, e.g. 'Berlin' or 'New York'"},
                },
                "required": ["city"],
            },
        },
    },
    "exchange_rates": {
        "handler": tool_exchange_rates,
        "definition": {
            "name": "exchange_rates",
            "title": "Live Exchange Rates",
            "description": (
                "Returns live foreign exchange rates. Source: frankfurter.app (no API key). "
                "Set as_sql=true to get INSERT statements ready to run in DBeaver."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "base":    {"type": "string", "description": "Base currency code, e.g. 'EUR' (default)."},
                    "symbols": {"type": "string", "description": "Comma-separated currency codes to include, e.g. 'USD,GBP,JPY'. Omit for all currencies."},
                    "as_sql":  {"type": "boolean", "description": "Return INSERT statements instead of table view."},
                    "table":   {"type": "string",  "description": "Target table name for INSERT (default: exchange_rates)."},
                    "dialect": {"type": "string",  "description": "postgresql | mysql | sqlite | generic"},
                },
            },
        },
    },
    "random_users": {
        "handler": tool_random_users,
        "definition": {
            "name": "random_users",
            "title": "Random User Generator",
            "description": (
                "Generates realistic fake user records (names, emails, locations). "
                "Source: randomuser.me. Set as_sql=true for INSERT statements."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "count":   {"type": "integer", "description": "Number of users to generate (1–50, default 5)."},
                    "as_sql":  {"type": "boolean", "description": "Return INSERT statements."},
                    "table":   {"type": "string",  "description": "Target table (default: users)."},
                    "dialect": {"type": "string",  "description": "postgresql | mysql | sqlite | generic"},
                },
            },
        },
    },
    "countries": {
        "handler": tool_countries,
        "definition": {
            "name": "countries",
            "title": "Country Reference Data",
            "description": (
                "Returns country reference data: capital, region, population, area, currency, language. "
                "Source: restcountries.com. Great for seeding a countries/currencies table."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "region":  {"type": "string",  "description": "Filter by region, e.g. 'Europe' or 'Asia'."},
                    "limit":   {"type": "integer", "description": "Max rows (default 20, max 250)."},
                    "as_sql":  {"type": "boolean", "description": "Return INSERT statements."},
                    "table":   {"type": "string",  "description": "Target table (default: countries)."},
                    "dialect": {"type": "string",  "description": "postgresql | mysql | sqlite | generic"},
                },
            },
        },
    },
    "crypto_prices": {
        "handler": tool_crypto_prices,
        "definition": {
            "name": "crypto_prices",
            "title": "Cryptocurrency Prices",
            "description": (
                "Returns current cryptocurrency prices from CoinGecko (no API key). "
                "Useful for financial SQL demos and seeding price tables."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "coins":   {"type": "string",
                                "description": "Comma-separated CoinGecko coin IDs, e.g. 'bitcoin,ethereum,litecoin'. Default: bitcoin,ethereum,litecoin."},
                    "as_sql":  {"type": "boolean", "description": "Return INSERT statements."},
                    "table":   {"type": "string",  "description": "Target table (default: crypto_prices)."},
                    "dialect": {"type": "string",  "description": "postgresql | mysql | sqlite | generic"},
                },
            },
        },
    },
    "time_now": {
        "handler": tool_time_now,
        "definition": {
            "name": "time_now",
            "title": "Current Date & Time",
            "description": "Returns the current date/time in any IANA timezone. Source: worldtimeapi.org.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "timezone": {"type": "string", "description": "IANA timezone, e.g. 'Europe/Berlin' or 'America/New_York'. Default: UTC."},
                },
            },
        },
    },
    "http_get": {
        "handler": tool_http_get,
        "definition": {
            "name": "http_get",
            "title": "HTTP GET Request",
            "description": (
                "Performs a GET request to any public JSON API and returns the response. "
                "Set as_sql=true to convert the JSON response directly to SQL INSERT statements."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url":        {"type": "string",  "description": "URL to fetch (must be http/https)."},
                    "headers":    {"type": "object",  "description": "Optional HTTP headers as key-value pairs."},
                    "max_length": {"type": "integer", "description": "Max response chars to return (default 4000)."},
                    "as_sql":     {"type": "boolean", "description": "Convert JSON response to INSERT statements."},
                    "table":      {"type": "string",  "description": "Target table name for INSERT."},
                    "dialect":    {"type": "string",  "description": "postgresql | mysql | sqlite | generic"},
                },
                "required": ["url"],
            },
        },
    },
    "to_sql_inserts": {
        "handler": tool_to_sql_inserts,
        "definition": {
            "name": "to_sql_inserts",
            "title": "Convert JSON to SQL INSERT Statements",
            "description": (
                "Converts a JSON object or array of objects into SQL INSERT statements. "
                "Works with any data – paste API response, CSV data as JSON, etc."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "data":       {"description": "JSON object or array of objects to convert."},
                    "table":      {"type": "string",  "description": "Target table name (default: data)."},
                    "dialect":    {"type": "string",  "description": "postgresql | mysql | sqlite | generic (default: generic)."},
                    "batch_size": {"type": "integer", "description": "Rows per INSERT statement (default 1, max 500)."},
                },
                "required": ["data"],
            },
        },
    },
}

# ---------------------------------------------------------------------------
# MCP dispatcher
# ---------------------------------------------------------------------------

def handle_request(message: dict) -> dict | None:
    rid    = message.get("id")
    method = message.get("method", "")
    params = message.get("params") or {}

    log(f"→ {method} (id={rid})", "verbose")

    if method == "initialize":
        log(f"  client protocolVersion={params.get('protocolVersion', '?')}")
        return result_response(rid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {
                "tools":     {"listChanged": False},
                "resources": {"listChanged": False},
                "prompts":   {"listChanged": False},
            },
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    if method == "ping":
        return result_response(rid, {})

    if method == "tools/list":
        return result_response(rid, {"tools": [t["definition"] for t in TOOLS.values()]})

    if method == "tools/call":
        name      = params.get("name")
        arguments = params.get("arguments") or {}
        tool      = TOOLS.get(name)
        if tool is None:
            return error_response(rid, -32602, f"Unknown tool: {name}")
        log(f"  tool '{name}' args={list(arguments.keys())}", "verbose")
        try:
            return result_response(rid, tool["handler"](arguments))
        except Exception as exc:
            log(f"  ERROR in '{name}': {exc}")
            return error_response(rid, -32603, f"Internal error in '{name}': {exc}")

    if method in ("resources/list", "prompts/list"):
        key = "resources" if "resources" in method else "prompts"
        return result_response(rid, {key: []})

    return error_response(rid, -32601, f"Method not found: {method}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="server.py",
        description="DBeaver MCP Data Server – fetches real-world data for SQL workflows",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Environment variables (lower priority than CLI args):
              MCP_TIMEOUT    HTTP timeout in seconds
              MCP_OFFLINE    Set to '1' to use stub data (no internet)
              MCP_LOG_LEVEL  quiet | normal | verbose

            Examples:
              python server.py
              python server.py --timeout 5 --log-level verbose
              python server.py --offline
              MCP_OFFLINE=1 python server.py
        """),
    )
    p.add_argument("--timeout",   type=int, metavar="SECS",
                   help="HTTP request timeout in seconds (default: 10)")
    p.add_argument("--offline",   action="store_true",
                   help="Return stub data instead of live HTTP requests")
    p.add_argument("--log-level", metavar="LEVEL",
                   choices=["quiet", "normal", "verbose"],
                   help="Logging verbosity (default: normal)")
    return p


def main() -> None:
    args = _build_parser().parse_args()

    # Step 1: lowest priority – mcp-config.json in CWD
    cwd_config_path = Path(CONFIG["cwd"]) / "mcp-config.json"
    if cwd_config_path.exists():
        try:
            file_cfg = json.loads(cwd_config_path.read_text(encoding="utf-8"))
            CONFIG["config_file"]     = str(cwd_config_path)
            CONFIG["config_from_file"] = file_cfg
            # Apply file values as defaults (will be overridden by env/args below)
            if "timeout"   in file_cfg: CONFIG["timeout"]   = int(file_cfg["timeout"])
            if "offline"   in file_cfg: CONFIG["offline"]   = bool(file_cfg["offline"])
            if "log_level" in file_cfg: CONFIG["log_level"] = str(file_cfg["log_level"])
        except Exception as exc:
            sys.stderr.write(f"[{SERVER_NAME}] WARNING: could not load {cwd_config_path}: {exc}\n")

    # Step 2: env vars override file config
    if os.environ.get("MCP_TIMEOUT"):   CONFIG["timeout"]   = int(os.environ["MCP_TIMEOUT"])
    if os.environ.get("MCP_OFFLINE"):   CONFIG["offline"]   = os.environ["MCP_OFFLINE"] == "1"
    if os.environ.get("MCP_LOG_LEVEL"): CONFIG["log_level"] = os.environ["MCP_LOG_LEVEL"]

    # Step 3: CLI args have highest priority
    if args.timeout:   CONFIG["timeout"]   = args.timeout
    if args.offline:   CONFIG["offline"]   = True
    if args.log_level: CONFIG["log_level"] = args.log_level

    log(f"started v{SERVER_VERSION} (protocol {PROTOCOL_VERSION})")
    log(f"CWD={CONFIG['cwd']}  timeout={CONFIG['timeout']}s  "
        f"offline={CONFIG['offline']}  log_level={CONFIG['log_level']}")
    if CONFIG["config_file"]:
        log(f"config loaded from {CONFIG['config_file']}")

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            write_message(error_response(None, -32700, f"Parse error: {exc}"))
            continue

        # Notifications (no "id") must NOT be replied to – MCP spec §4.1
        if "id" not in message:
            log(f"  notification: {message.get('method', '?')} (ignored)", "verbose")
            continue

        response = handle_request(message)
        if response is not None:
            write_message(response)


if __name__ == "__main__":
    main()
