import socket
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Hello")

def start_server(addr, port):
    httpd = HTTPServer((addr, port), Handler)
    print(f"Listening on {addr}:{port}")
    httpd.serve_forever()

try:
    threading.Thread(target=start_server, args=('127.0.0.1', 8888), daemon=True).start()
    threading.Thread(target=start_server, args=('192.168.1.33', 8888), daemon=True).start()
except Exception as e:
    print(f"Error: {e}")

import time
time.sleep(1)
