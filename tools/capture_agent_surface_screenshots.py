"""Capture real 1280x900 viewer screenshots for the four recorded agent surfaces."""

from __future__ import annotations

import argparse
import json
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import uvicorn
from playwright.sync_api import expect, sync_playwright

from telco_iq.api import create_app
from telco_iq.demo_library import RecordedAgentSurface

try:
    from .build_agent_surface_captures import build_agent_surfaces, surface_filename
except ImportError:
    from build_agent_surface_captures import build_agent_surfaces, surface_filename

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECORDINGS_DIR = ROOT / "recordings"
DEFAULT_IMAGES_DIR = ROOT / "docs" / "agents" / "images"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MIN_PNG_BYTES = 20_480


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _serve_surface(recording_path: Path):
    port = _free_port()
    app = create_app(ROOT, local_port=port, recorded_library=recording_path)
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            try:
                resp = httpx.get(f"{base_url}/api/health", timeout=1.0)
                if resp.status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            raise TimeoutError(f"Timed out waiting for temporary uvicorn server on {base_url}")
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)


def capture_screenshots(
    recordings_dir: Path = DEFAULT_RECORDINGS_DIR,
    images_dir: Path = DEFAULT_IMAGES_DIR,
) -> list[dict[str, object]]:
    images_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, object]] = []

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
        except Exception:
            browser = playwright.chromium.launch(headless=True)
        try:
            for expected in build_agent_surfaces():
                recording_path = recordings_dir / surface_filename(expected.surface_id)
                loaded = RecordedAgentSurface.read(recording_path)
                out_png = images_dir / f"{loaded.surface_id}.png"
                page_errors: list[str] = []

                with _serve_surface(recording_path) as base_url:
                    page = browser.new_page(
                        viewport={"width": 1280, "height": 900},
                        device_scale_factor=1,
                    )
                    page.on("pageerror", lambda exc, errs=page_errors: errs.append(str(exc)))
                    page.goto(base_url, wait_until="networkidle")
                    expect(page.get_by_role("heading", name=loaded.surface_name, level=1)).to_be_visible()
                    expect(page.locator('[data-turn-level="L2"]')).to_be_visible()
                    expect(page.locator('[data-turn-level="L4"]')).to_be_visible()
                    page.screenshot(path=str(out_png), full_page=False)
                    page.close()

                if page_errors:
                    raise RuntimeError(f"Browser page error on {loaded.surface_id}: {page_errors}")
                raw = out_png.read_bytes()
                if not raw.startswith(PNG_MAGIC):
                    raise RuntimeError(f"Invalid PNG header in {out_png}")
                if len(raw) < MIN_PNG_BYTES:
                    raise RuntimeError(f"Screenshot unexpectedly small ({len(raw)} bytes): {out_png}")
                results.append(
                    {
                        "surface_id": loaded.surface_id,
                        "image": out_png.relative_to(ROOT).as_posix(),
                        "bytes": len(raw),
                    }
                )
        finally:
            browser.close()

    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recordings-dir", type=Path, default=DEFAULT_RECORDINGS_DIR)
    parser.add_argument("--images-dir", type=Path, default=DEFAULT_IMAGES_DIR)
    args = parser.parse_args(argv)
    captured = capture_screenshots(recordings_dir=args.recordings_dir, images_dir=args.images_dir)
    print(json.dumps({"status": "SCREENSHOTS_CAPTURED", "images": captured}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
