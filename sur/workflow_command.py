import logging
from typing import Optional, Literal


def escape(s: str) -> str:
    return s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def set_output(name: str, value: str) -> str:
    print(f"::set-output name={name}::{escape(value)}")


def log_debug(message: str) -> None:
    print(f"::debug::{escape(message)}")


def log_message(
    message_type: Literal["notice", "warning", "error"],
    message: str,
    title: str,
    file: Optional[str] = None,
    line: Optional[int] = None,
    end_line: Optional[int] = None,
    col: Optional[int] = None,
    end_column: Optional[int] = None,
) -> None:
    pairs = [
        ("file", file),
        ("line", line),
        ("endLine", end_line),
        ("col", col),
        ("endColumn", end_column),
        ("title", title),
    ]
    values = ",".join(f"{k}={escape(str(v))}" for k, v in pairs if v)
    print(f"::{message_type} {values}::{escape(message)}")


def log_notice(
    message: str,
    title: str,
    file: Optional[str] = None,
    line: Optional[int] = None,
    end_line: Optional[int] = None,
    col: Optional[int] = None,
    end_column: Optional[int] = None,
) -> None:
    log_message(
        "notice", message, title, file, line, end_line, col, end_column
    )


def log_warning(
    message: str,
    title: str,
    file: Optional[str] = None,
    line: Optional[int] = None,
    end_line: Optional[int] = None,
    col: Optional[int] = None,
    end_column: Optional[int] = None,
) -> None:
    log_message(
        "warning", message, title, file, line, end_line, col, end_column
    )


def log_error(
    message: str,
    title: str,
    file: Optional[str] = None,
    line: Optional[int] = None,
    end_line: Optional[int] = None,
    col: Optional[int] = None,
    end_column: Optional[int] = None,
) -> None:
    log_message("error", message, title, file, line, end_line, col, end_column)


class WorkflowHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.ERROR:
            log_error(record.msg, record.name, record.pathname, record.lineno)
        elif record.levelno >= logging.WARNING:
            log_warning(
                record.msg, record.name, record.pathname, record.lineno
            )
        elif record.levelno >= logging.INFO:
            log_notice(record.msg, record.name, record.pathname, record.lineno)
        else:
            log_debug(record.msg)
