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
gitignored) holds each source's ck. cs is resolved at run time, in order:
an explicit value (typed into the Streamlit form for that run) > a
per-source environment variable ("<SOURCE>_CS", e.g. KATRINA_CS) > a line
in report_fetch_secrets.txt (gitignored, "<SOURCE>_CS=value" per line,
same names as the env vars) - see load_cs_secrets_file(). The secrets file
exists for CLI/scheduled-run convenience so cs doesn't have to be typed
every run or set as a system env var; it is plaintext on disk, so treat it
like any other local secrets file (don't widen its permissions, don't
commit it - already gitignored).

cobt.id (no "www") 301-redirects to www.cobt.id and the redirect drops the
Authorization header, so cobt_mt's base_url below points straight at
www.cobt.id to avoid that hop entirely.

Usage (CLI):
    python report_fetch.py --outdir rawData/katrina_daily_reports
    python report_fetch.py --date 2026-07-13 --source katrina --outdir DIR
    python report_fetch.py --secrets report_fetch_secrets.txt --outdir DIR
    python report_fetch.py --outdir DIR --max-retries 10 --retry-delay 120

Any endpoint still failing after its own connect/read retries is not given
up on immediately - fetch_all_until_complete() re-fetches just the failing
endpoints, waiting --retry-delay seconds between passes, up to --max-retries
times (default 5 retries / 60s apart) before finally giving up. Endpoints
failing due to a missing ck/cs are excluded from this - retrying those is
pointless.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import os
import shutil
import ssl
import sys
import time
import zipfile
from datetime import date, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import certifi
import openpyxl

PROJECT_DIR = Path(__file__).parent
REPORT_FETCH_CONFIG_PATH = PROJECT_DIR / "report_fetch_config.json"
REPORT_FETCH_SECRETS_PATH = PROJECT_DIR / "report_fetch_secrets.txt"
FILENAME_REPLACE_MAP_PATH = PROJECT_DIR / "dataFilter" / "fileNameReplace.xlsx"
REPORT_FETCH_LOG_PATH = PROJECT_DIR / "report_fetch.log"
DEFAULT_KEEP_ORIGINALS = 7

# Explicit CA bundle rather than trusting the OS/venv Python to have one
# wired up correctly - python.org macOS builds need "Install Certificates
# .command" run manually or every HTTPS call fails with
# CERTIFICATE_VERIFY_FAILED, and a fresh Windows Python install can hit the
# same thing. certifi ships its own bundle so this works out of the box.
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

log = logging.getLogger("report_fetch")


