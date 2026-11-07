from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zipfile

import httpx
from vouch.utils import get_platform

OBSCURA_VERSION = "v0.2.6"
OBSCURA_VERSION_NUMBER = "0.2.6"

OBSCURA_RELEASE_URL = (
    "https://github.com/h4ckf0r0day/obscura"
    "/releases/download"
)

@dataclass(frozen=True)
class ReleaseAsset:
    filename: str
    sha256: str
    binary: str
    worker: str

ASSETS = {
    ("linux", "x86_64"): ReleaseAsset(
        "obscura-x86_64-linux.tar.gz",
        "eed986e478c5ec5439f531f2d7fb6b0e"
        "36a356f1df7a6c44a8e8fd4f2486bd76",
        "obscura",
        "obscura-worker",
    ),

    ("linux", "aarch64"): ReleaseAsset(
        "obscura-aarch64-linux.tar.gz",
        "e12aff8bbe40326386a471060083e9ce"
        "c5255b67048996b84f649efef10cac39",
        "obscura",
        "obscura-worker",
    ),

    ("darwin", "x86_64"): ReleaseAsset(
        "obscura-x86_64-macos.tar.gz",
        "897f03df4e3d704f946c5e14c6a90a34"
        "e0d4e4ef898c4b3201771712962cd7fb",
        "obscura",
        "obscura-worker",
    ),

    ("darwin", "aarch64"): ReleaseAsset(
        "obscura-aarch64-macos.tar.gz",
        "19faa248ac762f466ddb822db03ed4f0"
        "976312d10d1a4d5b2143e6040a9bebd0",
        "obscura",
        "obscura-worker",
    ),

    ("windows", "x86_64"): ReleaseAsset(
        "obscura-x86_64-windows.zip",
        "0a110d0693753a252ed7ffd61e682363"
        "e8622caf79697f564c529099a31a6242",
        "obscura.exe",
        "obscura-worker.exe",
    ),
}

def get_install_dir(os_name: str, arch: str) -> Path:
    override = os.getenv("VOUCH_OBSCURA_HOME")

    if override:
        base = Path(override).expanduser()

    elif os_name == "windows":
        base = Path(
            os.getenv("LOCALAPPDATA", Path.home() / "AppData/Local")
        ) / "vouch" / "obscura"

    elif os_name == "darwin":
        base = Path.home() / "Library" / "Caches" / "vouch" / "obscura"

    else:
        base = Path(
            os.getenv("XDG_CACHE_HOME", Path.home() / ".cache")
        ) / "vouch" / "obscura"

    return base / OBSCURA_VERSION / f"{os_name}-{arch}"

def download_archive(url: str, destination: Path) -> None:
    headers={"User-Agent": "vouch-obscura-bootstrap"}

    with httpx.stream("GET", url, headers=headers, timeout=60, follow_redirects=True) as response:
        response.raise_for_status()
        with destination.open("wb") as file:
            for chunk in response.iter_bytes():
                file.write(chunk)

def calculate_sha256(path: Path) -> str:
    digest = sha256()

    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024*1024), b""):
            digest.update(chunk)

    return digest.hexdigest()

def verify_archive(path: Path, expected: str) -> None:
    actual = calculate_sha256(path)

    if actual != expected:
        raise RuntimeError(
            f"Obscura checksum mismatch: "
            f"expected {expected}, got {actual}"
        )

def extract_archive(archive: Path, destination: Path, asset: ReleaseAsset, ) -> None:
    expected = {asset.binary, asset.worker}
    extracted = set()

    def copy_member(source, name: str):
        if name in extracted:
            raise RuntimeError(f"Duplicate archive member: {name}")

        with (destination / name).open("wb") as output:
            shutil.copyfileobj(source, output)

        extracted.add(name)

    if archive.name.endswith(".tar.gz"):
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                name = PurePosixPath(member.name).name

                if name not in expected:
                    continue

                if not member.isfile():
                    raise RuntimeError(f"Invalid archive member: {name}")

                source = tar.extractfile(member)

                if source is None:
                    raise RuntimeError(f"Cannot read archive member: {name}")

                with source:
                    copy_member(source, name)

    elif archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zipped:
            for member in zipped.infolist():
                name = PurePosixPath(member.filename).name

                if name not in expected:
                    continue

                mode = (member.external_attr >> 16) & 0o170000

                if member.is_dir() or mode == stat.S_IFLNK:
                    raise RuntimeError(f"Invalid archive member: {name}")

                with zipped.open(member) as source:
                    copy_member(source, name)

    if extracted != expected:
        raise RuntimeError(
            f"Incomplete Obscura archive. "
            f"Missing: {expected - extracted}"
        )

def verify_executable(binary: Path, expected_version: str | None = None) -> None:
    if not binary.is_file:
        raise RuntimeError(f"Obscura executable missing: {binary}")

    result = subprocess.run(
        [str(binary), "--version"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    output = result.stdout + result.stderr

    if result.returncode != 0:
        raise RuntimeError(f"Obscura failed to start: {output}")

    if expected_version and expected_version not in output:
        raise RuntimeError(
            f"Unexpected Obscura version: {output}"
        )

def ensure_obscura() -> Path:
    """Ensure obscura is installed and return it's executable path."""

    platform_key = get_platform()
    if platform_key not in ASSETS:
        raise RuntimeError(f"Unsupported platform: {platform_key}")

    asset = ASSETS[platform_key]

    install_dir = get_install_dir(*platform_key)
    binary = install_dir / asset.binary
    worker = install_dir / asset.worker

    # if already installed
    if binary.is_file() and worker.is_file():
        try:
            verify_executable(binary, OBSCURA_VERSION_NUMBER)
            return binary
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            pass

    install_dir.parent.mkdir(parents=True, exist_ok=True)

    # Remove an incomplete previous installation.
    if install_dir.exists():
        if install_dir.is_dir():
            shutil.rmtree(install_dir)
        else:
            install_dir.unlink()

    with tempfile.TemporaryDirectory(prefix=".obscura-install-", dir=install_dir.parent) as temporary:
        temporary = Path(temporary)

        archive = temporary / asset.filename
        staging = temporary / "payload"
        staging.mkdir()

        url = f"{OBSCURA_RELEASE_URL}/{OBSCURA_VERSION}/{asset.filename}"

        download_archive(url, archive)
        verify_archive(archive, asset.sha256)

        extract_archive(archive, staging, asset)

        # Unix executables need executable permissions.
        if platform_key[0] != "windows":
            for name in (asset.binary, asset.worker):
                (staging / name).chmod(0o755)

        # Validate before installation.
        verify_executable(
            staging / asset.binary,
            OBSCURA_VERSION_NUMBER,
        )

        try:
            # Atomic directory installation.
            os.replace(staging, install_dir)
        except OSError:
            # Another application worker may have installed it.
            if not (
                binary.is_file()
                and worker.is_file()
            ):
                raise

            verify_executable(binary, OBSCURA_VERSION_NUMBER)

    verify_executable(binary, OBSCURA_VERSION_NUMBER)

    return binary
