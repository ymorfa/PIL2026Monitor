import logging
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from monitor import (
    MAX_TELEGRAM_MESSAGE_LENGTH,
    ConfigurationError,
    MonitorError,
    PageValidationError,
    Settings,
    Snapshot,
    StateError,
    TelegramError,
    configure_logging,
    format_change_message,
    load_state,
    perform_check,
    save_state,
    send_telegram,
    snapshot_from_html,
)


PAGE_URL = "https://www.secihti.mx/convocatoria/pil-2026/"
SELECTOR = "div.post-58580"
EXPECTED_TEXT = "Inserción Laboral 2026"


def page(content: str, *, script_nonce: str = "1") -> str:
    return f"""
    <html>
      <body>
        <header>Contenido global que no debe monitorearse</header>
        <div class="elementor-location-single post-58580">
          <h1>Convocatoria del Programa de Inserción Laboral 2026</h1>
          <p>{content}</p>
          <p>Periodo 2026</p>
          <p>Estatus: abierta</p>
          <a href="/archivo.pdf">Descargar convocatoria</a>
          <script src="/_Incapsula_Resource?cb={script_nonce}"></script>
        </div>
      </body>
    </html>
    """


class SnapshotTests(unittest.TestCase):
    def snapshot(self, html: str):
        return snapshot_from_html(
            html,
            page_url=PAGE_URL,
            selector=SELECTOR,
            expected_text=EXPECTED_TEXT,
        )

    def test_ignores_dynamic_scripts(self):
        first = self.snapshot(page("Sin resultados", script_nonce="123"))
        second = self.snapshot(page("Sin resultados", script_nonce="999"))
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_ignores_inline_markup_when_visible_text_is_equal(self):
        first = self.snapshot(page("Resultados publicados."))
        second = self.snapshot(page("<strong>Resultados</strong> publicados."))
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_detects_visible_text_change(self):
        first = self.snapshot(page("Sin resultados"))
        second = self.snapshot(page("Resultados publicados"))
        self.assertNotEqual(first.fingerprint, second.fingerprint)

    def test_detects_link_change_even_when_label_is_equal(self):
        first = self.snapshot(page("Sin resultados"))
        second_html = page("Sin resultados").replace("archivo.pdf", "resultados.pdf")
        second = self.snapshot(second_html)
        self.assertNotEqual(first.fingerprint, second.fingerprint)

    def test_captures_a_results_section_and_its_pdf(self):
        html = page(
            "Sin resultados</p><section id='resultados'><h2>Resultados</h2>"
            "<a href='/resultados.pdf'>Dictamen de personas seleccionadas</a>"
            "</section><p>Fin"
        )
        snapshot = self.snapshot(html)
        joined = "\n".join(snapshot.lines)
        self.assertIn("Resultados", joined)
        self.assertIn("https://www.secihti.mx/resultados.pdf", joined)

    def test_rejects_page_without_expected_container(self):
        with self.assertRaises(PageValidationError):
            self.snapshot("<html><body><h1>Error temporal</h1></body></html>")

    def test_rejects_page_without_expected_text(self):
        html = page("Sin resultados").replace("Inserción Laboral 2026", "Otra página")
        with self.assertRaises(PageValidationError):
            self.snapshot(html)


class MessageTests(unittest.TestCase):
    def test_classifies_results_and_stays_within_telegram_limit(self):
        old = ["Sin resultados"]
        new = ["Resultados publicados", *("x" * 500 for _ in range(30))]
        message = format_change_message(
            old_lines=old,
            new_lines=new,
            target_url=PAGE_URL,
            detected_at="2026-08-27T12:00:00-06:00",
        )
        self.assertIn("relacionado con resultados", message)
        self.assertLessEqual(len(message), MAX_TELEGRAM_MESSAGE_LENGTH)


