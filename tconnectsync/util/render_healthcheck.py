import logging
import os
import threading

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)


class _HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'tconnectsync is running\n')

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args) -> None:
        # BaseHTTPRequestHandler logs every request to stderr by default;
        # silence it so a keep-alive pinger hitting this every few minutes
        # doesn't drown out the actual sync logs.
        pass


def maybe_start_health_server() -> None:
    """On Render's Web Service tier (or any platform that sets $PORT),
    binds a minimal HTTP server that always returns 200.

    Render kills and restarts a container that never binds $PORT, which is
    exactly what happens to a process with no built-in web server -- e.g.
    tconnectsync running --auto-update, or even a single non-interactive
    run that just exits. That restart loop forces a fresh Tandem login on
    every restart, which is the behavior the retry/backoff logic elsewhere
    in this codebase specifically exists to avoid. Render's Background
    Worker service type doesn't have this requirement, but is not on the
    free plan; this lets --auto-update run continuously as a Web Service
    on the free plan instead.

    A no-op if $PORT is not set, which is the case for every other
    deployment method (Pip, Pipenv, Cron, Heroku, plain `docker run`
    without Render).
    """
    port = os.environ.get('PORT')
    if not port:
        return

    try:
        port_num = int(port)
    except ValueError:
        logger.warning("PORT environment variable is not a valid integer: %s" % port)
        return

    server = ThreadingHTTPServer(('0.0.0.0', port_num), _HealthCheckHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Started health-check HTTP server on port %d (binds $PORT for Render Web Service compatibility)" % port_num)
