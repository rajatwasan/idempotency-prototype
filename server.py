"""Loopback demonstration HTTP adapter; worker runs explicitly via CLI."""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from billing import Billing, Conflict, Provider


def make_server(app, port=8080):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path != '/entitlements':
                return self.reply(404, {'error': 'not found'})
            customer = parse_qs(parsed.query).get('customer', [''])[0]
            self.reply(200, {'customer': customer, 'enabled': app.entitled(customer)})

        def do_POST(self):
            if self.path != '/subscriptions':
                return self.reply(404, {'error': 'not found'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 16384:
                    raise ValueError('body must be 1..16384 bytes')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('expected JSON object')
                result = app.subscribe(self.headers.get('Idempotency-Key'), body.get('customer'), body.get('amount'))
                self.reply(202, result)
            except Conflict as exc:
                self.reply(409, {'error': str(exc)})
            except (ValueError, UnicodeError) as exc:
                self.reply(400, {'error': str(exc)})
    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['serve', 'drain', 'reconcile'])
    parser.add_argument('--db', default='billing.db')
    parser.add_argument('--provider-db', default='provider.db')
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()
    app = Billing(args.db, Provider(args.provider_db))
    if args.command == 'serve':
        print(f'Listening on http://127.0.0.1:{args.port}', flush=True)
        try:
            make_server(app, args.port).serve_forever()
        except KeyboardInterrupt:
            pass
    else:
        print(getattr(app, args.command)())
