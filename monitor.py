#!/usr/bin/env python3
"""Monitorea cambios relevantes en la convocatoria PIL 2026 de SECIHTI."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import logging
import math
import os
import re
import signal
import sys
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Comment
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows no incluye fcntl.
    fcntl = None  # type: ignore[assignment]


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_URL = (
    "https://www.secihti.mx/convocatoria/ciencias-y-humanidades/"
    "programa-de-insercion-laboral-pil/"
    "convocatoria-del-programa-de-insercion-laboral-2026/"
)
DEFAULT_SELECTOR = "div.elementor-location-single.post-58580"
STATE_SCHEMA_VERSION = 2
MAX_TELEGRAM_MESSAGE_LENGTH = 4096
TELEGRAM_MESSAGE_TARGET = 3900
MAX_PAGE_BYTES = 5_000_000
IGNORED_TAGS = ("script", "style", "noscript", "template", "svg", "canvas", "form")
SPACE_RE = re.compile(r"\s+")
SPACE_BEFORE_PUNCTUATION_RE = re.compile(r"\s+([,.;:!?])")
LOGGER = logging.getLogger("pil_monitor")


class MonitorError(Exception):
    """Error esperado y seguro para mostrar en los logs."""


class ConfigurationError(MonitorError):
    """La configuración no es válida."""


class PageValidationError(MonitorError):
    """La respuesta no parece ser la convocatoria esperada."""


class StateError(MonitorError):
    """El archivo de estado no se pudo leer o escribir."""


class TelegramError(MonitorError):
    """Telegram no confirmó la entrega del mensaje."""


class AlreadyRunningError(MonitorError):
    """Ya existe otra instancia usando el mismo estado."""


@dataclass(frozen=True, slots=True)
class Settings:
    target_url: str
    telegram_bot_token: str
    telegram_chat_id: str
    interval_seconds: float
    connect_timeout: float
    read_timeout: float
    request_retries: int
    css_selector: str
    expected_text: str
    state_file: Path
    failure_alert_threshold: int
    log_level: str

    @classmethod
    def from_env(cls, *, require_telegram: bool = True) -> "Settings":
        load_dotenv(BASE_DIR / ".env")

        target_url = os.getenv("TARGET_URL", DEFAULT_URL).strip()
        parsed_url = urlparse(target_url)
        if (
            parsed_url.scheme != "https"
            or not parsed_url.netloc
            or parsed_url.username is not None
            or parsed_url.password is not None
        ):
            raise ConfigurationError(
                "TARGET_URL debe ser una URL HTTPS válida y sin credenciales."
            )

        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if require_telegram and (not token or not chat_id):
            raise ConfigurationError(
                "Faltan TELEGRAM_BOT_TOKEN y/o TELEGRAM_CHAT_ID en el archivo .env."
            )

        raw_state_file = os.getenv("STATE_FILE", ".state/monitor_state.json").strip()
        if not raw_state_file:
            raise ConfigurationError("STATE_FILE no puede estar vacío.")
        state_file = Path(raw_state_file).expanduser()
        if not state_file.is_absolute():
            state_file = BASE_DIR / state_file

        css_selector = os.getenv("CSS_SELECTOR", DEFAULT_SELECTOR).strip()
        if not css_selector:
            raise ConfigurationError("CSS_SELECTOR no puede estar vacío.")

        expected_text = os.getenv("EXPECTED_TEXT", "Inserción Laboral 2026").strip()
        if not expected_text:
            raise ConfigurationError("EXPECTED_TEXT no puede estar vacío.")

        interval_minutes = _positive_float("CHECK_INTERVAL_MINUTES", 30.0)
        connect_timeout = _positive_float("REQUEST_CONNECT_TIMEOUT_SECONDS", 10.0)
        read_timeout = _positive_float("REQUEST_READ_TIMEOUT_SECONDS", 30.0)
        request_retries = _nonnegative_int("REQUEST_RETRIES", 4)
        failure_threshold = _positive_int("FAILURE_ALERT_THRESHOLD", 3)
        log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
        if log_level not in logging.getLevelNamesMapping():
            raise ConfigurationError(f"LOG_LEVEL no es válido: {log_level!r}.")

        return cls(
            target_url=target_url,
            telegram_bot_token=token,
            telegram_chat_id=chat_id,
            interval_seconds=interval_minutes * 60,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            request_retries=request_retries,
            css_selector=css_selector,
            expected_text=expected_text,
            state_file=state_file,
            failure_alert_threshold=failure_threshold,
            log_level=log_level,
        )

    @property
    def source_key(self) -> str:
        source = "\0".join((self.target_url, self.css_selector, self.expected_text))
        return hashlib.sha256(source.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Snapshot:
    fingerprint: str
    lines: tuple[str, ...]
    fetched_at: str


def _positive_float(name: str, default: float) -> float:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} debe ser un número.") from exc
    if not math.isfinite(value) or value <= 0:
        raise ConfigurationError(f"{name} debe ser un número finito mayor que cero.")
    return value


def _nonnegative_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} debe ser un entero.") from exc
    if value < 0:
        raise ConfigurationError(f"{name} no puede ser negativo.")
    return value


def _positive_int(name: str, default: int) -> int:
    value = _nonnegative_int(name, default)
    if value == 0:
        raise ConfigurationError(f"{name} debe ser mayor que cero.")
    return value


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def local_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value).replace("\xa0", " ")
    normalized = SPACE_RE.sub(" ", normalized).strip()
    return SPACE_BEFORE_PUNCTUATION_RE.sub(r"\1", normalized)


def build_http_session(retries: int) -> requests.Session:
    retry_policy = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry_policy)
    session = requests.Session()
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": "PIL-SECIHTI-Change-Monitor/1.0",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "es-MX,es;q=0.9",
        }
    )
    return session


def fetch_snapshot(settings: Settings, session: requests.Session) -> Snapshot:
    response: requests.Response | None = None
    try:
        response = session.get(
            settings.target_url,
            timeout=(settings.connect_timeout, settings.read_timeout),
            stream=True,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        if response is not None:
            response.close()
        raise MonitorError(
            f"No fue posible descargar la convocatoria ({type(exc).__name__})."
        ) from exc

    try:
        content_type = response.headers.get("Content-Type", "").lower()
        if (
            "text/html" not in content_type
            and "application/xhtml+xml" not in content_type
        ):
            raise PageValidationError(
                f"La respuesta no es HTML (Content-Type: {content_type or 'ausente'})."
            )

        raw_content_length = response.headers.get("Content-Length", "").strip()
        if raw_content_length.isdigit() and int(raw_content_length) > MAX_PAGE_BYTES:
            raise PageValidationError(
                f"La página excede el límite de {MAX_PAGE_BYTES:,} bytes."
            )

        body = bytearray()
        try:
            for chunk in response.iter_content(chunk_size=65_536):
                if not chunk:
                    continue
                body.extend(chunk)
                if len(body) > MAX_PAGE_BYTES:
                    raise PageValidationError(
                        f"La página excede el límite de {MAX_PAGE_BYTES:,} bytes."
                    )
        except requests.RequestException as exc:
            raise MonitorError(
                f"La descarga de la convocatoria quedó incompleta ({type(exc).__name__})."
            ) from exc
        if not body:
            raise PageValidationError("La respuesta HTML llegó vacía.")

        encoding = response.encoding or "utf-8"
        try:
            html = bytes(body).decode(encoding, errors="replace")
        except LookupError as exc:
            raise PageValidationError(
                f"La página indicó una codificación desconocida: {encoding!r}."
            ) from exc
        final_url = response.url or settings.target_url
    finally:
        response.close()

    return snapshot_from_html(
        html,
        page_url=final_url,
        selector=settings.css_selector,
        expected_text=settings.expected_text,
    )


def snapshot_from_html(
    html: str,
    *,
    page_url: str,
    selector: str,
    expected_text: str,
) -> Snapshot:
    """Convierte el HTML en texto y recursos estables para evitar falsos positivos."""

    soup = BeautifulSoup(html, "html.parser")
    try:
        root = soup.select_one(selector)
    except Exception as exc:
        raise PageValidationError(f"CSS_SELECTOR no es válido: {selector!r}.") from exc
    if root is None:
        raise PageValidationError(
            "No se encontró el bloque principal de la convocatoria; "
            "no se modificará la línea base."
        )

    for comment in root.find_all(string=lambda item: isinstance(item, Comment)):
        comment.extract()
    for unwanted in root.find_all(IGNORED_TAGS):
        unwanted.decompose()

    text_lines = tuple(
        line
        for raw_line in root.stripped_strings
        if (line := normalize_text(str(raw_line)))
    )
    # Esta representación global hace que añadir/quitar etiquetas inline como
    # <strong> o <em> no cambie la huella si el texto visible sigue idéntico.
    canonical_text = normalize_text(root.get_text(" ", strip=True))
    searchable_text = canonical_text.casefold()
    if expected_text.casefold() not in searchable_text:
        raise PageValidationError(
            f"La página no contiene el texto esperado: {expected_text!r}."
        )
    if len(text_lines) < 5:
        raise PageValidationError(
            "El bloque encontrado tiene muy poco contenido y podría ser una página de error."
        )

    resource_lines: list[str] = []
    resource_attributes = {
        "a": ("href",),
        "iframe": ("src",),
        "embed": ("src",),
        "object": ("data",),
        "img": ("src", "data-src", "data-lazy-src"),
    }
    for element in root.find_all(tuple(resource_attributes)):
        raw_url = ""
        for attribute in resource_attributes[element.name]:
            raw_url = normalize_text(str(element.get(attribute, "")))
            if raw_url:
                break
        if not raw_url or raw_url.lower().startswith(("data:", "javascript:")):
            continue
        absolute_url = urljoin(page_url, raw_url)
        label = normalize_text(element.get_text(" ", strip=True))
        if not label:
            label = normalize_text(
                str(element.get("alt", "") or element.get("title", ""))
            )
        if not label:
            label = "sin etiqueta"
        resource_lines.append(
            f"RECURSO {element.name.upper()}: {label} -> {absolute_url}"
        )

    lines = ("CONTENIDO:", *text_lines, "RECURSOS:", *resource_lines)
    canonical = json.dumps(
        {"text": canonical_text, "resources": resource_lines},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return Snapshot(fingerprint=fingerprint, lines=lines, fetched_at=utc_now())


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StateError(
            f"No se pudo leer {path}; corrige o respalda el archivo antes de continuar."
        ) from exc
    if not isinstance(data, dict):
        raise StateError(f"El estado en {path} no es un objeto JSON.")
    _validate_loaded_state(data, path)
    return data


def _validate_loaded_state(state: dict[str, Any], path: Path) -> None:
    def invalid(detail: str) -> StateError:
        return StateError(f"El estado en {path} no es válido: {detail}.")

    for field in ("fingerprint", "source_key", "target_url", "css_selector"):
        if field in state and state[field] is not None and not isinstance(state[field], str):
            raise invalid(f"{field} debe ser texto")

    schema_version = state.get("schema_version")
    if schema_version is not None and (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version < 1
    ):
        raise invalid("schema_version debe ser un entero positivo")

    lines = state.get("snapshot_lines")
    if lines is not None and (
        not isinstance(lines, list) or not all(isinstance(line, str) for line in lines)
    ):
        raise invalid("snapshot_lines debe ser una lista de textos")

    failures = state.get("consecutive_failures")
    if failures is not None and (
        isinstance(failures, bool)
        or not isinstance(failures, int)
        or failures < 0
    ):
        raise invalid("consecutive_failures debe ser un entero no negativo")

    alert_sent = state.get("failure_alert_sent")
    if alert_sent is not None and not isinstance(alert_sent, bool):
        raise invalid("failure_alert_sent debe ser booleano")

    if "pending_events" not in state:
        return
    pending_events = state["pending_events"]
    if not isinstance(pending_events, list):
        raise invalid("pending_events debe ser una lista")
    required_text_fields = (
        "event_id",
        "old_fingerprint",
        "new_fingerprint",
        "target_url",
        "detected_at",
    )
    for index, event in enumerate(pending_events):
        if not isinstance(event, dict):
            raise invalid(f"pending_events[{index}] debe ser un objeto")
        if any(not isinstance(event.get(field), str) for field in required_text_fields):
            raise invalid(f"pending_events[{index}] tiene campos de texto inválidos")
        for field in ("old_lines", "new_lines"):
            values = event.get(field)
            if not isinstance(values, list) or not all(
                isinstance(value, str) for value in values
            ):
                raise invalid(f"pending_events[{index}].{field} no es válido")


def save_state(path: Path, state: dict[str, Any]) -> None:
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_path, path)
    except OSError as exc:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise StateError(f"No se pudo guardar el estado en {path}.") from exc


@contextmanager
def single_instance_lock(state_file: Path) -> Iterator[None]:
    lock_path = state_file.with_suffix(f"{state_file.suffix}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AlreadyRunningError(
                    "Ya hay otra instancia del monitor usando este STATE_FILE."
                ) from exc
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def send_telegram(settings: Settings, message: str) -> None:
    if len(message) > MAX_TELEGRAM_MESSAGE_LENGTH:
        raise TelegramError("El mensaje generado excede el límite de Telegram.")

    endpoint = (
        "https://api.telegram.org/bot"
        f"{settings.telegram_bot_token}/sendMessage"
    )
    payload = {
        "chat_id": settings.telegram_chat_id,
        "text": message,
        "disable_web_page_preview": False,
    }
    try:
        response = requests.post(
            endpoint,
            json=payload,
            timeout=(settings.connect_timeout, settings.read_timeout),
        )
    except requests.RequestException as exc:
        # No se incluye str(exc): algunas excepciones contienen la URL y el token.
        raise TelegramError(
            f"No fue posible contactar Telegram ({type(exc).__name__})."
        ) from exc

    try:
        result = response.json()
    except ValueError as exc:
        raise TelegramError(
            f"Telegram devolvió HTTP {response.status_code} sin JSON válido."
        ) from exc

    if not isinstance(result, dict):
        raise TelegramError(
            f"Telegram devolvió HTTP {response.status_code} con un JSON inesperado."
        )
    if not response.ok or not result.get("ok"):
        description = normalize_text(str(result.get("description", "error desconocido")))
        raise TelegramError(
            f"Telegram rechazó el mensaje (HTTP {response.status_code}): {description}."
        )


def _collect_line_changes(
    old_lines: Sequence[str], new_lines: Sequence[str]
) -> tuple[list[str], list[str]]:
    added: list[str] = []
    removed: list[str] = []
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for operation, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if operation in {"insert", "replace"}:
            added.extend(new_lines[new_start:new_end])
        if operation in {"delete", "replace"}:
            removed.extend(old_lines[old_start:old_end])
    return added, removed


def _short_line(line: str, limit: int = 280) -> str:
    return line if len(line) <= limit else f"{line[: limit - 1]}…"


def format_change_message(
    *,
    old_lines: Sequence[str],
    new_lines: Sequence[str],
    target_url: str,
    detected_at: str,
    event_id: str | None = None,
) -> str:
    added, removed = _collect_line_changes(old_lines, new_lines)
    searchable_added = " ".join(added).casefold()
    searchable_removed = " ".join(removed).casefold()
    if any(word in searchable_added for word in ("resultado", "dictamen", "seleccionad")):
        heading = "🚨 Cambio relacionado con resultados"
    elif any(
        word in searchable_removed for word in ("resultado", "dictamen", "seleccionad")
    ):
        heading = "🚨 Cambio relacionado con resultados"
    elif any(word in searchable_added for word in ("aviso", "modificación", "calendario")):
        heading = "📢 Posible aviso nuevo o actualizado"
    elif any(
        word in searchable_removed for word in ("aviso", "modificación", "calendario")
    ):
        heading = "📢 Cambio relacionado con un aviso"
    else:
        heading = "🔔 Cambio detectado en la convocatoria PIL 2026"

    output = [heading, "", f"Detectado: {detected_at}"]
    if event_id:
        output.append(f"Evento: {event_id}")
    output.append(f"Página: {target_url}")

    def append_section(title: str, marker: str, items: Sequence[str]) -> None:
        if not items:
            return
        output.extend(("", title))
        included = 0
        for item in items:
            candidate = f"{marker} {_short_line(item)}"
            projected = "\n".join((*output, candidate))
            if len(projected) > TELEGRAM_MESSAGE_TARGET - 90:
                break
            output.append(candidate)
            included += 1
        omitted = len(items) - included
        if omitted:
            output.append(f"… y {omitted} línea(s) más.")

    append_section("Contenido agregado o actualizado:", "+", added)
    append_section("Contenido anterior reemplazado o eliminado:", "-", removed)
    output.extend(("", "Abre la página para confirmar el detalle."))
    message = "\n".join(output)
    if len(message) > MAX_TELEGRAM_MESSAGE_LENGTH:
        # Defensa final; append_section normalmente mantiene el texto por debajo.
        message = message[: TELEGRAM_MESSAGE_TARGET - 1] + "…"
    return message


def _state_for_snapshot(
    settings: Settings,
    snapshot: Snapshot,
    previous: dict[str, Any],
    *,
    changed: bool,
) -> dict[str, Any]:
    state = dict(previous)
    state.update(
        {
            "schema_version": STATE_SCHEMA_VERSION,
            "source_key": settings.source_key,
            "target_url": settings.target_url,
            "css_selector": settings.css_selector,
            "fingerprint": snapshot.fingerprint,
            "snapshot_lines": list(snapshot.lines),
            "last_checked_at": snapshot.fetched_at,
        }
    )
    state.setdefault("pending_events", [])
    state.setdefault("baseline_created_at", snapshot.fetched_at)
    if changed:
        state["last_change_at"] = snapshot.fetched_at
    return state


def _mark_healthy(state: dict[str, Any]) -> dict[str, Any]:
    healthy_state = dict(state)
    healthy_state.update(
        {
            "consecutive_failures": 0,
            "last_error": None,
            "failure_alert_sent": False,
        }
    )
    return healthy_state


def _create_change_event(
    *,
    old_fingerprint: str,
    old_lines: Sequence[str],
    snapshot: Snapshot,
    target_url: str,
) -> dict[str, Any]:
    return {
        "event_id": uuid.uuid4().hex[:12],
        "old_fingerprint": old_fingerprint,
        "new_fingerprint": snapshot.fingerprint,
        "old_lines": list(old_lines),
        "new_lines": list(snapshot.lines),
        "target_url": target_url,
        "detected_at": local_now(),
        "detected_at_utc": snapshot.fetched_at,
    }


def _flush_pending_events(
    settings: Settings,
    state: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    """Entrega en orden la outbox; persiste cada confirmación de Telegram."""

    current_state = dict(state)
    delivered = 0
    while current_state.get("pending_events"):
        pending_events = list(current_state["pending_events"])
        event = pending_events[0]
        message = format_change_message(
            old_lines=event["old_lines"],
            new_lines=event["new_lines"],
            target_url=event["target_url"],
            detected_at=event["detected_at"],
            event_id=event["event_id"],
        )
        send_telegram(settings, message)

        # Si este guardado falla, el evento queda pendiente y puede duplicarse,
        # pero nunca se pierde silenciosamente.
        updated_state = dict(current_state)
        updated_state["pending_events"] = pending_events[1:]
        updated_state["last_notification_at"] = utc_now()
        updated_state["last_notified_fingerprint"] = event["new_fingerprint"]
        save_state(settings.state_file, updated_state)
        current_state = updated_state
        # Mantiene al llamador sincronizado si un evento posterior falla.
        state.clear()
        state.update(current_state)
        delivered += 1
    return current_state, delivered


def _safe_error_text(error: Exception, settings: Settings) -> str:
    text = normalize_text(str(error)) or type(error).__name__
    if settings.telegram_bot_token:
        text = text.replace(settings.telegram_bot_token, "[TOKEN OCULTO]")
    return _short_line(text, 500)


def record_failure(
    settings: Settings,
    state: dict[str, Any],
    error: Exception,
) -> None:
    failure_state = dict(state)
    previous_failures = failure_state.get("consecutive_failures", 0)
    if (
        isinstance(previous_failures, bool)
        or not isinstance(previous_failures, int)
        or previous_failures < 0
    ):
        previous_failures = 0
    failures = previous_failures + 1
    safe_error = _safe_error_text(error, settings)
    failure_state.update(
        {
            "schema_version": STATE_SCHEMA_VERSION,
            "target_url": settings.target_url,
            "last_attempt_at": utc_now(),
            "consecutive_failures": failures,
            "last_error": safe_error,
        }
    )
    LOGGER.error("Revisión fallida (%d consecutiva[s]): %s", failures, safe_error)

    should_alert = (
        failures >= settings.failure_alert_threshold
        and not failure_state.get("failure_alert_sent", False)
        and not isinstance(error, TelegramError)
    )
    if should_alert:
        alert = (
            "⚠️ El monitor PIL 2026 no puede revisar la página\n\n"
            f"Fallos consecutivos: {failures}\n"
            f"Último error: {safe_error}\n"
            f"Página: {settings.target_url}"
        )
        try:
            send_telegram(settings, alert)
        except TelegramError as telegram_error:
            LOGGER.error("Tampoco se pudo enviar la alerta operativa: %s", telegram_error)
        else:
            failure_state["failure_alert_sent"] = True

    try:
        save_state(settings.state_file, failure_state)
    except StateError as state_error:
        LOGGER.error("No se pudo registrar el fallo: %s", state_error)


def _try_recovery_notification(settings: Settings, state: dict[str, Any]) -> bool:
    if not state.get("failure_alert_sent", False):
        return True
    message = (
        "✅ El monitor PIL 2026 volvió a funcionar\n\n"
        f"La página se revisó correctamente: {local_now()}\n"
        f"Página: {settings.target_url}"
    )
    try:
        send_telegram(settings, message)
    except TelegramError as error:
        LOGGER.warning("No se pudo enviar el aviso de recuperación: %s", error)
        return False
    return True


def perform_check(settings: Settings, session: requests.Session) -> bool:
    # Un estado ilegible implica pérdida de continuidad. Se deja que el error
    # detenga el proceso para que terminal/systemd lo hagan visible.
    state = load_state(settings.state_file)

    has_compatible_baseline = (
        state.get("schema_version") == STATE_SCHEMA_VERSION
        and state.get("source_key") == settings.source_key
        and isinstance(state.get("fingerprint"), str)
        and isinstance(state.get("snapshot_lines"), list)
    )
    if state.get("pending_events") and not has_compatible_baseline:
        raise StateError(
            "El estado contiene alertas pendientes pero ya no es compatible con "
            "la configuración actual. Restaura la configuración anterior para "
            "entregarlas o respalda el estado antes de restablecer la línea base."
        )

    try:
        snapshot = fetch_snapshot(settings, session)
    except MonitorError as error:
        failure_state = state
        if state.get("pending_events"):
            try:
                failure_state, delivered = _flush_pending_events(settings, state)
            except (TelegramError, StateError) as delivery_error:
                LOGGER.error(
                    "No se pudo entregar la cola durante el fallo de página: %s",
                    _safe_error_text(delivery_error, settings),
                )
                failure_state = state
            except Exception:
                LOGGER.exception(
                    "Error inesperado al entregar la cola durante un fallo de página."
                )
                raise
            else:
                if delivered:
                    LOGGER.info(
                        "%d alerta(s) pendiente(s) entregada(s) aunque la página falló.",
                        delivered,
                    )
        record_failure(settings, failure_state, error)
        return False
    except Exception:
        LOGGER.exception("Error inesperado al procesar la página.")
        raise

    if not has_compatible_baseline:
        health_seed: dict[str, Any] = {}
        if not state.get("fingerprint") and state.get("target_url") in {
            None,
            settings.target_url,
        }:
            for field in (
                "consecutive_failures",
                "last_error",
                "failure_alert_sent",
                "last_attempt_at",
            ):
                if field in state:
                    health_seed[field] = state[field]
        try:
            baseline_state = _state_for_snapshot(
                settings, snapshot, health_seed, changed=False
            )
            baseline_state["pending_events"] = []
            save_state(settings.state_file, baseline_state)
        except StateError as error:
            record_failure(settings, state, error)
            return False

        if baseline_state.get("failure_alert_sent", False):
            if not _try_recovery_notification(settings, baseline_state):
                record_failure(
                    settings,
                    baseline_state,
                    TelegramError("No se pudo enviar el aviso de recuperación."),
                )
                return False

        healthy_baseline = _mark_healthy(baseline_state)
        try:
            save_state(settings.state_file, healthy_baseline)
        except StateError as error:
            record_failure(settings, baseline_state, error)
            return False
        LOGGER.info(
            "Línea base creada (%s). No se envió alerta de cambio.",
            snapshot.fingerprint[:12],
        )
        return True

    old_lines = tuple(str(line) for line in state["snapshot_lines"])
    changed = snapshot.fingerprint != state["fingerprint"]
    working_state = _state_for_snapshot(
        settings, snapshot, state, changed=changed
    )
    if changed:
        event = _create_change_event(
            old_fingerprint=state["fingerprint"],
            old_lines=old_lines,
            snapshot=snapshot,
            target_url=settings.target_url,
        )
        pending_events = list(working_state.get("pending_events", []))
        pending_events.append(event)
        working_state["pending_events"] = pending_events

    try:
        # La transición queda en disco antes de contactar Telegram. Así no se
        # pierde aunque la página vuelva al contenido anterior entre revisiones.
        save_state(settings.state_file, working_state)
    except StateError as error:
        record_failure(settings, working_state, error)
        return False

    try:
        flushed_state, delivered = _flush_pending_events(settings, working_state)
    except (TelegramError, StateError) as error:
        record_failure(settings, working_state, error)
        return False
    except Exception:
        LOGGER.exception("Error inesperado al procesar la cola de notificaciones.")
        raise

    if flushed_state.get("failure_alert_sent", False) and delivered == 0:
        if not _try_recovery_notification(settings, flushed_state):
            record_failure(
                settings,
                flushed_state,
                TelegramError("No se pudo enviar el aviso de recuperación."),
            )
            return False

    healthy_state = _mark_healthy(flushed_state)
    try:
        save_state(settings.state_file, healthy_state)
    except StateError as error:
        record_failure(settings, flushed_state, error)
        return False

    if delivered:
        LOGGER.info(
            "%d cambio(s) notificado(s); huella observada %s.",
            delivered,
            snapshot.fingerprint[:12],
        )
    elif changed:
        # Sólo debería ocurrir con una cola alterada externamente.
        LOGGER.warning(
            "Se observó un cambio, pero no había eventos pendientes para notificar."
        )
    else:
        LOGGER.info("Sin cambios (%s).", snapshot.fingerprint[:12])
    return True


def run_forever(settings: Settings, session: requests.Session) -> int:
    stop_event = threading.Event()

    def request_stop(signum: int, _frame: Any) -> None:
        LOGGER.info("Señal %s recibida; cerrando el monitor.", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    LOGGER.info(
        "Monitor iniciado. Intervalo: %.2f minutos. URL: %s",
        settings.interval_seconds / 60,
        settings.target_url,
    )
    next_run = time.monotonic()
    while not stop_event.is_set():
        perform_check(settings, session)
        next_run += settings.interval_seconds
        now = time.monotonic()
        if next_run <= now:
            next_run = now + settings.interval_seconds
        wait_seconds = next_run - now
        LOGGER.info("Siguiente revisión en %.1f minutos.", wait_seconds / 60)
        stop_event.wait(wait_seconds)
    return 0


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Monitorea la convocatoria PIL 2026 y avisa por Telegram."
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--once",
        action="store_true",
        help="hace una revisión y termina (útil para cron)",
    )
    modes.add_argument(
        "--test-telegram",
        action="store_true",
        help="envía un mensaje de prueba y termina",
    )
    modes.add_argument(
        "--dry-run",
        action="store_true",
        help="descarga e imprime el snapshot sin usar Telegram ni el estado",
    )
    return parser


def configure_logging(level: str) -> None:
    """Configura sólo nuestro logger; urllib3 nunca debe imprimir el token."""

    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    LOGGER.handlers.clear()
    LOGGER.addHandler(handler)
    LOGGER.setLevel(getattr(logging, level))
    LOGGER.propagate = False

    # urllib3 registra rutas HTTP completas en DEBUG. La ruta de sendMessage
    # contiene el token del bot, por lo que se mantiene silenciado siempre.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    try:
        settings = Settings.from_env(require_telegram=not args.dry_run)
    except ConfigurationError as error:
        configure_logging("INFO")
        LOGGER.error("Configuración inválida: %s", error)
        return 2

    configure_logging(settings.log_level)
    session = build_http_session(settings.request_retries)

    if args.dry_run:
        try:
            snapshot = fetch_snapshot(settings, session)
        except Exception as error:
            LOGGER.error("No se pudo generar el snapshot: %s", error)
            return 1
        print(f"Huella: {snapshot.fingerprint}")
        print("\n".join(snapshot.lines))
        return 0

    if args.test_telegram:
        try:
            send_telegram(
                settings,
                "✅ Prueba correcta del monitor PIL 2026\n\n"
                f"Hora: {local_now()}\n"
                f"Página: {settings.target_url}",
            )
        except TelegramError as error:
            LOGGER.error(
                "Falló la prueba de Telegram: %s",
                _safe_error_text(error, settings),
            )
            return 1
        LOGGER.info("Telegram confirmó el mensaje de prueba.")
        return 0

    try:
        with single_instance_lock(settings.state_file):
            if args.once:
                return 0 if perform_check(settings, session) else 1
            return run_forever(settings, session)
    except AlreadyRunningError as error:
        LOGGER.error("%s", error)
        return 1
    except MonitorError as error:
        LOGGER.error("El monitor se detuvo: %s", error)
        return 1
    except OSError as error:
        LOGGER.error("No se pudo crear el bloqueo de instancia: %s", error)
        return 1
    except Exception:
        LOGGER.exception("El monitor se detuvo por un error inesperado.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
