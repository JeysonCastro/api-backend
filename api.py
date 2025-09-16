# app.py  — versão Mercado Pago (PIX + Cartão) para sua VM atual
import redis
import logging
import os
import json
import uuid
import base64
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import traceback
import time
import requests
from flask_cors import CORS

from flask import Flask, request, jsonify, redirect
from docusign_esign import (
    ApiClient, EnvelopesApi,
    EnvelopeDefinition, Document, Signer, SignHere, Tabs, Recipients,
    RecipientViewRequest
)
from supabase import create_client, Client
import certifi
import mercadopago
from dotenv import load_dotenv

load_dotenv()

# ---------------------------
# Variáveis de ambiente
# ---------------------------
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
MP_ACCESS_TOKEN = os.environ.get("MP_ACCESS_TOKEN")
MP_PUBLIC_KEY = os.environ.get("MP_PUBLIC_KEY")
DS_BASE_PATH = os.getenv("DS_BASE_PATH", "https://n2.docusign.net/restapi")
DS_AUTH_SERVER = os.getenv("DS_AUTH_SERVER", "account.docusign.com")
DS_INTEGRATION_KEY = os.getenv("DOCUSIGN_INTEGRATION_KEY")
DS_USER_ID = os.getenv("DOCUSIGN_USER_ID")
DS_ACCOUNT_ID = os.getenv("DOCUSIGN_ACCOUNT_ID")
with open(os.getenv("DOCUSIGN_PRIVATE_KEY_PATH"), "r") as key_file:
    DS_PRIVATE_KEY = key_file.read().encode("utf-8")
DOCUSIGN_CLIENT_ID = os.getenv("DOCUSIGN_CLIENT_ID") or DS_INTEGRATION_KEY
DOCUSIGN_CLIENT_SECRET = os.getenv("DOCUSIGN_CLIENT_SECRET")

DOCUSIGN_REDIRECT_URI = os.getenv("DOCUSIGN_REDIRECT_URI", "https://casadoar.ddns.net")
DOCUSIGN_TOKEN_URL = "https://account.docusign.com/oauth/token"

# Redis (ajuste host/porta se necessário)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
mp = mercadopago.SDK(MP_ACCESS_TOKEN)

r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

app = Flask(__name__)
CORS(app)
PDF_PATH = "/home/jeysincastrin/api-backend/contrato_padrao.pdf"
# --------------------------------------------------------
# JWT Flow - geração e cache de token
# --------------------------------------------------------
def get_jwt_access_token() -> str:
    try:
        token = r.get("docusign_access_token")
        expira_em = r.get("docusign_token_expires_at")

        if token and expira_em and float(expira_em) > time.time():
            return token  # já é string no Redis

        api_client = ApiClient()
        api_client.set_base_path(DS_BASE_PATH)
        api_client.set_oauth_host_name(DS_AUTH_SERVER)

        token_response = api_client.request_jwt_user_token(
            client_id=DS_INTEGRATION_KEY,
            user_id=DS_USER_ID,
            oauth_host_name=DS_AUTH_SERVER,
            private_key_bytes=DS_PRIVATE_KEY,
            expires_in=3600,
            scopes=["signature", "impersonation"]
        )

        access_token = token_response.access_token
        if not access_token:
            raise Exception("Falha ao gerar access_token JWT")

        # guarda no Redis
        r.set("docusign_access_token", access_token)
        r.set("docusign_token_expires_at", str(time.time() + 3600 - 60))

        return access_token

    except Exception as e:
        print("[ERRO get_jwt_access_token]", str(e))
        print(traceback.format_exc())
        return None



# ---------------------------
# Helpers
# ---------------------------

def _ensure_data_uri_png(b64_str: str) -> str:
    """Garante prefixo data URI para imagens base64."""
    if not b64_str:
        return ""
    if not b64_str.startswith("data:image"):
        return f"data:image/png;base64,{b64_str}"
    return b64_str

redis_client = redis.Redis(host='localhost', port=6379, db=0)

# Tempo de expiração da sessão (10 minutos)
SESSION_EXPIRATION_SECONDS = 600

