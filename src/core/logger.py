"""Centralized logging setup shared by every pipeline."""

import logging

from config.settings import LOGS_DIR


def configure_logging(log_file=None, level=logging.INFO):
    """Configure the root logger once: console output, plus a file under
    logs/ when log_file (a filename, not a path) is given."""
    handlers = [logging.StreamHandler()]

    if log_file:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(LOGS_DIR / log_file, encoding="utf-8"))

    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=handlers,
        force=True,
    )


def get_logger(name):
    return logging.getLogger(name)
