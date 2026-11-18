
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import urllib.request
import time

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
port = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
url = f'http://127.0.0.1:{port}/'

req = urllib.request.Request(url, data=b"{}", method='POST')
with urllib.request.urlopen(req) as r:
    print(f"Status: {r.status}")
httpd.shutdown()
httpd.server_close()
