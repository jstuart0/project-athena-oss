"""Structured logging configuration for Project Athena"""

import os
import sys
import logging
import structlog
from typing import Optional
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


# Global debug file handler for persistent logging
_debug_file_handler = None
_debug_log_path = None


def get_debug_log_path() -> Optional[str]:
    """Get the current debug log file path"""
    return _debug_log_path


def configure_logging(service_name: str, level: Optional[str] = None):
    """Configure structured logging for a service

    Args:
        service_name: Name of the service (e.g., "gateway", "orchestrator")
        level: Log level (default: INFO, from LOG_LEVEL env var)
    """
    global _debug_file_handler, _debug_log_path

    log_level = level or os.getenv("LOG_LEVEL", "INFO").upper()
    log_format = os.getenv("LOG_FORMAT", "json")  # json or console
    debug_mode = os.getenv("ATHENA_DEBUG_MODE", "false").lower() == "true"

    # Debug logging directory
    log_dir = os.getenv("ATHENA_LOG_DIR", os.path.expanduser("~/dev/project-athena/logs/debug"))

    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    # Set up output targets
    output_files = [sys.stdout]

    # Add persistent file logging if debug mode is enabled
    if debug_mode:
        try:
            Path(log_dir).mkdir(parents=True, exist_ok=True)

            # Create date-stamped log file
            date_str = datetime.now().strftime("%Y-%m-%d")
            log_file = Path(log_dir) / f"{service_name}_{date_str}.log"
            _debug_log_path = str(log_file)

            # Use TimedRotatingFileHandler to keep logs organized by day
            _debug_file_handler = TimedRotatingFileHandler(
                log_file,
                when="midnight",
                interval=1,
                backupCount=30,  # Keep 30 days of logs
                encoding="utf-8"
            )
            _debug_file_handler.setLevel(logging.DEBUG)

            # Create a multi-output logger factory
            output_files.append(open(log_file, "a", encoding="utf-8"))

            print(f"[DEBUG MODE] Persistent logging enabled: {log_file}", file=sys.stderr)
        except Exception as e:
            print(f"[WARNING] Failed to set up debug logging: {e}", file=sys.stderr)

    if log_format == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    # Create a custom logger factory that writes to multiple outputs
    class MultiOutputLoggerFactory:
        def __init__(self, outputs):
            self.outputs = outputs

        def __call__(self, *args):
            return MultiOutputLogger(self.outputs)

    class MultiOutputLogger:
        def __init__(self, outputs):
            self.outputs = outputs

        def msg(self, message):
            for output in self.outputs:
                try:
                    print(message, file=output, flush=True)
                except:
                    pass

        # Aliases for different log levels
        debug = info = warning = error = critical = exception = msg

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, log_level, logging.INFO)
        ),
        context_class=dict,
        logger_factory=MultiOutputLoggerFactory(output_files) if debug_mode else structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Add service name to all logs
    structlog.contextvars.bind_contextvars(service=service_name)

    return structlog.get_logger()


# Backward compatibility alias
setup_logging = configure_logging


def get_logger(name: Optional[str] = None):
    """Get a logger instance
    
    Args:
        name: Optional logger name (defaults to calling module)
    """
    return structlog.get_logger(name)
