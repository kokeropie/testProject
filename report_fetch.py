"""
Fetches KEDP's three external "daily transaction report" API partners -
KATRINA, COBT MT PROD, COBT DANAMON PROD - each exposing flight/train/hotel
endpoints that return a zip of CSV report(s) for a given date. New,
standalone module - does not modify pipeline.py, consumer.py, or any other
already-committed file's logic. Wired into app.py only as an additive new
sidebar page ("Fetch Daily Reports") and a Schedule page; see
report_fetch_scheduler.py for the recurring-run half.

Auth is HTTP Basic (ck as username, cs as password) - not documented
anywhere, found by probing the live APIs. Config (report_fetch_config.json,
gitignored) holds each source's ck, but never cs: cs is supplied at run
time, either via a per-source environment variable (CLI / scheduled runs,
named "<SOURCE>_CS", e.g. KATRINA_CS) or typed into the Streamlit form
(interactive runs) - mirrors sql_import.py's MSSQL_PASSWORD pattern.

cobt.id (no "www") 301-redirects to www.cobt.id and the redirect drops the
Authorization header, so cobt_mt's base_url below points straight at
www.cobt.id to avoid that hop entirely.

Usage (CLI):
    python report_fetch.py --outdir rawData/katrina_daily_reports
    python report_fetch.py --date 2026-07-13 --source katrina --outdir DIR
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import os
import ssl
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import certifi

PROJECT_DIR = Path(__file__).parent
REPORT_FETCH_CONFIG_PATH = PROJECT_DIR / "report_fetch_config.json"

# Explicit CA bundle rather than trusting the OS/venv Python to have one
# wired up correctly - python.org macOS builds need "Install Certificates
# .command" run manually or every HTTPS call fails with
# CERTIFICATE_VERIFY_FAILED, and a fresh Windows Python install can hit the
# same thing. certifi ships its own bundle so this works out of the box.
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

log = logging.getLogger("report_fetch")

SOURCES: dict[str, dict] = {
    "katrina": {"label": "KATRINA", "base_url": "https://www.katrina.id",
                "products": ["flight", "train", "hotel"]},
    "cobt_mt": {"label": "COBT MT PROD", "base_url": "https://www.cobt.id",
                "products": ["flight", "train", "hotel"]},
    "cobt_danamon": {"label": "COBT DANAMON PROD", "base_url": "https://danamon.cobt.id",
                      "products": ["flight", "train", "hotel"]},
}

_RETRY_ATTEMPTS = 2
_RETRY_DELAY_SECONDS = 3


class ReportFetchError(Exception):
    pass


# ---------------------------------------------------------------------------
# Config I/O - ck only, cs is never persisted (see module docstring)
# ---------------------------------------------------------------------------

def default_report_fetch_config() -> dict:
    return {key: "" for key in SOURCES}


def load_report_fetch_config() -> dict:
    if not REPORT_FETCH_CONFIG_PATH.exists():
        return default_report_fetch_config()
    return {**default_report_fetch_config(), **json.loads(REPORT_FETCH_CONFIG_PATH.read_text())}


def save_report_fetch_config(config: dict) -> None:
    """Only ever writes ck values - a cs slipped into config by a caller is dropped."""
    safe = {key: config.get(key, "") for key in SOURCES}
    REPORT_FETCH_CONFIG_PATH.write_text(json.dumps(safe, indent=2))


def cs_env_var(source_key: str) -> str:
    return f"{source_key.upper()}_CS"


def read_cs(source_key: str, cli_value: str | None = None) -> str | None:
    if cli_value:
        return cli_value
    return os.environ.get(cs_env_var(source_key))


def validate_report_fetch_scope(config: dict, sources: list[str], cs_values: dict[str, str | None]) -> list[str]:
    """Returns human-readable problems for the sources actually in scope for
    this run; empty means OK to fetch."""
    errors = []
    for key in sources:
        if key not in SOURCES:
            errors.append(f"Unknown source: {key!r}")
            continue
        if not config.get(key):
            errors.append(f"{SOURCES[key]['label']}: ck is not set (Fetch Daily Reports page, or "
                           f"report_fetch_config.json).")
        if not cs_values.get(key):
            errors.append(f"{SOURCES[key]['label']}: cs is not set (type it in, or set the "
                           f"{cs_env_var(key)} environment variable).")
    return errors


# ---------------------------------------------------------------------------
# HTTP + zip extraction
# ---------------------------------------------------------------------------

def _basic_auth_header(ck: str, cs: str) -> str:
    token = base64.b64encode(f"{ck}:{cs}".encode()).decode()
    return f"Basic {token}"


def _http_get(url: str, ck: str, cs: str, timeout: int) -> bytes:
    # Cloudflare (fronting all three partners) blocks urllib's default
    # "Python-urllib/x.y" User-Agent outright (403, "error code: 1010") -
    # a plain browser-ish UA is enough to pass.
    req = Request(url, headers={
        "Authorization": _basic_auth_header(ck, cs),
        "User-Agent": "Mozilla/5.0 (compatible; KEDP-ReportFetch/1.0)",
    })
    last_error: Exception | None = None
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            with urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as resp:
                return resp.read()
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise ReportFetchError(f"HTTP {e.code} from {url}: {body[:300]}") from e
        except (URLError, TimeoutError) as e:
            # A read-phase (post-connect) timeout surfaces as a bare
            # TimeoutError, not wrapped in URLError - must be caught
            # separately or it escapes retry/error-isolation entirely and
            # crashes the whole batch instead of failing just this endpoint.
            last_error = e
            if attempt < _RETRY_ATTEMPTS:
                log.warning("connection error for %s (attempt %d/%d): %s - retrying in %ds",
                            url, attempt, _RETRY_ATTEMPTS, e, _RETRY_DELAY_SECONDS)
                time.sleep(_RETRY_DELAY_SECONDS)
    raise ReportFetchError(f"connection error for {url}: {last_error}") from last_error


def extract_zip_bytes(content: bytes, outdir: Path) -> list[Path]:
    """Flattens every entry to outdir/<basename> - deliberately ignores any
    directory structure inside the zip (zip-slip: an entry name like
    '../../evil' must not be able to write outside outdir)."""
    if content[:2] != b"PK":
        raise ReportFetchError(f"response is not a zip file: {content[:200]!r}")
    outdir.mkdir(parents=True, exist_ok=True)
    extracted = []
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            target = outdir / Path(name).name
            target.write_bytes(zf.read(name))
            extracted.append(target)
    return extracted


# ---------------------------------------------------------------------------
# Fetch orchestration
# ---------------------------------------------------------------------------

def report_url(source_key: str, product: str, date_str: str) -> str:
    source = SOURCES[source_key]
    if product not in source["products"]:
        raise ValueError(f"{source_key!r} has no {product!r} product")
    return f"{source['base_url']}/{product}/api/v1/getDailyTransactionReport?date={date_str}"


def fetch_one(source_key: str, product: str, date_str: str, ck: str, cs: str,
               outdir: Path, timeout: int = 30) -> dict:
    """Fetches and extracts a single source/product report. Never raises -
    failures are isolated per endpoint so one bad source doesn't abort the
    rest of the batch; caller inspects result['status']."""
    url = report_url(source_key, product, date_str)
    result = {"source": source_key, "product": product, "url": url, "files": []}
    try:
        content = _http_get(url, ck, cs, timeout)
        result["files"] = [str(p) for p in extract_zip_bytes(content, outdir)]
        result["status"] = "ok"
        log.info("%s/%s: saved %d file(s) to %s", source_key, product, len(result["files"]), outdir)
    except ReportFetchError as e:
        result["status"] = "error"
        result["error"] = str(e)
        log.error("%s/%s: %s", source_key, product, e)
    return result


def fetch_all(date_str: str, outdir: Path, config: dict, cs_values: dict[str, str | None],
              sources: list[str] | None = None, products: list[str] | None = None,
              timeout: int = 30) -> list[dict]:
    sources = sources or list(SOURCES.keys())
    results = []
    for source_key in sources:
        source = SOURCES[source_key]
        ck = config.get(source_key, "")
        cs = cs_values.get(source_key)
        source_products = [p for p in source["products"] if products is None or p in products]
        for product in source_products:
            if not ck or not cs:
                results.append({
                    "source": source_key, "product": product,
                    "url": report_url(source_key, product, date_str),
                    "files": [], "status": "error",
                    "error": f"missing credentials ({'ck' if not ck else 'cs'})",
                })
                continue
            results.append(fetch_one(source_key, product, date_str, ck, cs, outdir, timeout))
    return results


def summarize_results(results: list[dict]) -> str:
    ok = sum(1 for r in results if r["status"] == "ok")
    lines = [f"{ok}/{len(results)} endpoint(s) OK"]
    for r in results:
        label = SOURCES[r["source"]]["label"]
        if r["status"] == "ok":
            lines.append(f"  OK   {label}/{r['product']}: {len(r['files'])} file(s)")
        else:
            lines.append(f"  FAIL {label}/{r['product']}: {r['error']}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="Explicit report date, YYYY-MM-DD (overrides --days-ago)")
    parser.add_argument("--days-ago", type=int, default=1,
                         help="Report date is N days before today (default 1 = yesterday). "
                              "Computed at run time so a scheduled task always fetches the right day.")
    parser.add_argument("--outdir", type=Path, default=PROJECT_DIR / "rawData" / "katrina_daily_reports",
                         help="Folder to save extracted CSVs into")
    parser.add_argument("--config", type=Path, default=REPORT_FETCH_CONFIG_PATH)
    parser.add_argument("--source", action="append", choices=list(SOURCES.keys()),
                         help="Limit to this source (repeatable). Default: all.")
    parser.add_argument("--product", action="append", choices=["flight", "train", "hotel"],
                         help="Limit to this product (repeatable). Default: all.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    date_str = args.date or (date.today() - timedelta(days=args.days_ago)).isoformat()
    config = {**default_report_fetch_config(), **json.loads(args.config.read_text())} if args.config.exists() else default_report_fetch_config()
    sources = args.source or list(SOURCES.keys())
    cs_values = {key: read_cs(key) for key in sources}

    errors = validate_report_fetch_scope(config, sources, cs_values)
    if errors:
        for e in errors:
            log.error(e)
        raise SystemExit(1)

    log.info("fetching %s report(s) for %s into %s", ", ".join(sources), date_str, args.outdir)
    results = fetch_all(date_str, args.outdir, config, cs_values, sources=sources, products=args.product)
    log.info("\n%s", summarize_results(results))
    if any(r["status"] != "ok" for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
