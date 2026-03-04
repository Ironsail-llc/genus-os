#!/usr/bin/env python3
"""Simple OAuth callback server"""

import http.server
import urllib.parse


class OAuthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "code" in params:
            code = params["code"][0]
            # Save code to file
            with open("/tmp/oauth_code.txt", "w") as f:
                f.write(code)

            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"""
                <html><body style="font-family: sans-serif; text-align: center; padding: 50px;">
                <h1>Authorization Successful!</h1>
                <p>You can close this window.</p>
                <p style="color: #666; font-size: 12px;">Code received and saved.</p>
                </body></html>
            """)
            print(f"\n✅ Code received: {code[:20]}...")
        else:
            self.send_response(400)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"No code in request")

    def log_message(self, format, *args):
        pass  # Suppress logs


if __name__ == "__main__":
    server = http.server.HTTPServer(("0.0.0.0", 8085), OAuthHandler)
    print("OAuth callback server running on port 8085...")
    print("Waiting for callback...")
    server.handle_request()  # Handle one request then exit
    print("Server stopped.")
