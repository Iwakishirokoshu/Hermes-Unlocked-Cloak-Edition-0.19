# Gmail Factory — Hermes vendor

Slim, Cloak-integrated build of the Gmail account-creation engine.

Installed by `scripts/install_gmail_factory.sh` into
`/usr/local/lib/gmail-factory/` on the VPS. Hermes calls it via the
four `gmail_factory_*` tools registered by `hermes-plugin-cloak`.

## Entry point

`hermes_runner.py` — non-interactive CLI driver. Reads JSON on stdin,
runs a batch through `core.batch_runner.run_batch`, prints a structured
JSON result on stdout.

Example payload (sent by the plug-in's `gmail_factory_create` tool):

```json
{
    "count": 1,
    "use_sms": false,
    "sms_provider": null,
    "warmup_minutes": 0,
    "flow_mode": "standard"
}
```

Example result:

```json
{
    "total": 1,
    "successes": 1,
    "failures": 0,
    "duration": 132.4,
    "results": [
        {
            "index": 0,
            "username": "...",
            "email": "...@gmail.com",
            "password": "...",
            "success": true,
            "proxy": "...",
            "error_type": null
        }
    ]
}
```

## Environment

Reads `/etc/gmail-factory/runner.env` (created by the installer). Key
variables:

- `BROWSER_CDP_URL` — exported by Hermes after `cloak_set_active`. When
  set, the engine attaches to the Cloak browser instead of launching a
  local Chromium.
- `FIVESIM_API_KEY`, `SMS_ACTIVATE_API_KEY`, `ONLINESIM_API_KEY`,
  `GETSMS_API_KEY` — SMS providers (any one is enough for `use_sms=true`).
- `TWOCAPTCHA_API_KEY`, `ANTICAPTCHA_API_KEY`, `CAPMONSTER_API_KEY` —
  CAPTCHA solvers (optional, raises success rate).
- `YOUR_PASSWORD` — optional fixed password for every account; leave empty
  to auto-generate a unique random password per account (recommended).
  leave empty to auto-generate per account.
- `YOUR_BIRTHDAY` (e.g. `"2 4 1990"`), `YOUR_GENDER` (1/2/3).
- `ENABLE_PROXY=False` by default; flip to `True` and populate
  `config/proxies.txt` if you want a per-call proxy rotation
  independent from the Cloak profile.

The installer pulls existing keys from `/etc/cloak/manager.env` so a
single source of truth covers both products.

## Standalone use

This vendor will also run without Hermes — just call

```bash
/usr/local/lib/gmail-factory/venv/bin/python \
    /usr/local/lib/gmail-factory/hermes_runner.py \
    --count 1 --no-sms
```

…and it falls back to the upstream local-Chromium path.

See [`NOTICE`](NOTICE) for licensing and provenance information.
