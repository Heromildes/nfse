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


def test_upload_extracts_zip_fallback_stem(client: TestClient, dest_dir: Path) -> None:
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
    # Client HTTP pode normalizar ../../; / restante vira _
    assert response.json()["extractedTo"] == "etc_passwd"
    assert (dest_dir / "etc_passwd").is_dir()
    assert not (dest_dir / "etc").exists()
    assert not (dest_dir / "passwd").exists()


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


def test_upload_structured_layout_with_cnpj_slash(client: TestClient, dest_dir: Path) -> None:
    """Nome com / no CNPJ não cria pasta 0001-23 - 07-2026; vai para codigo - empresa."""
    root = "313 - EMPRESA LTDA - 12.345.678_0001-23"
    entry = f"{root}/2026-07/Saidas/nota.xml"
    data = _zip_bytes(entry, b"<nfse/>")
    filename = "313 - EMPRESA LTDA - 12.345.678/0001-23 - 07-2026.zip"

    response = client.post(
        "/v1/nfse-zips",
        headers=_auth_headers(),
        files={"file": (filename, data, "application/zip")},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["extractedTo"] == root
    assert body["files"] == 1

    client_dir = dest_dir / root
    period_file = client_dir / "07-2026" / "Saidas" / "nota.xml"
    assert period_file.read_bytes() == b"<nfse/>"
    assert not (dest_dir / "0001-23 - 07-2026").exists()
    assert not any(dest_dir.glob("0001-23*"))


def test_upload_structured_layout_with_cnpj_underscore(client: TestClient, dest_dir: Path) -> None:
    root = "042 - SUL BANANAS LTDA - 24.057.467_0001-36"
    entry = f"{root}/2026-07/Entradas/nota.xml"
    data = _zip_bytes(entry, b"<xml/>")
    filename = f"{root} - 07-2026.zip"

    response = client.post(
        "/v1/nfse-zips",
        headers=_auth_headers(),
        files={"file": (filename, data, "application/zip")},
    )
    assert response.status_code == 201
    assert response.json()["extractedTo"] == root
    assert (dest_dir / root / "07-2026" / "Entradas" / "nota.xml").read_bytes() == b"<xml/>"


def test_upload_merges_periods_into_same_client_folder(client: TestClient, dest_dir: Path) -> None:
    root = "313 - EMPRESA LTDA - 12.345.678_0001-23"

    first = _zip_bytes(f"{root}/2026-07/Saidas/a.xml", b"<a/>")
    second = _zip_bytes(f"{root}/2026-08/Saidas/b.xml", b"<b/>")

    r1 = client.post(
        "/v1/nfse-zips",
        headers=_auth_headers(),
        files={"file": (f"{root} - 07-2026.zip", first, "application/zip")},
    )
    r2 = client.post(
        "/v1/nfse-zips",
        headers=_auth_headers(),
        files={"file": (f"{root} - 08-2026.zip", second, "application/zip")},
    )
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["extractedTo"] == root
    assert r2.json()["extractedTo"] == root

    assert (dest_dir / root / "07-2026" / "Saidas" / "a.xml").read_bytes() == b"<a/>"
    assert (dest_dir / root / "08-2026" / "Saidas" / "b.xml").read_bytes() == b"<b/>"
    assert not (dest_dir / f"{root}_1").exists()


def test_upload_same_period_merges_without_suffix_folder(client: TestClient, dest_dir: Path) -> None:
    root = "313 - EMPRESA LTDA - 12.345.678_0001-23"
    first = _zip_bytes(f"{root}/2026-07/Saidas/old.xml", b"<old/>")
    second = _zip_bytes(f"{root}/2026-07/Saidas/new.xml", b"<new/>")
    filename = f"{root} - 07-2026.zip"

    assert (
        client.post(
            "/v1/nfse-zips",
            headers=_auth_headers(),
            files={"file": (filename, first, "application/zip")},
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/v1/nfse-zips",
            headers=_auth_headers(),
            files={"file": (filename, second, "application/zip")},
        ).status_code
        == 201
    )

    period = dest_dir / root / "07-2026" / "Saidas"
    assert (period / "old.xml").read_bytes() == b"<old/>"
    assert (period / "new.xml").read_bytes() == b"<new/>"
    assert not (dest_dir / f"{root}_1").exists()
