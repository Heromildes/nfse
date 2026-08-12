#!/usr/bin/env python3
"""API local — recebe ZIP NFS-e do hub e grava na pasta de controle."""

from __future__ import annotations

import logging
import os
import re
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse

DEFAULT_DEST = (
    r"S:\Contabilidade\Privado\Planilhas de Controle"
    r"\Planilha Controle 2026\Controle NFS-e"
)
DEFAULT_ORIGINS = (
    "https://hub.silveirasoares.com.br,"
    "https://63qe0.hatchboxapp.com"
)
UNSAFE_NAME = re.compile(r"[^\w.\- ]+", re.UNICODE)

logger = logging.getLogger("nfse_zip_receiver")


@dataclass(frozen=True)
class Settings:
    api_key: str
    dest_dir: Path
    allowed_origins: frozenset[str]
    max_upload_bytes: int


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or not str(value).strip():
        raise RuntimeError(f"Variável de ambiente obrigatória: {name}")
    return str(value).strip()


def load_settings() -> Settings:
    raw_origins = os.environ.get("NFSE_ALLOWED_ORIGINS", DEFAULT_ORIGINS)
    origins = frozenset(
        o.strip().rstrip("/")
        for o in raw_origins.split(",")
        if o.strip()
    )
    max_mb = float(os.environ.get("NFSE_MAX_UPLOAD_MB", "50"))
    return Settings(
        api_key=_env("NFSE_RECEIVER_API_KEY"),
        dest_dir=Path(os.environ.get("NFSE_DEST_DIR", DEFAULT_DEST)),
        allowed_origins=origins,
        max_upload_bytes=int(max_mb * 1024 * 1024),
    )


settings = load_settings() if os.environ.get("NFSE_RECEIVER_API_KEY") else None


def reload_settings() -> Settings:
    global settings
    settings = load_settings()
    return settings


def require_settings() -> Settings:
    if settings is None:
        reload_settings()
    assert settings is not None
    return settings


def setup_logging() -> None:
    if logger.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def sanitize_filename(raw: str | None) -> str:
    name = Path(raw or "nfse.zip").name
    name = name.replace("\\", "/").split("/")[-1]
    name = UNSAFE_NAME.sub("_", name).strip(" ._") or "nfse.zip"
    if not name.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Apenas arquivos .zip são aceitos.")
    return name


def unique_path(directory: Path, filename: str) -> Path:
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    index = 1
    while True:
        candidate = directory / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def origin_from_headers(origin: str | None, referer: str | None) -> str | None:
    if origin and origin.strip():
        return origin.strip().rstrip("/")
    if referer and referer.strip():
        parsed = urlparse(referer.strip())
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    return None


def verify_auth(authorization: str | None, cfg: Settings) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Não autorizado.")
    token = authorization.removeprefix("Bearer ").strip()
    if not token or not secrets.compare_digest(token, cfg.api_key):
        raise HTTPException(status_code=401, detail="Não autorizado.")


def verify_origin(origin: str | None, referer: str | None, cfg: Settings) -> None:
    resolved = origin_from_headers(origin, referer)
    if resolved is None or resolved not in cfg.allowed_origins:
        raise HTTPException(status_code=403, detail="Origem não permitida.")


setup_logging()
app = FastAPI(title="NFS-e ZIP Receiver", docs_url=None, redoc_url=None)


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.post("/v1/nfse-zips")
async def receive_zip(
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
    origin: str | None = Header(default=None),
    referer: str | None = Header(default=None),
) -> JSONResponse:
    cfg = require_settings()
    verify_auth(authorization, cfg)
    verify_origin(origin, referer, cfg)

    filename = sanitize_filename(file.filename)
    content_type = (file.content_type or "").lower()
    if content_type and content_type not in {
        "application/zip",
        "application/x-zip-compressed",
        "application/octet-stream",
    }:
        raise HTTPException(status_code=400, detail="Content-Type inválido.")

    data = await file.read()
    if len(data) > cfg.max_upload_bytes:
        raise HTTPException(status_code=413, detail="Arquivo excede o tamanho máximo.")
    if not data:
        raise HTTPException(status_code=400, detail="Arquivo vazio.")

    cfg.dest_dir.mkdir(parents=True, exist_ok=True)
    target = unique_path(cfg.dest_dir, filename)
    target.write_bytes(data)

    logger.info("ZIP salvo: %s (%s bytes)", target.name, len(data))
    return JSONResponse(
        status_code=201,
        content={"savedAs": target.name, "path": str(target)},
    )


def main() -> None:
    import uvicorn

    cfg = require_settings()
    host = os.environ.get("NFSE_RECEIVER_HOST", "0.0.0.0").strip() or "0.0.0.0"
    port = int(os.environ.get("NFSE_RECEIVER_PORT", "8787"))
    logger.info("Destino: %s", cfg.dest_dir)
    logger.info("Listening %s:%s", host, port)
    uvicorn.run("app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
