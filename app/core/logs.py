"""Terminal logging. There are no log files: everything goes to stdout, so `journalctl -u nazar-ops -f`,
`docker logs` or the terminal you started it from shows the full picture."""
import logging
import sys

from . import config

_COL = {"DEBUG": "\033[2m", "INFO": "\033[32m", "WARNING": "\033[33m", "ERROR": "\033[31m", "CRITICAL": "\033[1;31m"}
_TTY = sys.stdout.isatty()


class _Fmt(logging.Formatter):
    def format(self, r: logging.LogRecord) -> str:
        name = r.name.replace("ops.", "", 1)
        lvl = f"{_COL.get(r.levelname, '')}{r.levelname[:4]:4}\033[0m" if _TTY else f"{r.levelname[:4]:4}"
        msg = f"{self.formatTime(r, '%H:%M:%S')} {lvl} {name:<8} {r.getMessage()}"
        if r.exc_info:
            msg += "\n" + self.formatException(r.exc_info)
        return msg if _TTY else msg.replace("\033[0m", "")


def setup() -> None:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(_Fmt())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
    for n in ("uvicorn", "uvicorn.error", "uvicorn.access"):       # let uvicorn use the same handler and format
        lg = logging.getLogger(n)
        lg.handlers[:] = []
        lg.propagate = True
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
    for n in ("sqlalchemy.engine", "watchfiles", "httpx", "urllib3"):
        logging.getLogger(n).setLevel(logging.WARNING)


def banner(lines: list) -> None:
    """A boxed message that is hard to miss (first-run password, URLs)."""
    w = max(len(l) for l in lines) + 4
    print("\n" + "┌" + "─" * w + "┐", flush=True)
    for l in lines:
        print("│  " + l.ljust(w - 2) + "│", flush=True)
    print("└" + "─" * w + "┘\n", flush=True)
