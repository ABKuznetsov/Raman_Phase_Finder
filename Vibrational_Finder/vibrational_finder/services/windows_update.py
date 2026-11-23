from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re
import ssl
from tempfile import NamedTemporaryFile
from time import monotonic
from typing import Callable
from threading import Event
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
from urllib.parse import urlsplit

import certifi

from vibrational_finder.metadata import APP_VERSION, GITHUB_URL


_ASSET_HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
_MAX_SIZE = 256 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class WindowsInstaller:
    name: str
    url: str
    sha256: str
    size_bytes: int


def windows_release_installer(assets: object, version: str) -> WindowsInstaller | None:
    if not isinstance(assets, list):
        return None
    name = f"Raman_Phase_Finder_Setup_{version}.exe"
    url = f"{GITHUB_URL}/releases/download/v{version}/{name}"
    for asset in assets:
        if not isinstance(asset, dict) or (
            asset.get("type"), asset.get("platform"), asset.get("name"), asset.get("url")
        ) != ("installer", "windows", name, url):
            continue
        checksum, size = asset.get("sha256"), asset.get("size_bytes")
        if isinstance(checksum, str) and re.fullmatch(r"[0-9a-fA-F]{64}", checksum):
            if type(size) is int and 2 <= size <= _MAX_SIZE:
                return WindowsInstaller(name, url, checksum.lower(), size)
    return None


class _GitHubAssetRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        address = urlsplit(newurl)
        if address.scheme != "https" or address.netloc not in _ASSET_HOSTS:
            raise ValueError("The download redirected outside GitHub release storage.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_asset(url: str):
    tls = ssl.create_default_context(cafile=certifi.where())
    opener = build_opener(HTTPSHandler(context=tls), _GitHubAssetRedirect())
    return opener.open(Request(url, headers={"User-Agent": f"Raman-Phase-Finder/{APP_VERSION}"}), timeout=20)


def download_installer(
    asset: WindowsInstaller,
    directory: Path,
    progress: Callable[[int], None],
    cancelled: Event,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / asset.name
    deadline = monotonic() + 300
    digest = sha256()
    received = 0
    staged: Path | None = None
    try:
        if cancelled.is_set():
            raise InterruptedError("Update download cancelled.")
        with NamedTemporaryFile(dir=directory, prefix=".raman-", suffix=".part", delete=False) as output:
            staged = Path(output.name)
            with _open_asset(asset.url) as reply:
                while True:
                    if cancelled.is_set():
                        raise InterruptedError("Update download cancelled.")
                    if monotonic() >= deadline:
                        raise TimeoutError("The installer download took too long.")
                    block = reply.read(128 * 1024)
                    if not block:
                        break
                    if received == 0 and not block.startswith(b"MZ"):
                        raise ValueError("The download is not a Windows installer.")
                    received += len(block)
                    if received > asset.size_bytes:
                        raise ValueError("The installer size does not match the update manifest.")
                    output.write(block)
                    digest.update(block)
                    progress(min(99, received * 100 // asset.size_bytes))
        if received != asset.size_bytes or digest.hexdigest() != asset.sha256:
            raise ValueError("The installer size or SHA-256 does not match the update manifest.")
        if cancelled.is_set():
            raise InterruptedError("Update download cancelled.")
        staged.replace(target)
        progress(100)
        return target
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
