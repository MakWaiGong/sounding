import http.client
import json
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import feishu_api as api


class Response:
    def __init__(self, status=200, payload=None, headers=None, raw=None):
        self.status = status
        self.headers = headers or {}
        self.raw = raw if raw is not None else json.dumps(payload or {}).encode()

    def getheader(self, name):
        return self.headers.get(name)

    def read(self):
        if isinstance(self.raw, Exception):
            raise self.raw
        return self.raw


class Connection:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.closed = False

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, body, headers))

    def getresponse(self):
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        self.closed = True


def client(retries=2, token="fake-token"):
    result = api.FeishuClient("fake-app", "fake-secret", retries=retries)
    result.token = lambda: token
    return result


class RetryTests(unittest.TestCase):
    def test_throttle_responses_then_success(self):
        throttles = [Response(429, {"code": 0}),
                     Response(200, {"code": 99991400}),
                     Response(429, raw=b"not json")]
        for throttle in throttles:
            with self.subTest(status=throttle.status, body=throttle.raw):
                conn = Connection(throttle, Response(200, {"code": 0, "data": {"id": "ok"}}))
                with patch.object(api.http.client, "HTTPSConnection", return_value=conn), \
                     patch.object(api.time, "sleep") as sleep, \
                     patch.object(api.random, "uniform", return_value=0.2):
                    self.assertEqual(client().post("/create", {"name": "x"}), {"id": "ok"})
                self.assertEqual(len(conn.requests), 2)
                sleep.assert_called_once_with(0.2)

    def test_rate_limit_retry_budget_is_capped(self):
        conn = Connection(*[Response(429, {"msg": "fake-token"}) for _ in range(3)])
        with patch.object(api.http.client, "HTTPSConnection", return_value=conn), \
             patch.object(api.time, "sleep") as sleep, \
             patch.object(api.random, "uniform", side_effect=lambda low, high: high):
            with self.assertRaises(api.FeishuError) as context:
                client(retries=2).post("/create", {})
        self.assertIsInstance(context.exception, api.FeishuRateLimitError)
        self.assertNotIsInstance(context.exception, api.FeishuUncertainError)
        self.assertEqual(context.exception.code, 429)
        self.assertNotIn("fake-token", str(context.exception))
        self.assertEqual(len(conn.requests), 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.4, 0.8])
        self.assertEqual(client(retries=100000).retries, api._MAX_RETRIES)
        self.assertEqual(client(retries=-1).retries, 0)

    def test_retry_after_seconds_and_http_date(self):
        for header in ("3", "0000000003", "Thu, 01 Jan 1970 00:16:43 GMT"):
            with self.subTest(header=header):
                conn = Connection(Response(429, headers={"Retry-After": header}),
                                  Response(200, {"code": 0, "data": {"ok": True}}))
                with patch.object(api.http.client, "HTTPSConnection", return_value=conn), \
                     patch.object(api.time, "sleep") as sleep, \
                     patch.object(api.time, "time", return_value=1000), \
                     patch.object(api.random, "uniform", return_value=0.1):
                    self.assertEqual(client().get("/read"), {"ok": True})
                sleep.assert_called_once_with(3.0)

    def test_excessive_retry_after_fails_without_retrying_early(self):
        conn = Connection(Response(429, headers={"Retry-After": "3600"}))
        with patch.object(api.http.client, "HTTPSConnection", return_value=conn), \
             patch.object(api.time, "sleep") as sleep:
            with self.assertRaises(api.FeishuError) as context:
                client().post("/create?token=fake-token", {})
        self.assertIsInstance(context.exception, api.FeishuRateLimitError)
        self.assertIn("maximum wait", str(context.exception))
        self.assertNotIn("fake-token", str(context.exception))
        self.assertEqual(len(conn.requests), 1)
        sleep.assert_not_called()

    def test_invalid_retry_after_uses_jitter(self):
        for value in ("-1", "nan", "private-token", "3.5"):
            with self.subTest(header=value):
                conn = Connection(Response(429, headers={"Retry-After": value}),
                                  Response(200, {"code": 0, "data": {"ok": True}}))
                with patch.object(api.http.client, "HTTPSConnection", return_value=conn), \
                     patch.object(api.time, "sleep") as sleep, \
                     patch.object(api.random, "uniform", return_value=0.15):
                    self.assertEqual(client().get("/read"), {"ok": True})
                sleep.assert_called_once_with(0.15)

    def test_post_disconnect_never_replayed(self):
        failures = [OSError("fake-token"), http.client.RemoteDisconnected("fake-token")]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                conn = Connection(failure)
                with patch.object(api.http.client, "HTTPSConnection", return_value=conn) as factory, \
                     patch.object(api.time, "sleep") as sleep:
                    with self.assertRaises(api.FeishuUncertainError) as context:
                        client().post("/create", {"name": "test"})
                self.assertIn("uncertain", str(context.exception))
                self.assertNotIn("fake-token", str(context.exception))
                self.assertTrue(context.exception.__suppress_context__)
                self.assertEqual(len(conn.requests), 1)
                self.assertTrue(conn.closed)
                factory.assert_called_once()
                sleep.assert_not_called()

    def test_incomplete_response_and_5xx_write_are_uncertain(self):
        failures = [Response(200, raw=http.client.IncompleteRead(b"partial")),
                    Response(200, raw=b"not json"),
                    Response(500, raw=b"not json"),
                    Response(503, {"code": 99991400, "msg": "fake-token"})]
        for failure in failures:
            with self.subTest(status=failure.status, raw=failure.raw):
                conn = Connection(failure)
                with patch.object(api.http.client, "HTTPSConnection", return_value=conn), \
                     patch.object(api.time, "sleep") as sleep:
                    with self.assertRaises(api.FeishuUncertainError) as context:
                        client().post("/create", {})
                self.assertNotIn("fake-token", str(context.exception))
                self.assertEqual(len(conn.requests), 1)
                sleep.assert_not_called()

    def test_all_write_methods_fail_closed_on_server_error(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                conn = Connection(Response(500, {"code": 0}))
                with patch.object(api.http.client, "HTTPSConnection", return_value=conn):
                    with self.assertRaises(api.FeishuUncertainError):
                        client().call(method, "/write")
                self.assertEqual(len(conn.requests), 1)

    def test_create_convenience_methods_propagate_uncertain_outcome(self):
        operations = (lambda c: c.create_doc("title"),
                      lambda c: c.create_record("app", "table", {"Title": "title"}))
        for operation in operations:
            for response in (OSError("disconnect"), Response(503)):
                with self.subTest(operation=operation, response=type(response).__name__):
                    conn = Connection(response)
                    with patch.object(api.http.client, "HTTPSConnection", return_value=conn), \
                         patch.object(api.time, "sleep") as sleep:
                        with self.assertRaises(api.FeishuUncertainError):
                            operation(client())
                    self.assertEqual(len(conn.requests), 1)
                    sleep.assert_not_called()

    def test_create_methods_preserve_explicit_throttle_codes(self):
        operations = (lambda c: c.create_doc("title"),
                      lambda c: c.create_record("app", "table", {"Title": "title"}))
        for operation in operations:
            for status, code in ((429, 429), (200, 99991400)):
                with self.subTest(operation=operation, status=status, code=code):
                    conn = Connection(*[Response(status, {"code": code}) for _ in range(3)])
                    with patch.object(api.http.client, "HTTPSConnection", return_value=conn), \
                         patch.object(api.time, "sleep"):
                        with self.assertRaises(api.FeishuError) as context:
                            operation(client())
                    self.assertIsInstance(context.exception, api.FeishuRateLimitError)
                    self.assertNotIsInstance(context.exception, api.FeishuUncertainError)
                    self.assertEqual(context.exception.code, code)
                    self.assertEqual(len(conn.requests), 3)

    def test_create_methods_recover_after_throttle(self):
        cases = ((lambda c: c.create_doc("title"), {"document": {"document_id": "doc"}}, "doc"),
                 (lambda c: c.create_record("app", "table", {}),
                  {"record": {"record_id": "rec"}}, {"record_id": "rec"}))
        for operation, data, expected in cases:
            for status, code in ((429, 0), (200, 99991400)):
                with self.subTest(status=status, code=code):
                    conn = Connection(Response(status, {"code": code}),
                                      Response(200, {"code": 0, "data": data}))
                    with patch.object(api.http.client, "HTTPSConnection", return_value=conn), \
                         patch.object(api.time, "sleep") as sleep:
                        self.assertEqual(operation(client()), expected)
                    self.assertEqual(len(conn.requests), 2)
                    sleep.assert_called_once()

    def test_create_throttle_followed_by_lookalike_is_not_marker(self):
        operations = (lambda c: c.create_doc("title"),
                      lambda c: c.create_record("app", "table", {}))
        for operation in operations:
            for status in (200, 400, 403):
                with self.subTest(status=status):
                    conn = Connection(Response(429), Response(status, {"code": 429}))
                    with patch.object(api.http.client, "HTTPSConnection", return_value=conn), \
                         patch.object(api.time, "sleep") as sleep:
                        with self.assertRaises(api.FeishuError) as context:
                            operation(client())
                    self.assertEqual(context.exception.code, 429)
                    self.assertNotIsInstance(context.exception, api.FeishuRateLimitError)
                    self.assertNotIsInstance(context.exception, api.FeishuUncertainError)
                    self.assertEqual(len(conn.requests), 2)
                    sleep.assert_called_once()

    def test_retry_disabled_marks_only_genuine_throttles(self):
        cases = ((429, 0, 429), (429, 123, 429), (429, 99991400, 99991400),
                 (200, 99991400, 99991400), (400, 99991400, 99991400),
                 (403, 99991400, 99991400))
        for status, code, expected in cases:
            with self.subTest(status=status, code=code):
                conn = Connection(Response(status, {"code": code}))
                with patch.object(api.http.client, "HTTPSConnection", return_value=conn), \
                     patch.object(api.time, "sleep") as sleep:
                    with self.assertRaises(api.FeishuRateLimitError) as context:
                        client().call("POST", "/create", {}, retry=False)
                self.assertIsInstance(context.exception, api.FeishuError)
                self.assertEqual(context.exception.code, expected)
                self.assertEqual(len(conn.requests), 1)
                sleep.assert_not_called()

    def test_lookalike_api_code_429_is_not_rate_limit_marker(self):
        for status in (200, 400, 403):
            for retry in (True, False):
                with self.subTest(status=status, retry=retry):
                    conn = Connection(Response(status, {"code": 429}))
                    with patch.object(api.http.client, "HTTPSConnection", return_value=conn), \
                         patch.object(api.time, "sleep") as sleep:
                        with self.assertRaises(api.FeishuError) as context:
                            client().call("POST", "/create", {}, retry=retry)
                    self.assertEqual(context.exception.code, 429)
                    self.assertNotIsInstance(context.exception, api.FeishuRateLimitError)
                    self.assertNotIsInstance(context.exception, api.FeishuUncertainError)
                    self.assertEqual(len(conn.requests), 1)
                    sleep.assert_not_called()

    def test_throttle_then_ambiguous_write_stays_uncertain(self):
        for failure in (OSError("disconnect"), Response(503, {"code": 99991400}),
                        Response(500, {"code": 429}), Response(200, raw=b"invalid")):
            with self.subTest(failure=type(failure).__name__):
                conn = Connection(Response(429), failure)
                with patch.object(api.http.client, "HTTPSConnection", return_value=conn), \
                     patch.object(api.time, "sleep") as sleep:
                    with self.assertRaises(api.FeishuUncertainError) as context:
                        client().create_doc("title")
                self.assertNotIsInstance(context.exception, api.FeishuRateLimitError)
                self.assertEqual(len(conn.requests), 2)
                sleep.assert_called_once()

    def test_upload_exhausted_throttle_has_rate_limit_marker(self):
        for status, code in ((429, 0), (200, 99991400)):
            with self.subTest(status=status, code=code):
                conn = Connection(*[Response(status, {"code": code}) for _ in range(3)])
                with patch.object(api.http.client, "HTTPSConnection", return_value=conn), \
                     patch.object(api.time, "sleep") as sleep:
                    with self.assertRaises(api.FeishuRateLimitError) as context:
                        client().upload_media(b"bytes", "image.png", "docx_image", "block")
                self.assertEqual(context.exception.code, 429 if status == 429 else 99991400)
                self.assertEqual(len(conn.requests), 3)
                self.assertEqual(sleep.call_count, 2)

    def test_create_methods_require_identifiers_without_replay(self):
        cases = ((lambda c: c.create_doc("title"), "document", "document_id"),
                 (lambda c: c.create_record("app", "table", {}), "record", "record_id"))
        for operation, key, identifier in cases:
            for value in (None, [], {}, {identifier: None}, {identifier: ""}, {identifier: 123}):
                with self.subTest(key=key, value=value):
                    conn = Connection(Response(200, {"code": 0, "data": {key: value}}))
                    with patch.object(api.http.client, "HTTPSConnection", return_value=conn), \
                         patch.object(api.time, "sleep") as sleep:
                        with self.assertRaises(api.FeishuUncertainError):
                            operation(client())
                    self.assertEqual(len(conn.requests), 1)
                    sleep.assert_not_called()
            conn = Connection(Response(200, {"code": 0, "data": {key: {identifier: "valid"}}}))
            with patch.object(api.http.client, "HTTPSConnection", return_value=conn):
                result = operation(client())
            self.assertEqual(result, "valid" if key == "document" else {identifier: "valid"})

    def test_non_json_4xx_is_a_definite_failure(self):
        conn = Connection(Response(403, raw=b"Forbidden"))
        with patch.object(api.http.client, "HTTPSConnection", return_value=conn):
            with self.assertRaises(api.FeishuError) as context:
                client().post("/create", {})
        self.assertNotIsInstance(context.exception, api.FeishuUncertainError)
        self.assertEqual(context.exception.code, 403)
        self.assertEqual(len(conn.requests), 1)

    def test_private_send_never_reconnects_post_by_default(self):
        conn = Connection(OSError("disconnect"))
        with patch.object(api.http.client, "HTTPSConnection", return_value=conn) as factory:
            with self.assertRaises(OSError):
                client()._send("POST", "/create")
        self.assertEqual(len(conn.requests), 1)
        factory.assert_called_once()

    def test_absolute_retry_limit_and_exponential_cap(self):
        conn = Connection(*[Response(429) for _ in range(api._MAX_RETRIES + 1)])
        with patch.object(api.http.client, "HTTPSConnection", return_value=conn), \
             patch.object(api.time, "sleep") as sleep, \
             patch.object(api.random, "uniform", side_effect=lambda low, high: high):
            with self.assertRaises(api.FeishuError):
                client(retries=100000).get("/read")
        self.assertEqual(len(conn.requests), api._MAX_RETRIES + 1)
        self.assertEqual([call.args[0] for call in sleep.call_args_list],
                         [0.4, 0.8, 1.6, 3.2, 6.4])

    def test_get_503_honors_retry_after_or_fails_closed(self):
        for delay in ("3", "3600"):
            with self.subTest(delay=delay):
                conn = Connection(Response(503, headers={"Retry-After": delay}),
                                  Response(200, {"data": {"ok": True}}))
                with patch.object(api.http.client, "HTTPSConnection", return_value=conn), \
                     patch.object(api.time, "sleep") as sleep:
                    if delay == "3":
                        self.assertEqual(client().get("/read"), {"ok": True})
                        sleep.assert_called_once_with(3.0)
                        self.assertEqual(len(conn.requests), 2)
                    else:
                        with self.assertRaises(api.FeishuError):
                            client().get("/read")
                        sleep.assert_not_called()
                        self.assertEqual(len(conn.requests), 1)

    def test_get_retries_disconnect_on_fresh_connection(self):
        first = Connection(OSError("stale"))
        second = Connection(Response(200, {"code": 0, "data": {"ok": True}}))
        with patch.object(api.http.client, "HTTPSConnection", side_effect=[first, second]), \
             patch.object(api.time, "sleep") as sleep, \
             patch.object(api.random, "uniform", return_value=0.1):
            self.assertEqual(client().get("/read"), {"ok": True})
        self.assertTrue(first.closed)
        self.assertEqual((len(first.requests), len(second.requests)), (1, 1))
        sleep.assert_called_once_with(0.1)

    def test_exhausted_get_transport_is_redacted(self):
        for failure_type in (OSError, http.client.HTTPException):
            with self.subTest(failure_type=failure_type):
                connections = [Connection(failure_type("fake-token")) for _ in range(3)]
                with patch.object(api.http.client, "HTTPSConnection", side_effect=connections), \
                     patch.object(api.time, "sleep") as sleep:
                    with self.assertRaises(failure_type) as context:
                        client().get("/read?token=fake-token")
                self.assertNotIn("fake-token", str(context.exception))
                self.assertTrue(context.exception.__suppress_context__)
                self.assertEqual(sum(len(conn.requests) for conn in connections), 3)
                self.assertEqual(sleep.call_count, 2)

    def test_get_retries_5xx_and_bounded_failure(self):
        conn = Connection(Response(503, raw=b"unavailable"),
                          Response(200, {"code": 0, "data": {"ok": True}}))
        with patch.object(api.http.client, "HTTPSConnection", return_value=conn), \
             patch.object(api.time, "sleep") as sleep:
            self.assertEqual(client().get("/read"), {"ok": True})
        self.assertEqual(len(conn.requests), 2)
        sleep.assert_called_once()
        conn = Connection(*[Response(503) for _ in range(3)])
        with patch.object(api.http.client, "HTTPSConnection", return_value=conn), \
             patch.object(api.time, "sleep") as sleep:
            with self.assertRaises(api.FeishuError) as context:
                client().get("/read")
        self.assertNotIsInstance(context.exception, api.FeishuUncertainError)
        self.assertEqual(len(conn.requests), 3)
        self.assertEqual(sleep.call_count, 2)

    def test_explicit_retry_false_prevents_every_retry(self):
        for method, response in (("GET", OSError("stale")),
                                 ("GET", Response(503)),
                                 ("POST", Response(429)),
                                 ("POST", Response(200, {"code": 99991400})),
                                 ("POST", OSError("stale"))):
            with self.subTest(method=method, response=type(response).__name__):
                conn = Connection(response)
                with patch.object(api.http.client, "HTTPSConnection", return_value=conn), \
                     patch.object(api.time, "sleep") as sleep:
                    with self.assertRaises((api.FeishuError, OSError)):
                        client().call(method, "/test", retry=False)
                self.assertEqual(len(conn.requests), 1)
                sleep.assert_not_called()

    def test_upload_throttling_reuses_identical_body(self):
        throttles = ((Response(429, raw=b"throttled", headers={"Retry-After": "2"}), 2.0),
                     (Response(200, {"code": 99991400}, headers={"Retry-After": "3"}), 3.0))
        for throttle, delay in throttles:
            with self.subTest(status=throttle.status):
                conn = Connection(throttle, Response(200, {"code": 0, "data": {"file_token": "file-ok"}}))
                c = client()
                with patch.object(api.http.client, "HTTPSConnection", return_value=conn), \
                     patch.object(api.time, "sleep") as sleep:
                    self.assertEqual(c.upload_media(b"bytes", "image.png", "docx_image", "block", timed="upload"), "file-ok")
                sleep.assert_called_once_with(delay)
                self.assertEqual(len(conn.requests), 2)
                self.assertEqual(conn.requests[0], conn.requests[1])
                self.assertIn("upload", c.timings)

    def test_upload_disconnect_and_missing_token_are_uncertain(self):
        for response in (OSError("disconnect"), Response(200, {"code": 0, "data": {}}),
                         Response(200, {"code": 0, "data": ["invalid"]})):
            with self.subTest(response=type(response).__name__):
                conn = Connection(response)
                with patch.object(api.http.client, "HTTPSConnection", return_value=conn), \
                     patch.object(api.time, "sleep") as sleep:
                    with self.assertRaises(api.FeishuUncertainError):
                        client().upload_media(b"bytes", "image.png", "docx_image", "block")
                self.assertEqual(len(conn.requests), 1)
                sleep.assert_not_called()

    def test_concurrent_clients_isolate_connections_headers_and_waits(self):
        barrier = threading.Barrier(2)

        class ConcurrentResponse(Response):
            def read(self):
                barrier.wait(timeout=5)
                return super().read()

        first = Connection(ConcurrentResponse(429, headers={"Retry-After": "2"}),
                           Response(200, {"data": {"ok": True}}))
        second = Connection(ConcurrentResponse(429, headers={"Retry-After": "3"}),
                            Response(200, {"data": {"ok": True}}))
        clients = [client(token="token-one"), client(token="token-two")]
        values, errors, waits = [], [], []

        def run(c):
            try:
                values.append(c.get("/read"))
            except Exception as exc:
                errors.append(exc)

        with patch.object(api.http.client, "HTTPSConnection", side_effect=[first, second]), \
             patch.object(api.time, "sleep", side_effect=lambda delay: waits.append(
                 (threading.current_thread().name, delay))), \
             patch.object(api.random, "uniform", return_value=0):
            threads = [threading.Thread(target=run, args=(c,), name=c.token()) for c in clients]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(values, [{"ok": True}, {"ok": True}])
        self.assertIsNot(clients[0]._conn, clients[1]._conn)
        self.assertCountEqual(waits, [(first.requests[0][3]["Authorization"].split()[-1], 2.0),
                                      (second.requests[0][3]["Authorization"].split()[-1], 3.0)])
        self.assertEqual({first.requests[0][3]["Authorization"], second.requests[0][3]["Authorization"]},
                         {"Bearer token-one", "Bearer token-two"})
        for conn in (first, second):
            self.assertEqual(len(conn.requests), 2)
            self.assertEqual(conn.requests[0][3]["Authorization"], conn.requests[1][3]["Authorization"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
