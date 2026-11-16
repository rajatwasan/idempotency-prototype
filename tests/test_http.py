import json
import threading
import unittest
import urllib.error
import urllib.request
import test_service

try:
    from server import make_server
except ImportError:
    make_server = None


class HTTPTests(unittest.TestCase):
    setUp = test_service.BillingTests.setUp
    # Reuse the real persistent fixture, without inheriting the service cases.
    def test_http_contract(self):
        self.assertIsNotNone(make_server, 'HTTP adapter is not implemented')
        server = make_server(self.app, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f'http://127.0.0.1:{server.server_port}'
        request = urllib.request.Request(url + '/subscriptions', data=json.dumps({'customer': 'alice', 'amount': 1500}).encode(), headers={'Idempotency-Key': 'http-1', 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 202)
            self.assertEqual(json.load(response)['status'], 'pending')
        self.app.drain()
        with urllib.request.urlopen(url + '/entitlements?customer=alice') as response:
            self.assertTrue(json.load(response)['enabled'])
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(urllib.request.Request(url + '/subscriptions', data=b'not json'))
        self.assertEqual(error.exception.code, 400)