def salvar_sessao_redis(session_id, envelope_id, nome, email):
    r.set(f"sessao:{session_id}", json.dumps({
        "envelope_id": envelope_id,
        "nome": nome,
        "email": email
    }), ex=3600)

def buscar_sessao_redis(guid):
    """Busca sessão no Redis"""
    try:
        data = redis_client.get(guid)
        return json.loads(data) if data else None
    except Exception as e:
        logging.error(f"Erro ao buscar sessão no Redis: {e}")
        return None

def remover_sessao_redis(guid):
    """Remove sessão do Redis"""
    try:
        redis_client.delete(guid)
    except Exception as e:
        logging.error(f"Erro ao remover sessão no Redis: {e}")

# ---------------------------
# Mercado Pago: PIX
# ---------------------------

def gerar_pix_mp(valor_centavos: int, email_cliente: str, nome_cliente: Optional[str] = None,
                 cpf_cliente: Optional[str] = None) -> Optional[Dict[str, Any]]:
    try:
        valor = round(valor_centavos / 100.0, 2)

        payer: Dict[str, Any] = {"email": email_cliente}
        if nome_cliente:
            payer["first_name"] = nome_cliente
        if cpf_cliente:
            payer["identification"] = {"type": "CPF", "number": cpf_cliente}

        pagamento = sdk.payment().create({
            "transaction_amount": valor,
            "description": "Pagamento Casa do Ar",
            "payment_method_id": "pix",
            "payer": payer
        })

        resp = pagamento["response"]
        poi = resp.get("point_of_interaction", {}) or {}
        txdata = poi.get("transaction_data", {}) or {}

        return {
            "payment_id": resp.get("id"),
            "status": resp.get("status"),
            "qr_code": txdata.get("qr_code"),
            "qr_code_base64": _ensure_data_uri_png(txdata.get("qr_code_base64", "")),
        }
    except Exception as e:
        print(f"[MP][PIX] Erro: {e}")
        return None

# ---------------------------
# Mercado Pago: Cartão (Checkout Pro via Preference)
# ---------------------------

def gerar_preferencia_cartao_mp(valor_centavos: int, email_cliente: str,
                                titulo_item: str = "Pagamento Casa do Ar") -> Optional[Dict[str, Any]]:
    try:
        valor = round(valor_centavos / 100.0, 2)
        payment_methods = {
            "excluded_payment_methods": [{"id": "pix"}, {"id": "bolbradesco"}],
            "excluded_payment_types": [{"id": "ticket"}],
            "installments": 12
        }

        preferencia = sdk.preference().create({
            "items": [{
                "title": titulo_item,
                "quantity": 1,
                "unit_price": valor
            }],
            "payer": {"email": email_cliente},
            "payment_methods": payment_methods,
            "back_urls": {
                "success": "casadoar://pagamento/sucesso",
                "failure": "casadoar://pagamento/falha",
                "pending": "casadoar://pagamento/pendente"
            },
            "auto_return": "approved"
        })

        return {
            "id": preferencia["response"]["id"],
            "init_point": preferencia["response"]["init_point"]
        }
    except Exception as e:
        print(f"[MP][CARD] Erro: {e}")
        return None

# ---------------------------
# DocuSign: criar envelope + gerar recipient view
# ---------------------------

# 🔹 Função auxiliar para autenticar
def obter_api_client() -> ApiClient:
    access_token = get_jwt_access_token()
    if not access_token:
        raise Exception("Não foi possível gerar access_token via JWT")

    api_client = ApiClient()
    api_client.set_base_path(DS_BASE_PATH)
    api_client.set_default_header("Authorization", f"Bearer {access_token}")
    return api_client



