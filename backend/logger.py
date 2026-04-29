"""Central logging utilities for VySol backend modules."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

_LOGGER_NAME = "vysol"
_LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
_MAX_LOG_FILES = 10
_WINDOWS_PATH_PATTERN = re.compile(r"[A-Za-z]:\\(?:[^\\/:*?\"<>|\r\n]+\\)*([^\\/:*?\"<>|\r\n]+)")


def get_logger(name: str) -> logging.Logger:
    """Return a module logger configured from the shared backend logger."""
    root_logger = _configure_root_logger()
    if name == _LOGGER_NAME:
        return root_logger
    return root_logger.getChild(name)


def _configure_root_logger() -> logging.Logger:
    logger = logging.getLogger(_LOGGER_NAME)
    if logger.handlers:
        return logger

    # BLOCK 1: Build the log directory inside the user area and rotate older numbered log files before opening a new one
    # VARS: logs_dir = folder that stores persistent terminal log copies, active_log_path = newest log file for the current run
    # WHY: The logging contract requires numbered files like logs_1.txt through logs_10.txt instead of one endlessly growing log file
    logs_dir = Path(__file__).resolve().parents[1] / "user" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    _rotate_log_files(logs_dir)
    active_log_path = logs_dir / "logs_1.txt"

    # BLOCK 2: Configure one shared logger that writes the same messages to both the terminal and the active log file
    # WHY: Central configuration keeps every module consistent and avoids per-file logging setup drift or accidental basicConfig calls
    configured_level = _resolve_log_level()
    logger.setLevel(configured_level)
    logger.propagate = False

    formatter = logging.Formatter(_LOG_FORMAT)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(configured_level)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(_LocalPathRedactionFilter())

    file_handler = logging.FileHandler(active_log_path, encoding="utf-8")
    file_handler.setLevel(configured_level)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(_LocalPathRedactionFilter())

    logger.addHandler(stream_handler)
    logger.addHandler(file_handler)
    logger.info("Logging initialized.")
    return logger


def _rotate_log_files(logs_dir: Path) -> None:
    # BLOCK 1: Delete the oldest numbered log if it exists so the folder never keeps more than ten log files
    # WHY: Removing the oldest file before shifting names down prevents the next rename step from colliding with an existing logs_10.txt
    oldest_log_path = logs_dir / f"logs_{_MAX_LOG_FILES}.txt"
    if oldest_log_path.exists():
        try:
            oldest_log_path.unlink()
        except PermissionError:
            return

    # BLOCK 2: Shift each existing log file up by one number so the next run can always take the logs_1.txt slot
    # WHY: Renaming in reverse order preserves every newer log; going forward would overwrite files before they could be moved
    for log_number in range(_MAX_LOG_FILES - 1, 0, -1):
        source_path = logs_dir / f"logs_{log_number}.txt"
        destination_path = logs_dir / f"logs_{log_number + 1}.txt"
        if source_path.exists():
            try:
                source_path.replace(destination_path)
            except PermissionError:
                continue


def _resolve_log_level() -> int:
    # BLOCK 1: Read the log level from the environment and default to INFO so DEBUG stays off unless deliberately enabled
    # WHY: DEBUG logging should stay out of normal runs by default, but an environment switch keeps deep diagnostics available when needed
    requested_level = os.getenv("VYSOL_LOG_LEVEL", "INFO").upper()
    return getattr(logging, requested_level, logging.INFO)


class _LocalPathRedactionFilter(logging.Filter):
    """Redact local Windows paths before log records reach terminal or file handlers."""

    def filter(self, record: logging.LogRecord) -> bool:
        # BLOCK 1: Render the log message once, replace full local paths, then clear raw args
        # WHY: Older modules may still pass paths as logger arguments, and users should be able to share logs without exposing local directories
        message = record.getMessage()
        record.msg = _WINDOWS_PATH_PATTERN.sub(lambda match: f"<local-path>\\{match.group(1)}", message)
        record.args = ()
        return True