def setup_logging(log_file: Path | None = REPORT_FETCH_LOG_PATH) -> None:
    """Console + rotating file logging, so a headless scheduled run (no
    console attached, nothing captured by Task Scheduler by default) still
    leaves a record of what happened. Mirrors utils.py's setup_logging()
    for the KEDP ingest pipeline, but self-contained - this pipeline shares
    nothing with that one but Python + this checkout."""
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file is not None:
        handlers.append(RotatingFileHandler(log_file, maxBytes=5_242_880, backupCount=3, encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=handlers)

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

# Batch-level retry (fetch_all_until_complete): distinct from _RETRY_ATTEMPTS
# above, which only covers transient connect/read failures within a single
# HTTP call. This layer re-fetches endpoints that came back "error" after
# exhausting that per-call retry too - e.g. a partner API that's simply slow
# to generate a report for a few minutes (COBT MT PROD flight/train have
# done this) rather than genuinely down.
DEFAULT_MAX_RETRIES = 5
DEFAULT_RETRY_DELAY_SECONDS = 60


class ReportFetchError(Exception):
    pass


# ---------------------------------------------------------------------------
# Config I/O - ck lives in report_fetch_config.json; cs lives in either the
# environment or report_fetch_secrets.txt (see module docstring for the
# resolution order)
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


def load_cs_secrets_file(path: Path = REPORT_FETCH_SECRETS_PATH) -> dict[str, str]:
    """Parses a simple '<SOURCE>_CS=value' per line file (blank lines and
    '#' comments ignored) into {env-var-name: value}."""
    if not path.exists():
        return {}
    secrets = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        secrets[key.strip()] = value.strip()
    return secrets


def save_cs_secrets(cs_values: dict[str, str], path: Path = REPORT_FETCH_SECRETS_PATH) -> None:
    """Writes '<SOURCE>_CS=value' lines for every non-empty value given -
    plaintext secrets on disk, so the file is chmod'd owner-only (best
    effort; a no-op on filesystems that don't support POSIX permissions)."""
    lines = [f"{cs_env_var(key)}={value}" for key, value in cs_values.items() if value]
    path.write_text("\n".join(lines) + ("\n" if lines else ""))
    try:
        path.chmod(0o600)
    except OSError:
        pass


def read_cs(source_key: str, cli_value: str | None = None,
            secrets_path: Path = REPORT_FETCH_SECRETS_PATH) -> str | None:
    if cli_value:
        return cli_value
    env_value = os.environ.get(cs_env_var(source_key))
    if env_value:
        return env_value
    return load_cs_secrets_file(secrets_path).get(cs_env_var(source_key))


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
            errors.append(f"{SOURCES[key]['label']}: cs is not set (type it in, set the "
                           f"{cs_env_var(key)} environment variable, or add it to "
                           f"{REPORT_FETCH_SECRETS_PATH.name}).")
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
# Filename replacement + retention - dataFilter/fileNameReplace.xlsx maps a
# downloaded file's name prefix (originalFileName, e.g. "KATRINA_HOTEL_
# Branch") to a fixed name to also save it as (replacedFileName, e.g.
# "katrinaHTLBrc"). The renamed copy always overwrites the previous one (one
# per mapping), while the dated originals accumulate - see
# prune_old_originals() for the retention side. Applies identically whether
# report_fetch.py was run from the CLI or the Fetch Daily Reports page, so
# both it and its schedule (report_fetch_scheduler.py) get the same
# behavior for free.
# ---------------------------------------------------------------------------

def load_filename_replace_map(path: Path = FILENAME_REPLACE_MAP_PATH) -> dict[str, str]:
    """Reads the two-column (originalFileName, replacedFileName) sheet into
    {originalFileName: replacedFileName}. Missing file (dataFilter/ is
    gitignored, like the rest of that folder) means no mapping is
    configured yet - copy/rename becomes a no-op rather than an error."""
    if not path.exists():
        return {}
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    ws = wb.worksheets[0]
    rows = ws.iter_rows(values_only=True)
    next(rows, None)  # header: originalFileName, replacedFileName
    mapping = {}
    for row in rows:
        if not row or not row[0] or len(row) < 2 or not row[1]:
            continue
        mapping[str(row[0]).strip()] = str(row[1]).strip()
    return mapping


def _match_original_pattern(filename_stem: str, mapping: dict[str, str]) -> str | None:
    """Longest matching originalFileName prefix - guards against a shorter
    pattern shadowing a more specific one if the mapping ever grows entries
    that share a prefix."""
    candidates = [pattern for pattern in mapping if filename_stem.startswith(pattern)]
    return max(candidates, key=len) if candidates else None


def apply_filename_replacements(files: list[Path], outdir: Path,
                                 mapping: dict[str, str] | None = None) -> list[Path]:
    """Copies each downloaded file that matches an originalFileName prefix
    to outdir/<replacedFileName><ext>, overwriting whatever was there from
    the previous fetch - so there's always exactly one renamed copy per
    mapping, regardless of how many dated originals have piled up."""
    mapping = load_filename_replace_map() if mapping is None else mapping
    if not mapping:
        return []
    renamed = []
    for f in files:
        pattern = _match_original_pattern(f.stem, mapping)
        if pattern is None:
            continue
        target = outdir / f"{mapping[pattern]}{f.suffix}"
        shutil.copyfile(f, target)
        renamed.append(target)
    return renamed


def prune_old_originals(outdir: Path, mapping: dict[str, str] | None = None,
                         keep: int = DEFAULT_KEEP_ORIGINALS) -> list[Path]:
    """Per originalFileName pattern, keeps only the `keep` most-recently-
    modified downloaded files in outdir and deletes the rest. The renamed
    copies (apply_filename_replacements) never match a pattern themselves
    (their names are the replacedFileName, not the original), so they're
    untouched by this."""
    mapping = load_filename_replace_map() if mapping is None else mapping
    if not mapping or not outdir.exists():
        return []
    removed = []
    for pattern in mapping:
        matches = sorted(
            (p for p in outdir.iterdir() if p.is_file() and p.stem.startswith(pattern)),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        for stale in matches[keep:]:
            stale.unlink()
            removed.append(stale)
    return removed


def postprocess_downloads(outdir: Path, results: list[dict],
                           keep: int = DEFAULT_KEEP_ORIGINALS) -> dict:
    """Runs after a fetch batch completes: renames/overwrites the fixed
    copies, then prunes originals down to `keep` per pattern. Returns
    {"renamed": [...], "pruned": [...], "mapping_found": bool} -
    mapping_found is False (and renamed/pruned always empty) when
    dataFilter/fileNameReplace.xlsx doesn't exist on this machine - that
    folder is gitignored, so the mapping has to be copied there manually,
    separately from a git pull/push."""
    mapping = load_filename_replace_map()
    downloaded = [Path(f) for r in results for f in r.get("files", [])]
    renamed = apply_filename_replacements(downloaded, outdir, mapping)
    pruned = prune_old_originals(outdir, mapping, keep)
    return {"renamed": renamed, "pruned": pruned, "mapping_found": bool(mapping)}


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


def fetch_all_until_complete(date_str: str, outdir: Path, config: dict, cs_values: dict[str, str | None],
                              sources: list[str] | None = None, products: list[str] | None = None,
                              timeout: int = 30, max_retries: int = DEFAULT_MAX_RETRIES,
                              retry_delay: int = DEFAULT_RETRY_DELAY_SECONDS) -> list[dict]:
    """Like fetch_all, but re-fetches only the endpoints still failing after
    each pass (up to max_retries more passes, retry_delay seconds apart)
    until every endpoint is OK or the retry budget runs out. Endpoints
    failing because of a missing ck/cs are never retried - no amount of
    waiting fixes a blank credential."""
    results = fetch_all(date_str, outdir, config, cs_values, sources=sources, products=products, timeout=timeout)
    for attempt in range(1, max_retries + 1):
        pending = [r for r in results if r["status"] != "ok" and "missing credentials" not in r.get("error", "")]
        if not pending:
            break
        log.warning(
            "%d endpoint(s) still failing (retry %d/%d, waiting %ds): %s",
            len(pending), attempt, max_retries, retry_delay,
            ", ".join(f"{SOURCES[r['source']]['label']}/{r['product']}" for r in pending),
        )
        time.sleep(retry_delay)
        retried = {
            (r["source"], r["product"]): fetch_one(
                r["source"], r["product"], date_str, config.get(r["source"], ""),
                cs_values.get(r["source"]), outdir, timeout,
            )
            for r in pending
        }
        results = [retried.get((r["source"], r["product"]), r) for r in results]
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
    parser.add_argument("--secrets", type=Path, default=REPORT_FETCH_SECRETS_PATH,
                         help="cs fallback file (env vars still take priority over this)")
    parser.add_argument("--source", action="append", choices=list(SOURCES.keys()),
                         help="Limit to this source (repeatable). Default: all.")
    parser.add_argument("--product", action="append", choices=["flight", "train", "hotel"],
                         help="Limit to this product (repeatable). Default: all.")
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES,
                         help=f"Re-fetch still-failing endpoints up to this many more times "
                              f"(default {DEFAULT_MAX_RETRIES}). Missing-credential failures "
                              f"are never retried. 0 disables batch-level retry.")
    parser.add_argument("--retry-delay", type=int, default=DEFAULT_RETRY_DELAY_SECONDS,
                         help=f"Seconds to wait between retry passes (default {DEFAULT_RETRY_DELAY_SECONDS}).")
    parser.add_argument("--log-file", type=Path, default=REPORT_FETCH_LOG_PATH,
                         help=f"Rotating log file (default {REPORT_FETCH_LOG_PATH}). Pass an empty "
                              f"string to log to console only.")
    args = parser.parse_args()

    setup_logging(args.log_file if str(args.log_file) else None)

    date_str = args.date or (date.today() - timedelta(days=args.days_ago)).isoformat()
    config = {**default_report_fetch_config(), **json.loads(args.config.read_text())} if args.config.exists() else default_report_fetch_config()
    sources = args.source or list(SOURCES.keys())
    cs_values = {key: read_cs(key, secrets_path=args.secrets) for key in sources}

    errors = validate_report_fetch_scope(config, sources, cs_values)
    if errors:
        for e in errors:
            log.error(e)
        raise SystemExit(1)

    log.info("fetching %s report(s) for %s into %s", ", ".join(sources), date_str, args.outdir)
    results = fetch_all_until_complete(date_str, args.outdir, config, cs_values, sources=sources,
                                        products=args.product, max_retries=args.max_retries,
                                        retry_delay=args.retry_delay)
    log.info("\n%s", summarize_results(results))

    post = postprocess_downloads(args.outdir, results)
    if post["mapping_found"]:
        log.info("renamed %d file(s), pruned %d stale original(s)", len(post["renamed"]), len(post["pruned"]))
    else:
        log.warning("%s not found - no files renamed (dataFilter/ is gitignored, copy it here manually)",
                    FILENAME_REPLACE_MAP_PATH)

    if any(r["status"] != "ok" for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        log.exception("unhandled error")
        raise
