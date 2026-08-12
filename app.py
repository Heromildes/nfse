#!/usr/bin/env python3
"""API local — recebe ZIP NFS-e do hub e extrai na pasta de controle."""

from __future__ import annotations

import io
import logging
import os
import re
import secrets
import sys
import zipfile
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

# macOS/browsers e o hub podem usar / ou _ no CNPJ do nome do arquivo
CNPJ_IN_FILENAME = r"\d{2}\.\d{3}\.\d{3}[/_]\d{4}-\d{2}"
CNPJ_PATTERN = re.compile(CNPJ_IN_FILENAME)
MONTH_FOLDER_PATTERN = re.compile(r"^\d{4}-\d{2}$")
ZIP_WITH_PERIOD = re.compile(
    rf"^(?P<codigo>.+?) - (?P<razao>.+) - (?P<cnpj>{CNPJ_IN_FILENAME}) - (?P<periodo>.+)\.zip$",
    re.IGNORECASE,
)
ZIP_LEGACY = re.compile(
    rf"^(?P<codigo>.+?) - (?P<razao>.+) - (?P<cnpj>{CNPJ_IN_FILENAME})\.zip$",
    re.IGNORECASE,
)
MAC_DUPLICATE_SUFFIX = re.compile(r"(_\d{8}_\d{6})+$")
BROWSER_COPY_SUFFIX = re.compile(r"\s*\(\d+\)\s*$")
PERIOD_MONTH_YEAR = re.compile(r"^\d{2}-\d{4}$")
PERIOD_RANGE = re.compile(r"^\d{2}-\d{2}-\d{4} a \d{2}-\d{2}-\d{4}$")

logger = logging.getLogger("nfse_zip_receiver")


@dataclass(frozen=True)
class Settings:
    api_key: str
    dest_dir: Path
    allowed_origins: frozenset[str]
    max_upload_bytes: int


@dataclass(frozen=True)
class NfseZipMeta:
    codigo: str
    razao: str
    cnpj: str
    periodo: str

    @property
    def client_folder(self) -> str:
        return f"{self.codigo} - {self.razao} - {self.cnpj}"


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
    # Substitui separadores antes de Path.name — senão "…678/0001-23 - 07-2026.zip" vira só o sufixo
    name = (raw or "nfse.zip").replace("\\", "_").replace("/", "_")
    name = Path(name).name
    name = UNSAFE_NAME.sub("_", name).strip(" ._") or "nfse.zip"
    if not name.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Apenas arquivos .zip são aceitos.")
    return name


def normalize_cnpj_folder(cnpj: str) -> str:
    # `_` no disco — `/` vira path separator no Windows/macOS
    return re.sub(r"(\d{2}\.\d{3}\.\d{3})[/_](\d{4}-\d{2})", r"\1_\2", cnpj.strip())


def normalize_periodo(raw: str) -> str:
    periodo = raw.strip().removesuffix(".zip").strip()
    periodo = BROWSER_COPY_SUFFIX.sub("", periodo).strip()
    periodo = MAC_DUPLICATE_SUFFIX.sub("", periodo).strip()

    if PERIOD_MONTH_YEAR.match(periodo) or PERIOD_RANGE.match(periodo):
        return periodo
    if periodo.lower() == "incremental":
        return "Incremental"

    month_year = re.match(r"^(\d{2}-\d{4})", periodo)
    if month_year:
        return month_year.group(1)

    if " a " in periodo:
        range_part = periodo.split("_")[0].strip()
        if PERIOD_RANGE.match(range_part):
            return range_part

    if "_" in periodo:
        head = periodo.split("_")[0].strip()
        if PERIOD_MONTH_YEAR.match(head):
            return head

    return "Incremental"


def parse_zip_filename(name: str) -> NfseZipMeta | None:
    # Normaliza / do CNPJ antes do basename — senão Path corta em 0001-XX
    stem = re.sub(
        r"(\d{2}\.\d{3}\.\d{3})/(\d{4}-\d{2})",
        r"\1_\2",
        (name or "").strip(),
    )
    stem = stem.replace("\\", "/")
    if "/" in stem:
        stem = stem.split("/")[-1]
    if not stem.lower().endswith(".zip"):
        return None

    match = ZIP_WITH_PERIOD.match(stem)
    if match:
        return NfseZipMeta(
            codigo=match.group("codigo").strip(),
            razao=match.group("razao").strip(),
            cnpj=normalize_cnpj_folder(match.group("cnpj")),
            periodo=normalize_periodo(match.group("periodo")),
        )

    match = ZIP_LEGACY.match(stem)
    if match:
        return NfseZipMeta(
            codigo=match.group("codigo").strip(),
            razao=match.group("razao").strip(),
            cnpj=normalize_cnpj_folder(match.group("cnpj")),
            periodo="Incremental",
        )

    # Após sanitize o CNPJ já vem com `_`; ainda assim cobre upload cru com `/`
    cnpj_match = CNPJ_PATTERN.search(stem)
    if not cnpj_match:
        return None

    cnpj = normalize_cnpj_folder(cnpj_match.group(0))
    before = stem[: cnpj_match.start()].rstrip()
    after = stem[cnpj_match.end() :].strip()
    if after.startswith("- "):
        periodo = normalize_periodo(after[2:])
    else:
        periodo = "Incremental"

    if " - " not in before:
        return None

    codigo, razao = before.split(" - ", 1)
    return NfseZipMeta(
        codigo=codigo.strip(),
        razao=razao.strip(),
        cnpj=cnpj,
        periodo=periodo,
    )


