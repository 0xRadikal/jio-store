"""Network-isolated regression tests. Run: python -m unittest -v test_safety."""
import io
import json
import os
import threading
import time
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import requests
from rich.console import Console
from typer.testing import CliRunner

from jiofarm import cli
from jiofarm.config import Config, load_config
from jiofarm.fivesim.client import FiveSim, FiveSimError
from jiofarm.grizzly.client import GrizzlySMS, GrizzlyError
from jiofarm.hunter import WorkerState, hunt_once
from jiofarm.jio.check import jio_check_subscriber
from jiofarm.storage.store import Store

KEY = "synthetic-secret-never-a-real-key"


def response(text, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = text.encode()
    result.url = "https://example.invalid/?api_key=" + KEY
    result.headers["content-type"] = "application/json" if text.startswith("{") else "text/plain"
    return result


class Isolated(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("NETWORK_BLOCKED")))
        self.stack.enter_context(patch("requests.sessions.Session.send", side_effect=AssertionError("NETWORK_BLOCKED")))


class ConfigTests(Isolated):
    def test_secrets_not_in_repr(self):
        self.assertNotIn(KEY, repr(Config(api_key=KEY, tg_bot_token=KEY)))

    def test_config_rejects_bad_numbers(self):
        for name, value in [("CONCURRENCY", "0"), ("CONCURRENCY", "-1"), ("MAX_PRICE", "nan"),
                            ("MAX_PRICE", "inf"), ("MAX_PRICE", "-1"), ("CANCEL_DELAY_SECONDS", "-1")]:
            with self.subTest(name=name, value=value), patch.dict(os.environ, {"GRIZZLY_API_KEY": KEY, name: value}, clear=True), patch("jiofarm.config.load_dotenv"):
                with self.assertRaises((ValueError, SystemExit)):
                    load_config()

    def test_zero_concurrency_override_not_silently_ignored(self):
        cfg = Config(concurrency_override=0)
        self.assertEqual(cfg.effective_concurrency, 0)


