"""Run the mock app in a background thread (demo/tests) or in the foreground."""

from __future__ import annotations

import threading
import time

import httpx
import uvicorn

from mock_bank.app import create_app


class AppServer:
    def __init__(self, port: int = 8600, tenant: str = "a"):
        self.port = port
        self.tenant = tenant
        self.base_url = f"http://127.0.0.1:{port}"
        config = uvicorn.Config(create_app(tenant), host="127.0.0.1", port=port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> "AppServer":
        self._thread.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                httpx.get(self.base_url + "/signon", timeout=0.5)
                return self
            except httpx.HTTPError:
                time.sleep(0.05)
        raise RuntimeError(f"mock app did not start on {self.base_url}")

    def faults(self, **kw) -> None:
        httpx.post(self.base_url + "/_harness/faults", json=kw).raise_for_status()

    def reset(self) -> None:
        httpx.post(self.base_url + "/_harness/reset").raise_for_status()

    def transfers(self) -> list:
        return httpx.get(self.base_url + "/_harness/transfers").json()

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
