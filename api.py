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
DS_BASE_PATH = os.getenv("DS_BASE_PATH", "https://demo.docusign.net")
DS_AUTH_SERVER = os.getenv("DS_AUTH_SERVER", "account-d.docusign.com")
DS_INTEGRATION_KEY = os.getenv("DOCUSIGN_INTEGRATION_KEY")
DS_USER_ID = os.getenv("DOCUSIGN_USER_ID")
DS_ACCOUNT_ID = os.getenv("DOCUSIGN_ACCOUNT_ID")
with open(os.getenv("DOCUSIGN_PRIVATE_KEY_PATH"), "r") as key_file:
    DS_PRIVATE_KEY = key_file.read().encode("utf-8")

# ---------------------------
# Inicializações
# ---------------------------
if not all([DS_INTEGRATION_KEY, DS_USER_ID, DS_ACCOUNT_ID, DS_PRIVATE_KEY]):
    raise ValueError("⚠️ Variáveis de ambiente DocuSign não configuradas corretamente.")

try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    app = Flask(__name__)
    sdk = mercadopago.SDK(MP_ACCESS_TOKEN)
except Exception as e:
    print(f"ERRO CRÍTICO ao inicializar serviços: {e}")
    raise

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

def salvar_sessao_redis(guid, envelope_id, nome, email):
    """Salva a sessão no Redis com expiração"""
    try:
        dados = {
            "envelope_id": envelope_id,
            "nome": nome,
            "email": email
        }
        redis_client.setex(guid, timedelta(seconds=SESSION_EXPIRATION_SECONDS), json.dumps(dados))
    except Exception as e:
        logging.error(f"Erro ao salvar sessão no Redis: {e}")

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
def obter_api_client():
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

    access_token = getattr(token_response, "access_token", None)
    if not access_token:
        raise Exception(f"Falha ao obter access_token: {token_response}")

    api_client.set_default_header("Authorization", f"Bearer {access_token}")
    return api_client


# 🔹 Cria envelope e já gera view
def criar_envelope_e_gerar_view(nome, email):
    try:
        # 1. Lê PDF fixo
        pdf_path = os.path.join(os.path.dirname(__file__), "contrato_padrao.pdf")
        if not os.path.exists(pdf_path):
            raise FileNotFoundError(f"PDF não encontrado em {pdf_path}")
        with open(pdf_path, "rb") as f:
            pdf_b64 = base64.b64encode(f.read()).decode("utf-8")

        # 2. API client autenticado
        api_client = obter_api_client()

        # 3. Documento
        document = Document(
            document_base64=pdf_b64,
            name="Contrato.pdf",
            file_extension="pdf",
            document_id="1"
        )

        # 4. Session ID único (e será usado como client_user_id)
        session_id = str(uuid.uuid4())

        signer = Signer(
            email=email,
            name=nome,
            recipient_id="1",
            client_user_id=session_id,
            tabs=Tabs(sign_here_tabs=[
                SignHere(document_id="1", page_number="1", x_position="100", y_position="150")
            ])
        )

        recipients = Recipients(signers=[signer])

        envelope_definition = EnvelopeDefinition(
            email_subject="Assine seu contrato",
            documents=[document],
            recipients=recipients,
            status="sent"
        )

        envelopes_api = EnvelopesApi(api_client)
        envelope_summary = envelopes_api.create_envelope(
            DS_ACCOUNT_ID,
            envelope_definition=envelope_definition
        )

        envelope_id = getattr(envelope_summary, "envelope_id", None)
        if not envelope_id:
            raise Exception(f"Não foi possível obter envelope_id: {envelope_summary}")

        # 5. Salvar sessão (Redis)
        salvar_sessao_redis(session_id, envelope_id, nome, email)

        # 6. Recipient view request (usando o mesmo session_id!)
        view_request = RecipientViewRequest(
            authentication_method="none",
            client_user_id=session_id,
            recipient_id="1",
            return_url=f"https://casadoar.ddns.net/docusign_callback?session_id={session_id}",
            user_name=nome,
            email=email
        )

        view = envelopes_api.create_recipient_view(
            DS_ACCOUNT_ID,
            envelope_id,
            recipient_view_request=view_request
        )

        signing_url = getattr(view, "url", None)
        if not signing_url:
            raise Exception(f"Não foi possível obter URL de assinatura: {view}")

        return envelope_id, signing_url, session_id

    except Exception as e:
        print("[ERRO DOCUSIGN criar_envelope_e_gerar_view]", str(e))
        print(traceback.format_exc())
        return None, None, None


# 🔹 Apenas gerar uma recipient view de um envelope já existente
def create_recipient_view_for_envelope(envelope_id, nome, email, session_id):
    try:
        api_client = obter_api_client()
        envelopes_api = EnvelopesApi(api_client)

        view_request = RecipientViewRequest(
            authentication_method="none",
            client_user_id=session_id,  # mesmo que foi usado ao criar o envelope
            recipient_id="1",
            return_url=f"https://casadoar.ddns.net/docusign_callback?session_id={session_id}",
            user_name=nome,
            email=email
        )

        view = envelopes_api.create_recipient_view(
            DS_ACCOUNT_ID,
            envelope_id,
            recipient_view_request=view_request
        )

        signing_url = getattr(view, "url", None)
        if not signing_url:
            raise Exception(f"[ERRO] Não foi possível obter signing_url. Resposta: {view}")

        return signing_url

    except Exception as e:
        print("[ERRO DOCUSIGN create_recipient_view_for_envelope]", str(e))
        print(traceback.format_exc())
        return None
# ---------------------------
# Rotas
# ---------------------------

@app.route("/")
def index():
    return "<h1>API da Casa do Ar (Mercado Pago) ok!</h1>"

@app.route("/health")
def health():
    return jsonify(ok=True, time=datetime.utcnow().isoformat())

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


@app.route("/docusign_callback", methods=["GET"])
def docusign_callback():
    """
    Callback do DocuSign após o usuário assinar ou sair.
    O DocuSign retorna query params como: event, state, etc.
    Vamos usar o session_id para buscar a sessão no Redis.
    """
    try:
        event = request.args.get("event") or request.args.get("eventStatus")
        session_id = request.args.get("state")  # usamos o state como nosso guid

        print(f"[CALLBACK DOCUSIGN] Event={event}, session_id={session_id}")

        if not session_id:
            return "Session ID ausente no callback.", 400

        sessao = buscar_sessao_redis(session_id)
        if not sessao:
            return "Sessão não encontrada ou expirada.", 404

        envelope_id = sessao.get("envelope_id")
        nome = sessao.get("nome")
        email = sessao.get("email")

        # Opcional: remover a sessão após callback
        remover_sessao_redis(session_id)

        # Aqui você pode decidir o que fazer:
        # - Atualizar status do usuário no banco
        # - Redirecionar para página de sucesso/erro
        if event and event.lower() == "signing_complete":
            return f"Assinatura concluída com sucesso! Envelope {envelope_id} para {nome} ({email})", 200
        else:
            return f"A assinatura foi encerrada com status: {event}", 200

    except Exception as e:
        import traceback
        print("[ERRO /docusign_callback]", str(e))
        print(traceback.format_exc())
        return f"Erro interno: {str(e)}", 500
        
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































