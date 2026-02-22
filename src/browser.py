from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from .config import SCHOLAR_BROWSER_HEADLESS

_log = logging.getLogger("CiteForge.browser")

# Graceful import: nodriver may not be installed
try:
    import nodriver as uc

    NODRIVER_AVAILABLE = True
except ImportError:
    uc = None  # type: ignore[assignment]
    NODRIVER_AVAILABLE = False


class ScholarBrowserLoop:
    """Thread-safe singleton managing a background asyncio event loop and a shared nodriver browser.

    All Scholar browser requests are serialized through a single asyncio event loop
    running in a background daemon thread. Worker threads submit coroutines via
    ``run()`` which blocks until the coroutine completes.
    """

    _instance: ScholarBrowserLoop | None = None
    _init_lock = threading.Lock()

    _loop: asyncio.AbstractEventLoop | None
    _thread: threading.Thread | None
    _browser: Any
    _browser_lock: asyncio.Lock | None
    _closed: bool

    def __new__(cls) -> ScholarBrowserLoop:
        with cls._init_lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._loop = None
                inst._thread = None
                inst._browser = None
                inst._browser_lock = None
                inst._closed = False
                cls._instance = inst
            return cls._instance

    # ------------------------------------------------------------------
    # Event loop lifecycle
    # ------------------------------------------------------------------

    def _start_loop(self) -> None:
        """Spin up a background daemon thread hosting an asyncio event loop."""
        if self._loop is not None and self._loop.is_running():
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name="scholar-browser-loop")
        self._thread.start()

    # ------------------------------------------------------------------
    # Browser lifecycle (runs inside the event loop thread)
    # ------------------------------------------------------------------

    async def get_browser(self) -> Any:
        """Lazily launch the nodriver browser on first use (called from within the event loop)."""
        if self._browser is not None:
            return self._browser
        # Create lock lazily inside the event loop thread to avoid cross-loop binding
        if self._browser_lock is None:
            self._browser_lock = asyncio.Lock()
        async with self._browser_lock:
            if self._browser is not None:
                return self._browser
            if not NODRIVER_AVAILABLE:
                raise RuntimeError("nodriver is not installed")
            _log.info("Launching headless browser (nodriver)...")
            self._browser = await uc.start(headless=SCHOLAR_BROWSER_HEADLESS)  # type: ignore[union-attr]
            _log.info("Browser launched successfully")
            return self._browser

    # ------------------------------------------------------------------
    # Sync-async bridge (called from worker threads)
    # ------------------------------------------------------------------

    def run(self, coro: Any) -> Any:
        """Submit an async coroutine to the event loop and block until it completes.

        This is the primary interface for worker threads to execute browser operations.
        The single event loop naturally serializes all Scholar requests.
        """
        if not NODRIVER_AVAILABLE:
            raise RuntimeError("nodriver is not installed")
        with self._init_lock:
            if self._closed:
                raise RuntimeError("ScholarBrowserLoop is closed")
            if self._loop is None or not self._loop.is_running():
                self._start_loop()
            assert self._loop is not None  # guaranteed by _start_loop
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=120)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Shutdown the browser and stop the event loop. Safe to call multiple times."""
        with self._init_lock:
            if self._closed:
                return
            self._closed = True

        loop = self._loop
        if loop is not None and loop.is_running():
            if self._browser is not None:
                try:
                    future = asyncio.run_coroutine_threadsafe(self._stop_browser(), loop)
                    future.result(timeout=10)
                except Exception as e:
                    _log.warning("Browser shutdown error (non-fatal): %s", e)

            loop.call_soon_threadsafe(loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=5)

        self._browser = None
        self._browser_lock = None
        self._loop = None
        self._thread = None

        with self._init_lock:
            type(self)._instance = None

        _log.info("Browser loop shut down")

    async def _stop_browser(self) -> None:
        """Stop the browser instance. Called within the event loop."""
        if self._browser is not None:
            try:
                await asyncio.to_thread(self._browser.stop)
            except Exception as exc:
                _log.debug("Browser stop error (non-fatal): %s", exc)
