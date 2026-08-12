"""TDD: API local de recebimento de ZIP NFS-e."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as receiver


API_KEY = "test-secret-key"
ALLOWED_ORIGIN = "https://hub.silveirasoares.com.br"


@pytest.fixture()
def dest_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("NFSE_RECEIVER_API_KEY", API_KEY)
    monkeypatch.setenv("NFSE_DEST_DIR", str(tmp_path))
    monkeypatch.setenv(
        "NFSE_ALLOWED_ORIGINS",
        f"{ALLOWED_ORIGIN},https://63qe0.hatchboxapp.com",
    )
    monkeypatch.setenv("NFSE_MAX_UPLOAD_MB", "1")
    receiver.reload_settings()
    return tmp_path


@pytest.fixture()
def client(dest_dir: Path) -> TestClient:
    return TestClient(receiver.app)


def _zip_bytes(name: str = "nota.xml", content: bytes = b"<xml/>") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, content)
    return buf.getvalue()


def _auth_headers(origin: str = ALLOWED_ORIGIN, key: str = API_KEY) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {key}",
        "Origin": origin,
    }


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_upload_extracts_zip(client: TestClient, dest_dir: Path) -> None:
    data = _zip_bytes("nota.xml", b"<xml/>")
    response = client.post(
        "/v1/nfse-zips",
        headers=_auth_headers(),
        files={"file": ("cliente.zip", data, "application/zip")},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["extractedTo"] == "cliente"
    assert body["files"] == 1
    extracted = dest_dir / "cliente"
    assert extracted.is_dir()
    assert (extracted / "nota.xml").read_bytes() == b"<xml/>"
    assert not (dest_dir / "cliente.zip").exists()
    assert Path(body["path"]) == extracted


def test_upload_rejects_missing_api_key(client: TestClient) -> None:
    response = client.post(
        "/v1/nfse-zips",
        headers={"Origin": ALLOWED_ORIGIN},
        files={"file": ("a.zip", _zip_bytes(), "application/zip")},
    )
    assert response.status_code == 401


def test_upload_rejects_wrong_api_key(client: TestClient) -> None:
    response = client.post(
        "/v1/nfse-zips",
        headers=_auth_headers(key="wrong"),
        files={"file": ("a.zip", _zip_bytes(), "application/zip")},
    )
    assert response.status_code == 401


def test_upload_rejects_disallowed_origin(client: TestClient) -> None:
    response = client.post(
        "/v1/nfse-zips",
        headers=_auth_headers(origin="https://evil.example.com"),
        files={"file": ("a.zip", _zip_bytes(), "application/zip")},
    )
    assert response.status_code == 403


def test_upload_accepts_referer_when_origin_missing(client: TestClient, dest_dir: Path) -> None:
    response = client.post(
        "/v1/nfse-zips",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Referer": f"{ALLOWED_ORIGIN}/gerar-nfse",
        },
        files={"file": ("via-referer.zip", _zip_bytes(), "application/zip")},
    )
    assert response.status_code == 201
    assert (dest_dir / "via-referer").is_dir()


def test_upload_rejects_non_zip_extension(client: TestClient) -> None:
    response = client.post(
        "/v1/nfse-zips",
        headers=_auth_headers(),
        files={"file": ("nota.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 400


def test_upload_sanitizes_path_traversal(client: TestClient, dest_dir: Path) -> None:
    response = client.post(
        "/v1/nfse-zips",
        headers=_auth_headers(),
        files={"file": ("../../etc/passwd.zip", _zip_bytes(), "application/zip")},
    )
    assert response.status_code == 201
    assert response.json()["extractedTo"] == "passwd"
    assert (dest_dir / "passwd").is_dir()
    assert not (dest_dir / "etc").exists()


def test_upload_does_not_overwrite_extract_dir(client: TestClient, dest_dir: Path) -> None:
    first = _zip_bytes(content=b"one")
    second = _zip_bytes(content=b"two")
    client.post(
        "/v1/nfse-zips",
        headers=_auth_headers(),
        files={"file": ("mesmo.zip", first, "application/zip")},
    )
    response = client.post(
        "/v1/nfse-zips",
        headers=_auth_headers(),
        files={"file": ("mesmo.zip", second, "application/zip")},
    )
    assert response.status_code == 201
    assert response.json()["extractedTo"] == "mesmo_1"
    assert (dest_dir / "mesmo" / "nota.xml").read_bytes() == b"one"
    assert (dest_dir / "mesmo_1" / "nota.xml").read_bytes() == b"two"


def test_upload_rejects_oversize(client: TestClient) -> None:
    # NFSE_MAX_UPLOAD_MB=1 → 1 MiB
    big = b"0" * (1 * 1024 * 1024 + 1)
    response = client.post(
        "/v1/nfse-zips",
        headers=_auth_headers(),
        files={"file": ("grande.zip", big, "application/zip")},
    )
    assert response.status_code == 413


def test_upload_rejects_invalid_zip(client: TestClient) -> None:
    response = client.post(
        "/v1/nfse-zips",
        headers=_auth_headers(),
        files={"file": ("ruim.zip", b"not-a-zip", "application/zip")},
    )
    assert response.status_code == 400


def test_upload_rejects_zip_slip(client: TestClient, dest_dir: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.txt", b"x")
    response = client.post(
        "/v1/nfse-zips",
        headers=_auth_headers(),
        files={"file": ("slip.zip", buf.getvalue(), "application/zip")},
    )
    assert response.status_code == 400
    assert not (dest_dir / "evil.txt").exists()
