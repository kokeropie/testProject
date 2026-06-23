import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"
LOG_PATH = Path(__file__).parent / "pipeline.log"


class ConfigError(Exception):
    pass


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise ConfigError(f"config.json not found at {CONFIG_PATH}")
    with open(CONFIG_PATH) as f:
        return json.load(f)


def check_dir_writable(path: Path, label: str) -> None:
    if not path.exists():
        raise ConfigError(
            f"{label} does not exist: {path}\n"
            "Create it or update config.json before running."
        )
    test_file = path / ".write_test"
    try:
        test_file.touch()
        test_file.unlink()
    except OSError:
        raise ConfigError(f"No write permission on {label}: {path}")


def setup_logging(cfg: dict, source: str) -> logging.Logger:
    log_cfg = cfg.get("log", {})
    formatter = logging.Formatter(
        f"%(asctime)s %(levelname)-5s [{source}] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        LOG_PATH,
        maxBytes=log_cfg.get("max_bytes", 5_242_880),
        backupCount=log_cfg.get("backup_count", 3),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    logger = logging.getLogger(source.lower())
    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger
