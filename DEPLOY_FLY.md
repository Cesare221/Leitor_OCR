# Deploy no Fly.io

## Pré-requisitos

1. Conta no [Fly.io](https://fly.io)
2. `flyctl` instalado:
   ```powershell
   # Windows (PowerShell)
   iwr https://fly.io/install.ps1 -useb | iex
   ```
3. Login no flyctl:
   ```powershell
   flyctl auth login
   ```

---

## Primeiro deploy

```powershell
.\deploy_fly.ps1 -Init
```

O script vai:
1. Criar o app `leitor-ocr` no Fly.io
2. Criar um volume persistente de 3 GB em São Paulo (`gru`) para o SQLite e arquivos
3. Pedir e configurar os secrets necessários
4. Fazer o deploy da imagem Docker

Após o deploy, acesse `https://leitor-ocr.fly.dev/setup` para criar o primeiro usuário.

---

## Deploys subsequentes

```powershell
.\deploy_fly.ps1
```

---

## Secrets necessários

Configure via `flyctl secrets set NOME=valor` ou durante o `-Init`:

| Secret | Descrição |
|--------|-----------|
| `OCR_GEMINI_API_KEY` | API Key do [Google AI Studio](https://aistudio.google.com/apikey) |
| `GOOGLE_APPLICATION_CREDENTIALS_JSON` | Conteúdo JSON da service account GCP (para Document AI) |
| `OCR_SETUP_TOKEN` | Token para criar o primeiro usuário (qualquer string segura) |

### Como gerar a service account para Document AI

1. Acesse [IAM & Admin > Service Accounts](https://console.cloud.google.com/iam-admin/serviceaccounts) no projeto `listreader`
2. Crie uma service account com o papel **Document AI API User**
3. Gere uma chave JSON e salve o arquivo
4. Configure o secret:
   ```powershell
   $json = Get-Content ".\service-account.json" -Raw
   flyctl secrets set GOOGLE_APPLICATION_CREDENTIALS_JSON="$json" --app leitor-ocr
   ```

> **Importante:** o arquivo JSON da service account **não deve ser commitado no Git**.

---

## Configurar credenciais GCP no container

O `web_app.py` usa Application Default Credentials (ADC). Para funcionar no Fly.io,
adicione este trecho ao `Dockerfile` ou use um entrypoint que escreva o JSON em disco:

O `fly.toml` já passa `GOOGLE_APPLICATION_CREDENTIALS_JSON` como secret.
Para que o Document AI funcione, é necessário um entrypoint que grave o arquivo antes
de iniciar o app. Veja a seção abaixo.

### Entrypoint para credenciais GCP

Crie o arquivo `entrypoint.sh` na raiz do projeto (já incluído):

```bash
# Grava o JSON da service account em disco se o secret estiver definido
if [ -n "$GOOGLE_APPLICATION_CREDENTIALS_JSON" ]; then
    echo "$GOOGLE_APPLICATION_CREDENTIALS_JSON" > /tmp/gcp-sa.json
    export GOOGLE_APPLICATION_CREDENTIALS=/tmp/gcp-sa.json
fi
exec python web_app.py
```

O `Dockerfile` deve usar este entrypoint (já configurado na versão atualizada).

---

## Estrutura de armazenamento

| Modo | Storage | Quando usar |
|------|---------|-------------|
| `local` (padrão) | SQLite + volume Fly.io | Recomendado — sem dependência de GCP |
| `cloud` | Firestore + GCS | Se quiser manter dados no Google |

Para alternar, mude `OCR_STORAGE_MODE` no `fly.toml` ou via:
```powershell
flyctl secrets set OCR_STORAGE_MODE=cloud --app leitor-ocr
```

---

## Comandos úteis

```powershell
# Ver logs em tempo real
flyctl logs --app leitor-ocr

# Abrir shell no container
flyctl ssh console --app leitor-ocr

# Ver status das máquinas
flyctl status --app leitor-ocr

# Listar volumes
flyctl volumes list --app leitor-ocr

# Ver secrets configurados (apenas os nomes)
flyctl secrets list --app leitor-ocr

# Escalar memória se necessário
flyctl scale memory 2048 --app leitor-ocr
```

---

## Observações

- **Região `gru`** = São Paulo. Mais próxima do Brasil, menor latência.
- **Volume persistente** garante que o SQLite e os arquivos sobrevivam a redeploys.
- **`auto_stop_machines = false`** mantém a máquina sempre ligada, evitando cold start.
  Se quiser economizar mais, mude para `true` (a máquina dorme quando sem tráfego).
- O plano gratuito do Fly.io inclui 3 VMs compartilhadas e 3 GB de volume — suficiente para uso moderado.