class StateTests(unittest.TestCase):
    def test_state_is_saved_and_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "state.json"
            expected = {"fingerprint": "abc", "snapshot_lines": ["uno"]}
            save_state(path, expected)
            self.assertEqual(load_state(path), expected)
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_rejects_malformed_state_types(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(
                '{"snapshot_lines": ["bien", 123]}', encoding="utf-8"
            )
            with self.assertRaises(StateError):
                load_state(path)

    def test_rejects_null_pending_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text('{"pending_events": null}', encoding="utf-8")
            with self.assertRaises(StateError):
                load_state(path)


class CheckFlowTests(unittest.TestCase):
    def settings(self, state_file: Path) -> Settings:
        return Settings(
            target_url=PAGE_URL,
            telegram_bot_token="123456:test-token",
            telegram_chat_id="123456",
            interval_seconds=1800,
            connect_timeout=1,
            read_timeout=1,
            request_retries=0,
            css_selector=SELECTOR,
            expected_text=EXPECTED_TEXT,
            state_file=state_file,
            failure_alert_threshold=3,
            log_level="INFO",
        )

    def test_first_check_creates_baseline_without_notification(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(Path(directory) / "state.json")
            snapshot = Snapshot("first", ("uno",), "2026-08-27T12:00:00+00:00")
            with (
                patch("monitor.fetch_snapshot", return_value=snapshot),
                patch("monitor.send_telegram") as telegram,
            ):
                self.assertTrue(perform_check(settings, Mock()))
            telegram.assert_not_called()
            self.assertEqual(load_state(settings.state_file)["fingerprint"], "first")

    def test_change_is_notified_and_then_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(Path(directory) / "state.json")
            first = Snapshot("first", ("sin resultados",), "2026-08-27T12:00:00+00:00")
            changed = Snapshot(
                "second",
                ("Resultados publicados",),
                "2026-08-27T12:30:00+00:00",
            )
            with (
                patch("monitor.fetch_snapshot", side_effect=(first, changed)),
                patch("monitor.send_telegram") as telegram,
            ):
                self.assertTrue(perform_check(settings, Mock()))
                self.assertTrue(perform_check(settings, Mock()))
            telegram.assert_called_once()
            self.assertEqual(load_state(settings.state_file)["fingerprint"], "second")

    def test_telegram_failure_persists_the_event_for_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(Path(directory) / "state.json")
            first = Snapshot("first", ("sin resultados",), "2026-08-27T12:00:00+00:00")
            changed = Snapshot(
                "second",
                ("Resultados publicados",),
                "2026-08-27T12:30:00+00:00",
            )
            with (
                patch("monitor.fetch_snapshot", return_value=first),
                patch("monitor.send_telegram"),
            ):
                self.assertTrue(perform_check(settings, Mock()))
            with (
                patch("monitor.fetch_snapshot", return_value=changed),
                patch(
                    "monitor.send_telegram",
                    side_effect=TelegramError("fallo de prueba"),
                ),
            ):
                self.assertFalse(perform_check(settings, Mock()))

            state = load_state(settings.state_file)
            self.assertEqual(state["fingerprint"], "second")
            self.assertEqual(state["consecutive_failures"], 1)
            self.assertEqual(len(state["pending_events"]), 1)
            self.assertEqual(state["pending_events"][0]["new_fingerprint"], "second")

    def test_failed_change_and_reversion_are_both_eventually_delivered(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(Path(directory) / "state.json")
            first = Snapshot("first", ("estado A",), "2026-08-27T12:00:00+00:00")
            changed = Snapshot("second", ("estado B",), "2026-08-27T12:30:00+00:00")
            reverted = Snapshot("first", ("estado A",), "2026-08-27T13:00:00+00:00")
            with (
                patch(
                    "monitor.fetch_snapshot",
                    side_effect=(first, changed, reverted),
                ),
                patch(
                    "monitor.send_telegram",
                    side_effect=(TelegramError("caído"), None, None),
                ) as telegram,
            ):
                self.assertTrue(perform_check(settings, Mock()))
                self.assertFalse(perform_check(settings, Mock()))
                self.assertTrue(perform_check(settings, Mock()))

            self.assertEqual(telegram.call_count, 3)
            sent_messages = [call.args[1] for call in telegram.call_args_list]
            self.assertEqual(sent_messages[0], sent_messages[1])
            self.assertNotEqual(sent_messages[1], sent_messages[2])
            state = load_state(settings.state_file)
            self.assertEqual(state["fingerprint"], "first")
            self.assertEqual(state["pending_events"], [])

    def test_unchanged_page_does_not_call_telegram(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(Path(directory) / "state.json")
            snapshot = Snapshot("same", ("igual",), "2026-08-27T12:00:00+00:00")
            with (
                patch("monitor.fetch_snapshot", return_value=snapshot),
                patch("monitor.send_telegram") as telegram,
            ):
                self.assertTrue(perform_check(settings, Mock()))
                self.assertTrue(perform_check(settings, Mock()))
            telegram.assert_not_called()

    def test_pending_event_is_delivered_even_when_page_fetch_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(Path(directory) / "state.json")
            first = Snapshot("first", ("estado A",), "2026-08-27T12:00:00+00:00")
            changed = Snapshot("second", ("estado B",), "2026-08-27T12:30:00+00:00")
            with (
                patch(
                    "monitor.fetch_snapshot",
                    side_effect=(first, changed, MonitorError("página caída")),
                ),
                patch(
                    "monitor.send_telegram",
                    side_effect=(TelegramError("Telegram caído"), None),
                ) as telegram,
            ):
                self.assertTrue(perform_check(settings, Mock()))
                self.assertFalse(perform_check(settings, Mock()))
                self.assertFalse(perform_check(settings, Mock()))

            self.assertEqual(telegram.call_count, 2)
            state = load_state(settings.state_file)
            self.assertEqual(state["pending_events"], [])
            self.assertEqual(state["fingerprint"], "second")

    def test_transient_first_write_failure_does_not_lose_change(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(Path(directory) / "state.json")
            first = Snapshot("first", ("estado A",), "2026-08-27T12:00:00+00:00")
            changed = Snapshot("second", ("estado B",), "2026-08-27T12:30:00+00:00")
            reverted = Snapshot("first", ("estado A",), "2026-08-27T13:00:00+00:00")

            with (
                patch("monitor.fetch_snapshot", return_value=first),
                patch("monitor.send_telegram"),
            ):
                self.assertTrue(perform_check(settings, Mock()))

            real_save_state = save_state
            write_calls = 0

            def fail_once(path, state):
                nonlocal write_calls
                write_calls += 1
                if write_calls == 1:
                    raise StateError("fallo transitorio")
                real_save_state(path, state)

            with (
                patch("monitor.fetch_snapshot", return_value=changed),
                patch("monitor.send_telegram") as telegram,
                patch("monitor.save_state", side_effect=fail_once),
            ):
                self.assertFalse(perform_check(settings, Mock()))
            telegram.assert_not_called()
            self.assertEqual(
                len(load_state(settings.state_file)["pending_events"]), 1
            )

            with (
                patch("monitor.fetch_snapshot", return_value=reverted),
                patch("monitor.send_telegram") as telegram,
            ):
                self.assertTrue(perform_check(settings, Mock()))
            self.assertEqual(telegram.call_count, 2)
            state = load_state(settings.state_file)
            self.assertEqual(state["pending_events"], [])
            self.assertEqual(state["fingerprint"], "first")

    def test_incompatible_configuration_never_discards_pending_events(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(Path(directory) / "state.json")
            first = Snapshot("first", ("estado A",), "2026-08-27T12:00:00+00:00")
            changed = Snapshot("second", ("estado B",), "2026-08-27T12:30:00+00:00")
            with (
                patch("monitor.fetch_snapshot", side_effect=(first, changed)),
                patch(
                    "monitor.send_telegram",
                    side_effect=TelegramError("Telegram caído"),
                ),
            ):
                self.assertTrue(perform_check(settings, Mock()))
                self.assertFalse(perform_check(settings, Mock()))

            incompatible = replace(settings, css_selector="div.otro-post")
            with patch("monitor.fetch_snapshot") as fetch:
                with self.assertRaises(StateError):
                    perform_check(incompatible, Mock())
            fetch.assert_not_called()
            self.assertEqual(
                len(load_state(settings.state_file)["pending_events"]), 1
            )


class TelegramTests(unittest.TestCase):
    def settings(self) -> Settings:
        return CheckFlowTests().settings(Path("/tmp/no-state-used.json"))

    def test_rejects_json_that_is_not_an_object(self):
        response = Mock(status_code=200, ok=True)
        response.json.return_value = []
        with (
            patch("monitor.requests.post", return_value=response),
            self.assertRaises(TelegramError),
        ):
            send_telegram(self.settings(), "prueba")

    def test_debug_logging_keeps_urllib3_at_warning(self):
        configure_logging("DEBUG")
        self.addCleanup(configure_logging, "WARNING")
        self.assertGreaterEqual(
            logging.getLogger("urllib3").getEffectiveLevel(), logging.WARNING
        )


class ConfigurationTests(unittest.TestCase):
    def test_rejects_non_finite_interval(self):
        for invalid in ("nan", "inf", "-inf"):
            with self.subTest(value=invalid):
                with patch.dict(
                    os.environ,
                    {"CHECK_INTERVAL_MINUTES": invalid},
                    clear=False,
                ):
                    with self.assertRaises(ConfigurationError):
                        Settings.from_env(require_telegram=False)


if __name__ == "__main__":
    unittest.main()