class ProviderTests(Isolated):
    def test_grizzly_balance(self):
        with patch("requests.sessions.Session.send", return_value=response("ACCESS_BALANCE:3.1")):
            self.assertEqual(GrizzlySMS(KEY).balance(), 3.1)

    def test_grizzly_error_is_redacted(self):
        with patch("requests.sessions.Session.send", return_value=response("unavailable", 503)), patch("jiofarm.grizzly.client.time.sleep"):
            with self.assertRaises(GrizzlyError) as caught:
                GrizzlySMS(KEY).balance()
        self.assertNotIn(KEY, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_grizzly_purchase_never_retries_timeout(self):
        with patch("requests.sessions.Session.send", side_effect=requests.Timeout(KEY)) as send, patch("jiofarm.grizzly.client.time.sleep"):
            with self.assertRaises(Exception):
                GrizzlySMS(KEY).rent(.3)
        self.assertEqual(send.call_count, 1)

    def test_grizzly_purchase_never_retries_429(self):
        with patch("requests.sessions.Session.send", return_value=response("busy", 429)) as send, patch("jiofarm.grizzly.client.time.sleep"):
            with self.assertRaises(Exception):
                GrizzlySMS(KEY).rent(.3)
        self.assertEqual(send.call_count, 1)

    def test_provider_does_not_follow_redirect(self):
        with patch("requests.get", return_value=response("redirect", 302)) as get:
            with self.assertRaises(GrizzlyError):
                GrizzlySMS(KEY).balance()
            self.assertIs(get.call_args.kwargs.get("allow_redirects"), False)

    def test_grizzly_mutation_error_not_success(self):
        for method in ("ready", "complete", "cancel"):
            with self.subTest(method=method), patch("requests.sessions.Session.send", return_value=response("BAD_STATUS")):
                with self.assertRaises(GrizzlyError):
                    getattr(GrizzlySMS(KEY), method)("test")

    def test_grizzly_cancel_success(self):
        with patch("requests.sessions.Session.send", return_value=response("ACCESS_CANCEL")):
            GrizzlySMS(KEY).cancel("test")

    def test_fivesim_price_limit_transmitted(self):
        with patch("requests.sessions.Session.send", return_value=response('{"id":1,"phone":"synthetic"}')) as send:
            FiveSim(KEY).rent(.3)
        query = parse_qs(urlsplit(send.call_args.args[0].url).query)
        self.assertEqual(query.get("maxPrice"), ["0.3"])

    def test_fivesim_purchase_never_retries_timeout(self):
        with patch("requests.sessions.Session.send", side_effect=requests.Timeout(KEY)) as send, patch("jiofarm.fivesim.client.time.sleep"):
            with self.assertRaises(Exception):
                FiveSim(KEY).rent(.3)
        self.assertEqual(send.call_count, 1)

    def test_fivesim_missing_balance_is_not_zero(self):
        with patch("requests.sessions.Session.send", return_value=response("{}")):
            with self.assertRaises(FiveSimError):
                FiveSim(KEY).balance()

    def test_fivesim_cancel_error_not_success(self):
        with patch("requests.sessions.Session.send", return_value=response("order not found")):
            with self.assertRaises(FiveSimError):
                FiveSim(KEY).cancel("test")

    def test_negative_or_nonfinite_price_rejected_before_network(self):
        for provider in (GrizzlySMS, FiveSim):
            for price in (-1, float("nan"), float("inf")):
                with self.subTest(provider=provider.__name__, price=price), patch("requests.sessions.Session.send") as send:
                    with self.assertRaises((ValueError, GrizzlyError, FiveSimError)):
                        provider(KEY).rent(price)
                    send.assert_not_called()


class CliTests(Isolated):
    def test_help(self):
        self.assertEqual(CliRunner().invoke(cli.app, ["--help"]).exit_code, 0)

    def test_balance_error_exit_nonzero_without_secret(self):
        with patch("jiofarm.cli.load_config", return_value=Config(api_key=KEY)), patch("jiofarm.cli.create_provider") as factory:
            factory.return_value.balance.side_effect = requests.ConnectionError(KEY)
            result = CliRunner().invoke(cli.app, ["balance"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertNotIn(KEY, result.output)

    def test_preserves_env_database_path(self):
        cfg = Config(api_key=KEY, db_path="configured-test-path")
        with patch("jiofarm.cli.load_config", return_value=cfg), patch("jiofarm.cli.install_doh_shield"), patch("jiofarm.cli.create_provider"), patch("jiofarm.cli.Store", side_effect=RuntimeError("STOP_BEFORE_WORKERS")) as store:
            CliRunner().invoke(cli.app, ["run"])
        self.assertEqual(store.call_args.args[0], "configured-test-path")

    def test_bad_run_values_rejected_before_provider(self):
        cases = [["--count", "0"], ["--concurrency", "-1"], ["--target", "-1"],
                 ["--max-price", "nan"], ["--max-price", "inf"], ["--duration", "0s"]]
        for args in cases:
            with self.subTest(args=args), patch("jiofarm.cli.load_config", return_value=Config(api_key=KEY)), patch("jiofarm.cli.install_doh_shield"), patch("jiofarm.cli.create_provider", side_effect=RuntimeError("STOP")) as factory:
                result = CliRunner().invoke(cli.app, ["run"] + args)
                self.assertNotEqual(result.exit_code, 0)
                factory.assert_not_called()

    def test_target_mode_requires_explicit_purchase_cap(self):
        with patch("jiofarm.cli.load_config", return_value=Config(api_key=KEY)), patch("jiofarm.cli.install_doh_shield"), patch("jiofarm.cli.create_provider", side_effect=RuntimeError("STOP")) as factory:
            result = CliRunner().invoke(cli.app, ["run", "--target", "3"])
        self.assertNotEqual(result.exit_code, 0)
        factory.assert_not_called()

    def test_cli_joins_workers_without_abandoning_them(self):
        joins = []
        original_join = threading.Thread.join
        def join(thread, timeout=None):
            joins.append(timeout)
            return original_join(thread, timeout)
        provider = Mock()
        provider.balance.return_value = 1
        cfg = Config(api_key=KEY, concurrency=1)
        with patch("jiofarm.cli.load_config", return_value=cfg), patch("jiofarm.cli.install_doh_shield"), patch("jiofarm.cli.create_provider", return_value=provider), patch("jiofarm.cli.Store"), patch("jiofarm.cli.RefundWorker"), patch("jiofarm.cli.hunt_once", side_effect=lambda *args: time.sleep(.02)), patch("jiofarm.cli.Live"), patch("jiofarm.cli._build_layout"), patch("jiofarm.cli.console", Console(file=io.StringIO())), patch("threading.Thread.join", join):
            result = CliRunner().invoke(cli.app, ["run", "--count", "1", "--db", ":memory:"])
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(joins)
        self.assertTrue(all(timeout is None for timeout in joins))


class CoreTests(Isolated):
    def test_storage_failure_still_releases_slot(self):
        provider = Mock()
        provider.rent.side_effect = RuntimeError("no order")
        store = Mock()
        store.save.side_effect = RuntimeError("disk full")
        slots = threading.Semaphore(1)
        with self.assertRaises(RuntimeError):
            hunt_once(provider, store, Mock(), slots, Config(), WorkerState(1))
        self.assertTrue(slots.acquire(blocking=False))

    def test_not_subscribed_is_false(self):
        with patch("requests.sessions.Session.send", return_value=response('{"errorCode":"400 BAD_REQUEST","errorMessage":"NOT_SUBSCRIBED_USER"}', 400)):
            self.assertFalse(jio_check_subscriber("0000000000"))

    def test_transport_failure_is_not_ineligibility(self):
        with patch("requests.sessions.Session.send", side_effect=requests.Timeout("synthetic")):
            with self.assertRaises(Exception):
                jio_check_subscriber("0000000000")

    def test_html_200_is_not_a_valid_subscriber(self):
        with patch("requests.sessions.Session.send", return_value=response("<html>maintenance</html>")):
            with self.assertRaises(Exception):
                jio_check_subscriber("0000000000")

    def test_sqlite_round_trip(self):
        store = Store(":memory:")
        self.addCleanup(store.conn.close)
        store.save("synthetic", "test", None, False, None)
        self.assertEqual(store.stats(), {"hunts": 1, "logins": 0, "links": 0})


class AdditionalSafetyTests(Isolated):
    def test_otp_application_errors_are_not_success(self):
        from jiofarm.jio.auth import jio_send_otp, jio_validate_otp
        for body in ('{"success":false}', '{"errorMessage":"invalid otp"}', '<html>maintenance</html>'):
            for method in (jio_send_otp, jio_validate_otp):
                with self.subTest(body=body, method=method.__name__), patch("requests.sessions.Session.send", return_value=response(body)):
                    self.assertFalse(method(requests.Session(), "synthetic"))

    def test_provider_factory_preserves_product(self):
        from jiofarm.hunter import create_provider
        for name in ("grizzlysms", "fivesim"):
            self.assertEqual(create_provider(Config(provider=name, product="test-product")).product, "test-product")

    def test_parallel_target_and_duration_respect_purchase_cap(self):
        for mode in (["--target", "9"], ["--duration", "5s"]):
            calls = []
            barrier = threading.Barrier(3)
            def controlled_job(*args):
                calls.append(1)
                barrier.wait(timeout=2)
            provider = Mock()
            provider.balance.return_value = 1
            with self.subTest(mode=mode), patch("jiofarm.cli.load_config", return_value=Config(api_key=KEY, concurrency=8)), patch("jiofarm.cli.install_doh_shield"), patch("jiofarm.cli.create_provider", return_value=provider), patch("jiofarm.cli.Store"), patch("jiofarm.cli.RefundWorker"), patch("jiofarm.cli.hunt_once", side_effect=controlled_job), patch("jiofarm.cli.Live"), patch("jiofarm.cli._build_layout"), patch("jiofarm.cli.console", Console(file=io.StringIO())):
                result = CliRunner().invoke(cli.app, ["run", "--count", "3", "--db", ":memory:"] + mode)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(len(calls), 3)

    def test_dashboard_failure_still_drains_workers_and_refunds(self):
        finished = threading.Event()
        def controlled_job(*args):
            time.sleep(.03)
            finished.set()
        provider = Mock()
        provider.balance.return_value = 1
        refunds = Mock()
        with patch("jiofarm.cli.load_config", return_value=Config(api_key=KEY)), patch("jiofarm.cli.install_doh_shield"), patch("jiofarm.cli.create_provider", return_value=provider), patch("jiofarm.cli.Store") as store, patch("jiofarm.cli.RefundWorker", return_value=refunds), patch("jiofarm.cli.hunt_once", side_effect=controlled_job), patch("jiofarm.cli._build_layout", side_effect=RuntimeError("display failed")), patch("jiofarm.cli.console", Console(file=io.StringIO())):
            result = CliRunner().invoke(cli.app, ["run", "--count", "1"])
        self.assertNotEqual(result.exit_code, 0)
        finished.wait(1)
        refunds.wait_all.assert_called_once_with(timeout=900)
        store.return_value.conn.close.assert_called_once()

    def test_dashboard_rendering_has_no_provider_requests(self):
        provider = Mock()
        refunds = Mock(pending=0)
        cli._build_layout(provider, [], 0, 0, refunds, Config(), [])
        provider.balance.assert_not_called()

    def test_balance_failure_does_not_start_purchases(self):
        provider = Mock()
        provider.balance.side_effect = RuntimeError(KEY)
        with patch("jiofarm.cli.load_config", return_value=Config(api_key=KEY)), patch("jiofarm.cli.install_doh_shield"), patch("jiofarm.cli.create_provider", return_value=provider), patch("jiofarm.cli.Store"), patch("jiofarm.cli.hunt_once") as hunt:
            result = CliRunner().invoke(cli.app, ["run", "--count", "1"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertNotIn(KEY, result.output)
        hunt.assert_not_called()

    def test_completion_failure_does_not_cancel_consumed_activation(self):
        provider = Mock()
        provider.rent.return_value = ("test", "0000000000")
        provider.complete.side_effect = RuntimeError("completion failed")
        refunds = Mock()
        with patch("jiofarm.hunter.jio_check_subscriber", return_value=True), patch("jiofarm.hunter.jio_send_otp", return_value=True), patch("jiofarm.hunter.poll_otp", return_value="synthetic"), patch("jiofarm.hunter.jio_validate_otp", return_value=True), patch("jiofarm.hunter.hunt_link", return_value=None):
            hunt_once(provider, Mock(), refunds, threading.Semaphore(1), Config(), WorkerState(1))
        refunds.schedule.assert_not_called()

    def test_refund_timeout_is_not_success(self):
        from jiofarm.grizzly.refund import RefundWorker
        provider = Mock()
        worker = RefundWorker(provider)
        try:
            worker.schedule("test", 120)
            self.assertFalse(worker.wait_all(timeout=0))
            self.assertEqual(worker.pending, 1)
            provider.cancel.assert_not_called()
        finally:
            worker._stop.set()
            worker._thread.join()

    def test_empty_refund_worker_closes(self):
        from jiofarm.grizzly.refund import RefundWorker
        worker = RefundWorker(Mock())
        self.assertTrue(worker.wait_all(timeout=0))
        self.assertFalse(worker._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