# ---------------------------
# DocuSign: criar envelope + gerar recipient view (versão final)
# ---------------------------
def criar_envelope_e_gerar_view(signer_name, signer_email, document_base64):
    try:
        api_client = obter_api_client()
        envelopes_api = EnvelopesApi(api_client)

        # Documento
        document = Document(
            document_base64=document_base64,
            name="Contrato",
            file_extension="pdf",
            document_id="1"
        )

        # Signatário
        signer = Signer(
            email=signer_email,
            name=signer_name,
            recipient_id="1",
            routing_order="1",
            client_user_id="1234"  # Identificador único no teu app
        )

        # Aba de assinatura
        sign_here = SignHere(
            document_id="1",
            page_number="1",
            recipient_id="1",
            tab_label="Assinatura",
            x_position="100",
            y_position="150"
        )

        signer.tabs = Tabs(sign_here_tabs=[sign_here])

        # Envelope
        envelope_definition = EnvelopeDefinition(
            email_subject="Por favor, assine este documento",
            documents=[document],
            recipients=Recipients(signers=[signer]),
            status="sent"
        )

        # Criar envelope
        results = envelopes_api.create_envelope(DS_ACCOUNT_ID, envelope_definition=envelope_definition)
        envelope_id = results.envelope_id

        # Gerar URL de assinatura embutida
        recipient_view_request = RecipientViewRequest(
            authentication_method="none",
            client_user_id="1234",
            recipient_id="1",
            return_url="https://casadoar.ddns.net/docusign_sign_callback",
            user_name=signer_name,
            email=signer_email
        )

        view = envelopes_api.create_recipient_view(
            DS_ACCOUNT_ID, envelope_id, recipient_view_request=recipient_view_request
        )
        signing_url = view.url

        # Gerar session_id para poder retomar depois
        session_id = str(uuid.uuid4())
        salvar_sessao_redis(session_id, envelope_id, signer_name, signer_email)

        return envelope_id, signing_url, session_id

    except Exception as e:
        print("[ERRO criar_envelope_e_gerar_view]", str(e))
        if hasattr(e, 'body'):
            print("Detalhes do erro DocuSign:", e.body)
        print(traceback.format_exc())
        return None, None, None


def create_recipient_view_for_envelope(envelope_id, nome, email, client_user_id="1234"):
    try:
        api_client = obter_api_client()
        envelopes_api = EnvelopesApi(api_client)

        view_request = RecipientViewRequest(
            authentication_method="none",
            client_user_id=client_user_id,
            recipient_id="1",
            return_url="https://casadoar.ddns.net/docusign_sign_callback",
            user_name=nome,
            email=email
        )

        view = envelopes_api.create_recipient_view(DS_ACCOUNT_ID, envelope_id, recipient_view_request=view_request)
        return view.url

    except Exception as e:
        print("[ERRO DOCUSIGN create_recipient_view_for_envelope]", str(e))
        print(traceback.format_exc())
        
# ---------------------------
# Rotas
# ---------------------------

@app.route("/")
def index():
    return "<h1>API da Casa do Ar (Mercado Pago) ok!</h1>"

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "docusign-jwt"})


@app.route("/envelope", methods=["POST"])
def envelope():
    try:
        data = request.json
        signer_email = data.get("email")
        signer_name = data.get("name")
        document_base64 = data.get("document")

        if not signer_email or not signer_name or not document_base64:
            return jsonify({"error": "Campos obrigatórios ausentes"}), 400

        # precisa do account_id
        api_client = obter_api_client()
        user_info = api_client.get_user_info(get_jwt_access_token())
        account_id = user_info.accounts[0].account_id

        url_assinatura = criar_envelope_e_gerar_view(account_id, signer_email, signer_name, document_base64)

        if not url_assinatura:
            return jsonify({"error": "Falha ao criar envelope"}), 500

        return jsonify({"signing_url": url_assinatura})

    except Exception as e:
        print("[ERRO /envelope]", str(e))
        print(traceback.format_exc())
        return jsonify({"error": str(e)}), 500
        
