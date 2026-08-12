# Receptor ZIP NFS-e (servidor local)

API Python simples no **servidor de arquivos**. O hub (Rails no Hatchbox) envia o ZIP via HTTP; este serviço grava o arquivo na pasta de controle.

Substitui o download no navegador quando o usuário clica **Baixar ZIP** em `/gerar-nfse`.

## Fluxo

1. Usuário clica **Baixar ZIP** no hub → export gerado até `ready`.
2. Frontend chama `POST /api/v1/nfse_zip_exports/:id/deliver_local`.
3. Rails envia o ZIP (multipart) para esta API (`POST /v1/nfse-zips`).
4. Arquivo é gravado em `NFSE_DEST_DIR` e o export fica `delivered`.

O robô pull (`tools/nfse-zip-robot`) **não muda**. Com `delivered`, ele não reprocessa o mesmo export.

## Pré-requisito de rede

O Hatchbox precisa alcançar esta API (VPN, Cloudflare Tunnel, ngrok, etc.).

## Configuração no hub (preferencial)

Em **Configurações → NFS-e → Robô NFS-e** (apenas `heromildes16@gmail.com`):

- URL base do receptor (inclui porta se necessário, ex. `https://tunel.exemplo.com` ou `http://host:8787`)
- API key (mesma de `NFSE_RECEIVER_API_KEY` neste `.env`)
- Origin (header enviado pelo Rails; default `https://hub.silveirasoares.com.br`)

O deliver usa primeiro esses valores do banco; se vazios, cai no ENV do Hatchbox.

## Início rápido (Windows)

1. Copie `.env.example` → `.env` e defina `NFSE_RECEIVER_API_KEY` (mesma chave cadastrada no hub).
2. (Opcional) Ajuste `NFSE_DEST_DIR`.
3. Rode:

```bat
cd tools\nfse-zip-receiver
start.bat
```

Agendador de Tarefas: execute `start.bat` na inicialização do servidor.

## Linux

```bash
cd tools/nfse-zip-receiver
cp .env.example .env
# edite NFSE_RECEIVER_API_KEY
chmod +x start.sh
./start.sh
```

## Endpoints

| Método | Caminho | Descrição |
|--------|---------|-----------|
| GET | `/health` | Healthcheck |
| POST | `/v1/nfse-zips` | Upload multipart campo `file` (`.zip`) |

Headers obrigatórios no upload:

- `Authorization: Bearer <NFSE_RECEIVER_API_KEY>`
- `Origin` (ou `Referer`) de um host permitido

## Variáveis (.env) do receptor Python

| Variável | Padrão | Descrição |
|----------|--------|-----------|
| `NFSE_RECEIVER_API_KEY` | *(obrigatória)* | Segredo compartilhado com o hub |
| `NFSE_DEST_DIR` | `S:\Contabilidade\Privado\Planilhas de Controle\Planilha Controle 2026\Controle NFS-e` | Pasta de destino |
| `NFSE_ALLOWED_ORIGINS` | `https://hub.silveirasoares.com.br,https://63qe0.hatchboxapp.com` | Origins aceitos |
| `NFSE_RECEIVER_HOST` | `0.0.0.0` | Bind |
| `NFSE_RECEIVER_PORT` | `8787` | Porta |
| `NFSE_MAX_UPLOAD_MB` | `50` | Limite de upload |

## Fallback no Hatchbox (ENV)

Só necessário se ainda não salvou a config no hub:

```env
NFSE_LOCAL_RECEIVER_URL=https://seu-tunel.exemplo.com
NFSE_LOCAL_RECEIVER_API_KEY=troque-esta-chave
NFSE_LOCAL_RECEIVER_ORIGIN=https://hub.silveirasoares.com.br
```

## Segurança

- API key Bearer obrigatória (comparação constante).
- Allowlist de Origin/Referer (hub + hatchbox).
- Só `.zip`; nome sanitizado (sem path traversal); não sobrescreve arquivos existentes.
- Limite de tamanho; docs Swagger desabilitados.
- UI de config no hub restrita ao e-mail gerenciador.

## Testes

```bash
cd tools/nfse-zip-receiver
python3 -m venv nfse_zip_receiver_venv
./nfse_zip_receiver_venv/bin/pip install -r requirements.txt
NFSE_RECEIVER_API_KEY=test ./nfse_zip_receiver_venv/bin/pytest -q
```
