from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from playwright.sync_api import Page, sync_playwright

from models.test_report import ConsoleError


class BrowserSession:
    """
    Owns a single Playwright browser session + page.

    Requirement: "use a single browser session per test run".
    """

    def __init__(self, headless: bool, artifacts_dir: str | Path, run_id: str):
        self.headless = headless
        self.artifacts_dir = Path(artifacts_dir)
        self.run_id = run_id

        self._playwright = None
        self.page: Optional[Page] = None

        self._console_errors: list[ConsoleError] = []

    def __enter__(self) -> "BrowserSession":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    @property
    def console_errors(self) -> list[ConsoleError]:
        return list(self._console_errors)

    def start(self) -> None:
        self._playwright = sync_playwright().start()
        browser = self._playwright.chromium.launch(headless=self.headless)
        context = browser.new_context()
        page = context.new_page()

        # Keep references to close them later.
        self._browser = browser
        self._context = context
        self.page = page

        # Capture console errors.
        page.on("console", self._on_console)
        page.on("pageerror", self._on_pageerror)

    def stop(self) -> None:
        try:
            if getattr(self, "page", None) is not None:
                # No-op; page will be closed when context is closed.
                pass
            if getattr(self, "_context", None) is not None:
                self._context.close()
            if getattr(self, "_browser", None) is not None:
                self._browser.close()
        finally:
            if self._playwright is not None:
                self._playwright.stop()

    def _on_console(self, msg) -> None:
        # msg.type is typically: "log", "debug", "info", "warning", "error"
        if msg.type == "error":
            location = None
            try:
                if msg.location:
                    location = f"{msg.location.get('url')}:{msg.location.get('lineNumber')}:{msg.location.get('columnNumber')}"
            except Exception:
                location = None

            self._console_errors.append(
                ConsoleError(
                    kind="console_error",
                    message=msg.text,
                    location=location,
                    page_url=self.page.url if self.page else None,
                )
            )

    def _on_pageerror(self, err) -> None:
        self._console_errors.append(
            ConsoleError(
                kind="page_error",
                message=str(err),
                location=None,
                page_url=self.page.url if self.page else None,
            )
        )