# PIX
@app.route("/criar_cobranca_pix", methods=["POST"])
def criar_cobranca_pix_endpoint():
    """
    Espera JSON:
    {
        "valor_centavos": 139900,
        "cliente_id": 123,
        "ar_id": 1,
        "data_id": 45,
        "email": "cliente@ex.com",
        "nome_cliente": "Fulano",
        "cpf_cliente": "12345678901"
    }
    """
    dados = request.get_json(force=True)
    try:
        resp_mp = gerar_pix_mp(
            valor_centavos=dados["valor_centavos"],
            email_cliente=dados["email"],
            nome_cliente=dados.get("nome_cliente"),
            cpf_cliente=dados.get("cpf_cliente"),
        )
        if not resp_mp or not resp_mp.get("payment_id"):
            raise Exception(f"Falha ao gerar PIX no MP: {resp_mp}")

        # Mantemos a coluna txid_efi para evitar migração (agora guarda o payment_id do MP).
        nova_inst = {
            "cliente_id": dados["cliente_id"],
            "ar_id": dados["ar_id"],
            "data_instalacao_id": dados["data_id"],
            "status": "AGUARDANDO_PAGAMENTO",
            "txid_efi": str(resp_mp["payment_id"]),
        }
        response_db = supabase.table("instalacoes").insert(nova_inst).execute()
        inst = response_db.data[0]

        return jsonify({
            "instalacao_id": inst["id"],
            "qrcode": resp_mp.get("qr_code"),
            "imagemQrcode": resp_mp.get("qr_code_base64"),
            "payment_id": resp_mp["payment_id"]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# CARTÃO
@app.route("/criar_link_cartao", methods=["POST"])
def criar_link_cartao_endpoint():
    """
    Espera JSON:
    {
        "valor_centavos": 139900,
        "email": "cliente@ex.com",
        "cliente_id": 123,
        "ar_id": 1,
        "data_id": 45
    }
    """
    dados = request.get_json(force=True)
    try:
        pref = gerar_preferencia_cartao_mp(
            valor_centavos=dados["valor_centavos"],
            email_cliente=dados["email"],
        )
        if not pref or not pref.get("init_point"):
            raise Exception(f"Erro ao criar preferência MP: {pref}")

        # Registra instalação pendente também para cartão
        nova_inst = {
            "cliente_id": dados["cliente_id"],
            "ar_id": dados["ar_id"],
            "data_instalacao_id": dados["data_id"],
            "status": "AGUARDANDO_PAGAMENTO",
            "txid_efi": str(pref["id"]),  # guardamos o ID da preferência (ou salve depois o payment_id no webhook)
        }
        response_db = supabase.table("instalacoes").insert(nova_inst).execute()
        inst = response_db.data[0]

        return jsonify({
            "instalacao_id": inst["id"],
            "payment_url": pref["init_point"],
            "preference_id": pref["id"],
            "public_key": MP_PUBLIC_KEY  # útil se um dia usar CardForm no app
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# STATUS POR ID DE INSTALAÇÃO
@app.route("/instalacao/status/<int:instalacao_id>", methods=["GET"])
def get_instalacao_status(instalacao_id: int):
    try:
        response = supabase.table("instalacoes").select("*").eq("id", instalacao_id).single().execute()
        if not response.data:
            return jsonify({"error": "Instalação não encontrada"}), 404
        return jsonify(response.data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/gerar_link_assinatura", methods=["POST"])
def gerar_link_assinatura_endpoint():
    """
    Cria envelope no DocuSign, gera link de assinatura e salva sessão no Redis.
    Retorna JSON com session_id e signing_url.
    """
    try:
        data = request.get_json(force=True)
        nome = data.get("nome")
        email = data.get("email")

        if not nome or not email:
            return jsonify({"error": "Nome e e-mail são obrigatórios"}), 400

        envelope_id, signing_url, session_id = criar_envelope_e_gerar_view(nome, email)

        if not envelope_id or not signing_url or not session_id:
            return jsonify({"error": "Falha ao criar envelope ou gerar link de assinatura"}), 500

        return jsonify({
            "session_id": session_id,
            "envelope_id": envelope_id,
            "signing_url": signing_url
        }), 200

    except Exception as e:
        import traceback
        print("[ERRO /gerar_link_assinatura]", str(e))
        print(traceback.format_exc())
        return jsonify({"error": str(e)}), 500
        
# -------------------------------
# Redireciona para DocuSign
# -------------------------------
@app.route("/sign/<guid>", methods=["GET"])
def redirect_to_docusign(guid):
    """Redireciona o navegador para a URL de assinatura real"""
    sessao = buscar_sessao_redis(guid)
    if not sessao:
        return jsonify({"error": "Sessão não encontrada ou expirada."}), 404

    try:
        envelope_id = sessao.get("envelope_id")
        nome = sessao.get("nome")
        email = sessao.get("email")
        if not envelope_id:
            return jsonify({"error": "Envelope ID não encontrado na sessão."}), 500

        url_assinatura = create_recipient_view_for_envelope(envelope_id, nome, email)
        if not url_assinatura:
            return jsonify({"error": "Falha ao gerar URL de assinatura para o envelope existente."}), 500

        return redirect(url_assinatura, code=302)
    except Exception as e:
        import traceback
        print("[ERRO FLASK redirect_to_docusign]", str(e))
        print(traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route("/docusign/criar", methods=["POST"])
def docusign_criar():
    try:
        data = request.get_json()
        signer_name = data.get("nome")
        signer_email = data.get("email")

        if not signer_name or not signer_email:
            return jsonify({"error": "Campos obrigatórios ausentes: nome, email"}), 400

        # 🔹 Lê o contrato fixo da VM
        if not os.path.exists(PDF_PATH):
            return jsonify({"error": f"Arquivo PDF não encontrado em {PDF_PATH}"}), 500

        with open(PDF_PATH, "rb") as f:
            document_base64 = base64.b64encode(f.read()).decode("utf-8")

        # 🔹 Cria envelope no DocuSign
        envelope_id, signing_url, session_id = criar_envelope_e_gerar_view(
            signer_name, signer_email, document_base64
        )

        if not envelope_id:
            return jsonify({"error": "Falha ao criar envelope"}), 500

        return jsonify({
            "envelope_id": envelope_id,
            "signing_url": signing_url,
            "session_id": session_id
        }), 200

    except Exception as e:
        print("[ERRO /docusign/criar]", str(e))
        print(traceback.format_exc())
        return jsonify({"error": str(e)}), 500

@app.route("/docusign/resume", methods=["POST"])
def docusign_resume():
    """
    Permite recuperar a URL de assinatura caso o usuário tenha fechado o navegador.
    Usa session_id salvo no Redis.
    """
    try:
        data = request.get_json()
        session_id = data.get("session_id")

        if not session_id:
            return jsonify({"erro": "session_id obrigatório"}), 400

        sessao = r.get(f"sessao:{session_id}")
        if not sessao:
            return jsonify({"erro": "Sessão não encontrada ou expirada"}), 404

        sessao_data = json.loads(sessao)
        envelope_id = sessao_data["envelope_id"]
        nome = sessao_data["nome"]
        email = sessao_data["email"]

        signing_url = create_recipient_view_for_envelope(envelope_id, nome, email)

        if not signing_url:
            return jsonify({"erro": "Falha ao gerar nova URL de assinatura"}), 500

        return jsonify({"signing_url": signing_url})

    except Exception as e:
        print("[ERRO /docusign/resume]", str(e))
        print(traceback.format_exc())
        return jsonify({"erro": str(e)}), 500


@app.route("/docusign_sign_callback", methods=["GET"])
def docusign_sign_callback():
    """
    Callback de retorno após assinatura. 
    Aqui você pode atualizar status no Supabase ou redirecionar usuário.
    """
    try:
        event = request.args.get("event")
        envelope_id = request.args.get("envelopeId")

        print(f"[CALLBACK DS] Envelope {envelope_id} evento={event}")

        # TODO: atualizar status no Supabase se necessário
        return redirect("https://casadoar.ddns.net/sucesso")  

    except Exception as e:
        print("[ERRO /docusign_sign_callback]", str(e))
        print(traceback.format_exc())
        return jsonify({"erro": str(e)}), 500

# @app.route("/callback")
# def docusign_callback():
#     """
#     Captura o "code" enviado pelo DocuSign, troca por access_token + refresh_token
#     e salva no Redis.
#     """
#     code = request.args.get("code")
#     if not code:
#         return jsonify({"error": "Authorization code não encontrado"}), 400

#     payload = {
#         "grant_type": "authorization_code",
#         "code": code,
#         "client_id": DOCUSIGN_CLIENT_ID,
#         "client_secret": DOCUSIGN_CLIENT_SECRET,
#         "redirect_uri": DOCUSIGN_REDIRECT_URI,
#     }

#     response = requests.post(DOCUSIGN_TOKEN_URL, data=payload)
#     if response.status_code != 200:
#         return jsonify({"error": "Falha ao trocar código por token", "details": response.text}), 400

#     data = response.json()
#     access_token = data["access_token"]
#     refresh_token = data.get("refresh_token")
#     expires_in = data.get("expires_in", 3600)

#     # Salva no Redis
#     r.set("docusign_access_token", access_token)
#     r.set("docusign_refresh_token", refresh_token)
#     r.set("docusign_token_expires_at", str(time.time() + expires_in - 60))  # margem de 1 min

#     # Redireciona para página de sucesso (pode ser sua UI ou uma página simples)
#     return redirect("/success")  # crie essa rota ou troque pelo seu frontend

# @app.route("/success")
# def success():
#     """
#     Página simples de confirmação de login com DocuSign.
#     """
#     import time

#     access_token = r.get("docusign_access_token")
#     expires_at = r.get("docusign_token_expires_at")

#     if not access_token:
#         return "<h2 style='color:red;'>❌ Nenhum token encontrado. Tente novamente o login.</h2>"

#     expira_em = int(float(expires_at) - time.time()) if expires_at else None

#     return f"""
#     <html>
#       <head><title>DocuSign Autenticado</title></head>
#       <body style="font-family: Arial, sans-serif; text-align: center; margin-top: 50px;">
#         <h2 style="color: green;">✅ Autenticação com DocuSign concluída com sucesso!</h2>
#         <p><b>Access Token:</b> {access_token[:20]}... (oculto)</p>
#         <p><b>Expira em:</b> {expira_em} segundos</p>
#         <p>Agora você já pode usar a API do DocuSign no backend 🚀</p>
#       </body>
#     </html>
#     """
        
# ---------------------------
# Webhook Mercado Pago
# ---------------------------
# Configure no painel do MP:
#   URL: https://SEU_HOST/webhook/mercadopago
#   Eventos: payments, merchant_orders
#
# Observação: o MP NÃO usa assinatura HMAC como a Efí.
# Você deve validar consultando o pagamento pelo ID que chega no webhook.
# ---------------------------

@app.route("/webhook/mercadopago", methods=["POST", "GET"])
def webhook_mercadopago():
    try:
        if request.method == "GET":
            # Para teste de conexão do MP
            return jsonify({"status": "ok"}), 200

        evento = request.get_json(force=True) or {}
        print(f"[WEBHOOK MP RAW] {json.dumps(evento, indent=2)}")

        data_obj = evento.get("data", {}) or {}
        payment_id = data_obj.get("id")

        if not payment_id:
            return jsonify({"status": "sem payment_id"}), 200

        pagamento = sdk.payment().get(payment_id)
        resp = pagamento.get("response", {}) or {}
        status = resp.get("status")
        order = resp.get("order") or {}
        pref_id = order.get("id")

        if status == "approved":
            # Atualiza por payment_id
            supabase.table("instalacoes").update(
                {"status": "PAGO"}
            ).eq("txid_efi", str(payment_id)).execute()

            # Se não achou pelo payment_id, tenta pelo preference_id
            if pref_id:
                supabase.table("instalacoes").update(
                    {"status": "PAGO", "txid_efi": str(payment_id)}
                ).eq("txid_efi", str(pref_id)).execute()

        elif status in ("rejected", "cancelled"):
            supabase.table("instalacoes").update(
                {"status": "FALHA"}
            ).eq("txid_efi", str(payment_id)).execute()

        print(f"[WEBHOOK MP] payment_id={payment_id} status={status}")
        return jsonify({"ok": True}), 200

    except Exception as e:
        print(f"[WEBHOOK MP] ERRO: {e}")
        return jsonify({"error": str(e)}), 500

# ---------------------------
# Execução local
# ---------------------------
if __name__ == "__main__":
    # Em produção na VM do Google, execute com gunicorn/uvicorn e HTTPS atrás de um proxy.
    app.run(host="0.0.0.0", port=5000, debug=False)