def unique_dir(directory: Path, name: str) -> Path:
    candidate = directory / name
    if not candidate.exists():
        return candidate
    index = 1
    while True:
        candidate = directory / f"{name}_{index}"
        if not candidate.exists():
            return candidate
        index += 1


def map_zip_entry_to_dest(entry_name: str, period_dir: Path) -> Path | None:
    parts = entry_name.replace("\\", "/").split("/")
    if not parts or parts[-1] == "":
        return None

    filename = parts[-1]
    if not filename or filename.endswith("/"):
        return None

    # root/YYYY-MM/Tipo/arquivo
    if len(parts) >= 4 and MONTH_FOLDER_PATTERN.match(parts[1]):
        tipo = parts[2]
        return period_dir / tipo / filename

    # root/Tipo/arquivo (sem mês)
    if len(parts) >= 3:
        tipo = parts[1]
        return period_dir / tipo / filename

    if len(parts) == 2:
        return period_dir / filename

    return None


def assert_relative(dest: Path, target: Path) -> None:
    dest = dest.resolve()
    target = target.resolve()
    if not target.is_relative_to(dest):
        raise HTTPException(status_code=400, detail="ZIP com caminho inválido.")


def extract_structured(zf: zipfile.ZipFile, dest_root: Path, meta: NfseZipMeta) -> tuple[Path, int]:
    """Extrai para {codigo} - {razao} - {cnpj}/{periodo}/… como o robô."""
    client_dir = dest_root / meta.client_folder
    period_dir = client_dir / meta.periodo
    period_dir.mkdir(parents=True, exist_ok=True)

    files = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        if not name or name.endswith("/"):
            continue
        if ".." in Path(name).parts:
            raise HTTPException(status_code=400, detail="ZIP com caminho inválido.")

        dest_file = map_zip_entry_to_dest(name, period_dir)
        if dest_file is None:
            # arquivo solto na raiz do ZIP
            if "/" not in name.rstrip("/"):
                dest_file = period_dir / Path(name).name
            else:
                logger.debug("Ignorando entrada: %s", name)
                continue

        assert_relative(dest_root, dest_file)
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, dest_file.open("wb") as out:
            out.write(src.read())
        files += 1

    return period_dir, files


def safe_extract(zf: zipfile.ZipFile, dest: Path) -> int:
    """Fallback: extrai membros do ZIP em dest; rejeita path traversal."""
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    files = 0
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if not name or name.endswith("/"):
            continue
        parts = [p for p in Path(name).parts if p not in ("/", ".", "..")]
        if not parts or ".." in Path(name).parts:
            raise HTTPException(status_code=400, detail="ZIP com caminho inválido.")
        target = (dest / Path(*parts)).resolve()
        if not target.is_relative_to(dest):
            raise HTTPException(status_code=400, detail="ZIP com caminho inválido.")
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, target.open("wb") as out:
            out.write(src.read())
        files += 1
    return files


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

    raw_filename = file.filename or "nfse.zip"
    filename = sanitize_filename(raw_filename)
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
    # Parse no nome cru (pode ter /) e no sanitizado (com _)
    meta = parse_zip_filename(raw_filename) or parse_zip_filename(filename)

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            if zf.testzip() is not None:
                raise HTTPException(status_code=400, detail="ZIP corrompido.")
            if meta is not None:
                extract_dir, file_count = extract_structured(zf, cfg.dest_dir, meta)
            else:
                # Fallback: stem completo já sanitizado (sem truncar no CNPJ)
                extract_dir = unique_dir(cfg.dest_dir, Path(filename).stem)
                file_count = safe_extract(zf, extract_dir)
    except HTTPException:
        raise
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="ZIP inválido.") from None

    logger.info(
        "ZIP extraído: %s (%s bytes, %s arquivos)",
        extract_dir.name,
        len(data),
        file_count,
    )
    return JSONResponse(
        status_code=201,
        content={
            "extractedTo": extract_dir.name if meta is None else meta.client_folder,
            "path": str(extract_dir if meta is None else extract_dir.parent),
            "files": file_count,
        },
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
