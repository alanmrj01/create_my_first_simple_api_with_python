# ERP Manutenção — API Central 2.0

API FastAPI usada pelo ERP para acessar o Fracttal e o Gemini sem executar essas
requisições diretamente no aplicativo desktop.

## Compatibilidade preservada

Os endpoints já usados pelo ERP continuam disponíveis com o mesmo contrato:

- `GET /api/bridge`
- `GET /api/executar`
- `GET /check_health`

O bridge legado agora aceita somente URLs HTTPS da API oficial do Fracttal. Isso
reduz o risco de SSRF sem alterar as consultas atuais do ERP.

## Nova integração de anexos

### Consultar metadados

```http
GET /api/fracttal/solicitacoes/{code}/anexos
Authorization: Bearer <API_SECRET_TOKEN>
X-Fracttal-Authorization: Basic <TOKEN_BASIC_JA_USADO_PELO_ERP>
```

Parâmetros opcionais:

- `start` (padrão `0`)
- `limit` (máximo `100`)
- `paginate_all` (padrão `true`)
- `include_signed_url` (padrão `false`)

A paginação é executada automaticamente e limitada pelas configurações de
segurança da API.

### Processar documentos para o ERP_22

```http
GET /api/fracttal/solicitacoes/{code}/anexos/processados
Authorization: Bearer <API_SECRET_TOKEN>
X-Fracttal-Authorization: Basic <TOKEN_BASIC_JA_USADO_PELO_ERP>
```

Esse endpoint:

1. consulta todos os anexos da solicitação;
2. baixa cada URL assinada imediatamente;
3. valida HTTPS, host, redirecionamentos, tamanho e tipo real do arquivo;
4. extrai texto de PDF textual, DOCX e TXT;
5. usa Gemini somente como OCR/transcrição de imagens ou PDF escaneado, quando
   essa opção estiver configurada;
6. devolve a chave `anexos_autorizacao`, já compatível com a estrutura preparada
   no ERP_22;
7. inclui uma URL temporária assinada em `imagem_analisada` para fotos e para a
   primeira página de PDFs, permitindo a exibição pelo botão de chave do ERP.

A API **não decide** se a autorização é válida. A decisão permanece no módulo
determinístico `document_authorization.py` do ERP.

Exemplo resumido:

```json
{
  "success": true,
  "code": "55365",
  "processing_status": "CONCLUIDO",
  "anexos_autorizacao": [
    {
      "id": 3,
      "id_request": 55365,
      "description": "de acordo.pdf",
      "mime_type": "application/pdf",
      "texto_extraido": "Estou ciente e de acordo...",
      "metodo_extracao": "pdf_text",
      "status_extracao": "EXTRAIDO"
    }
  ]
}
```

### Sem anexo versus erro de integração

- Consulta bem-sucedida sem arquivos: HTTP `200`, `processing_status=SEM_ANEXOS`
  e `anexos_autorizacao=[]`.
- Erro de autenticação, rede ou payload: resposta HTTP de erro com
  `success=false` e `error_type` específico.

Isso impede que uma indisponibilidade do Fracttal seja interpretada como ausência
de autorização.

## Autenticação do Fracttal nos endpoints de anexos

O ERP pode enviar no header `X-Fracttal-Authorization` o mesmo token Basic que
já utiliza nas consultas normais ao Fracttal. Esse valor prevalece somente na
requisição atual e fica temporariamente apenas em memória para permitir a
visualização e o download pelas URLs assinadas. Assim, não é necessário alterar
as variáveis do Render compartilhado.

As configurações já existentes no Render continuam sendo usadas como fallback.
O `API_SECRET_TOKEN` permanece obrigatório para proteger os endpoints privados.

Para OCR de prints e PDFs escaneados:

```text
DOCUMENT_GEMINI_API_KEY
DOCUMENT_OCR_WITH_GEMINI=true
```

A IA é usada apenas para transcrever o conteúdo visível. A validação da
aprovação continua determinística no ERP.

Use `.env.example` como referência completa.

## Implantação no Render

O `render.yaml` já contém:

```text
Build: pip install -r requirements.txt
Start: uvicorn main:app --host 0.0.0.0 --port $PORT --no-access-log
Health: /health
```

O acesso log foi desativado porque os endpoints legados ainda recebem
credenciais como parâmetros de URL. Nos endpoints especializados, a credencial
do Fracttal é recebida por header e nunca é incluída nas URLs de prévia ou de
download.

## Desenvolvimento e testes

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
uvicorn main:app --reload
```

## Próximo encaixe no ERP

Ao consumir o endpoint processado, o ERP deve fazer:

```python
chamado["anexos_autorizacao"] = resposta["anexos_autorizacao"]
```

Se a API central retornar erro HTTP, deve preencher:

```python
chamado["validacao_documental"] = {
    "aplicavel": True,
    "status": "ERRO_INTEGRACAO",
    "resumo": "Não foi possível consultar ou analisar os anexos.",
    "bloqueia_conversao": True,
}
```

Essa ligação é a única etapa restante para ativar a consulta real no ERP.


## Visualização e download do anexo

Para imagens e PDFs processados, cada item retorna:

- `imagem_analisada`: URL temporária para visualização rápida no ERP;
- `arquivo_url`: alias legado da mesma prévia, preservado por compatibilidade;
- `arquivo_original_url`: URL temporária exclusiva para download do arquivo original.

A rota de visualização responde com `Content-Disposition: inline`. A rota do
arquivo original responde com `Content-Disposition: attachment`, portanto não
há download automático ao abrir a prévia.

## Compartilhamento temporário do CENTRAL ANAYTICS (48 horas)

A API central publica uma cópia estática do relatório em um bucket privado e
entrega um link público assinado. O destinatário não informa login, senha ou
token: possuir o link é suficiente durante as 48 horas de validade.

Fluxo:

- `POST /api/reports/share`: protegido por Bearer `REPORT_UPLOAD_TOKEN` (ou
  `API_SECRET_TOKEN` como compatibilidade). Recebe o ZIP estático do relatório.
- `GET /relatorios/{token}`: público, sem credenciais, válido exatamente por 48h.
- `DELETE /api/reports/share/{token}`: protegido e usado pelo ERP para revogar.

As credenciais do bucket, o segredo de assinatura e os tokens principais nunca
são incorporados ao relatório nem devolvidos nas respostas públicas. O token do
link é aleatório, assinado e contém apenas identificador e validade.

### Configuração de produção

Crie um bucket privado no Cloudflare R2 (ou S3 compatível) e configure no Render
as variáveis `REPORT_SHARE_*` descritas em `.env.example`. Recomenda-se também
uma regra de ciclo de vida no bucket para apagar objetos após 2 dias. Mesmo sem
a regra, a API rejeita o acesso assim que as 48 horas terminam.

`REPORT_UPLOAD_TOKEN` é opcional e deve ser diferente do segredo de assinatura.
Quando ausente, publicação e revogação aceitam a credencial principal
`API_SECRET_TOKEN`, que o ERP já utiliza. Quando um token dedicado for adotado,
o mesmo valor pode ser configurado no ERP como `REPORT_SHARE_UPLOAD_TOKEN`.
