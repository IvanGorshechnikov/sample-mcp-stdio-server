# Sample MCP Data Server

An MCP server over **stdio** that fetches real-world data from free public APIs
and converts it to SQL INSERT statements.  
Designed to be registered as an external MCP server in **DBeaver**.

## Quick start

```bash
python3 tools/mcp-stdio-test-server/server.py
```

---

## Connection configuration

Paste this JSON when adding an MCP server in DBeaver → Settings → AI → MCP:

```json
{
  "type": "STDIO",
  "command": "python3",
  "args": [
    "/absolute/path/to/server.py"
  ]
}
```

After connecting, call the **`server_info`** tool to verify the active
configuration, or **`list_cwd`** to verify the working directory.

---


### 1 · CLI arguments (`args`)

| Argument | Type | Default | Description |
|---|---|---|---|
| `--timeout SECS` | int | `10` | HTTP request timeout in seconds |
| `--offline` | flag | `false` | Use built-in stub data, no internet |
| `--log-level LEVEL` | string | `normal` | `quiet` / `normal` / `verbose` |

**Example — verbose logging with 5 s timeout:**

```json
{
  "type": "STDIO",
  "command": "python3",
  "args": [
    "/absolute/path/to/server.py",
    "--timeout", "5",
    "--log-level", "verbose"
  ]
}
```

**Example — offline mode (no HTTP requests, stub data returned):**

```json
{
  "type": "STDIO",
  "command": "python3",
  "args": [
    "/absolute/path/to/server.py",
    "--offline"
  ]
}
```

**How to verify:** call `server_info` → check `timeout` and `log_level` lines.

---

### 2 · Environment variables (`env`)

| Variable | Equivalent to |
|---|---|
| `MCP_TIMEOUT` | `--timeout` |
| `MCP_OFFLINE` | `--offline` (set to `"1"` to enable) |
| `MCP_LOG_LEVEL` | `--log-level` |

**DBeaver example:**

```json
{
  "type": "STDIO",
  "command": "python3",
  "args": ["/absolute/path/to/server.py"],
  "env": {
    "MCP_TIMEOUT": "5",
    "MCP_OFFLINE": "1",
    "MCP_LOG_LEVEL": "verbose"
  }
}
```

**How to verify:** call `server_info` → check the `Env vars` section —
it shows the raw value of each `MCP_*` variable as the process sees it.

---

### 3 · Working directory (`workingDirectory`)

**DBeaver example — different working directories for different test scenarios:**

```json
{
  "type": "STDIO",
  "command": "python3",
  "args": ["server.py"],
  "workingDirectory": "/absolute/path/to-server-dir"
}
```

**How to verify:**
- `server_info` → `config_file` line shows the full path if loaded, or  
  `(mcp-config.json not found in CWD)` if not.
- `list_cwd` → lists all files in the working directory and marks the active
  `mcp-config.json` with `<-- active config`.

---

## Tools

| Tool | Description |
|---|---|
| `echo` | Connectivity check — echoes input back |
| `server_info` | Active config: timeout, offline, log-level, env vars, CWD, loaded config file |
| `list_cwd` | Lists files in the working directory; flags `mcp-config.json` if present |
| `weather` | Current weather for any city (wttr.in) |
| `exchange_rates` | Live FX rates (frankfurter.app); `as_sql=true` → INSERT statements |
| `random_users` | Realistic fake users (randomuser.me); `as_sql=true` → INSERT statements |
| `countries` | Country reference data (restcountries.com); `as_sql=true` → INSERT statements |
| `crypto_prices` | Crypto market prices (coingecko.com); `as_sql=true` → INSERT statements |
| `time_now` | Current time in any IANA timezone (worldtimeapi.org) |
| `http_get` | GET any public JSON API; `as_sql=true` → INSERT statements |
| `to_sql_inserts` | Convert any JSON array to SQL INSERT statements |

All data tools support `dialect`: `postgresql` / `mysql` / `sqlite` / `generic`.

---

