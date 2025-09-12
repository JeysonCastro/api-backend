# app.py  — versão Mercado Pago (PIX + Cartão) para sua VM atual
import redis
import logging
import os
import json
import uuid
import base64
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from flask import Flask, request, jsonify,redirect
from docusign_esign import ApiClient, EnvelopesApi, RecipientViewRequest
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
    """
    Cria um pagamento PIX (Payment API) e retorna os dados do QR Code.
    """
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
        # Campos importantes:
        # - resp["id"]: id do pagamento (usar como "txid" interno)
        # - resp["status"]: 'pending' até ser pago
        # - resp["point_of_interaction"]["transaction_data"]["qr_code"]
        # - resp["point_of_interaction"]["transaction_data"]["qr_code_base64"]
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
    """
    Gera uma preferência do Checkout Pro com método de pagamento limitado a CARTÃO.
    Retorna a URL (init_point) para redirecionamento em WebView/navegador.
    """
    try:
        valor = round(valor_centavos / 100.0, 2)
        # Limitando a cartão de crédito (exclui pix e boleto)
        payment_methods = {
            "excluded_payment_methods": [{"id": "pix"}, {"id": "bolbradesco"}],
            "excluded_payment_types": [{"id": "ticket"}],  # exclui boleto
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
                # Use seu deep link registrado no AndroidManifest e no buildozer.spec
                "success": "casadoar://pagamento/sucesso",
                "failure": "casadoar://pagamento/falha",
                "pending": "casadoar://pagamento/pendente"
            },
            "auto_return": "approved"  # retorna automaticamente no sucesso
        })

        return {
            "id": preferencia["response"]["id"],
            "init_point": preferencia["response"]["init_point"]
        }
    except Exception as e:
        print(f"[MP][CARD] Erro: {e}")
        return None

def gerar_link_embedded_signing(nome, email, envelope_id, client_user_id="1"):
    try:
        # 1. Configurar cliente DocuSign
        api_client = ApiClient()
        api_client.set_base_path("https://demo.docusign.net/restapi")
        api_client.set_oauth_host_name("account-d.docusign.com")

        # 2. Autenticar via JWT
        token_response = api_client.request_jwt_user_token(
            client_id=DS_INTEGRATION_KEY,
            user_id=DS_USER_ID,
            oauth_host_name="account-d.docusign.com",
            private_key_bytes=DS_PRIVATE_KEY,
            expires_in=3600,
            scopes=["signature", "impersonation"]
        )
        access_token = token_response.access_token
        api_client.set_access_token(access_token, 3600)

        # 3. Criar requisição de URL de assinatura
        view_request = RecipientViewRequest(
            authentication_method="none",
            client_user_id=client_user_id,  # tem que bater com o envelope criado
            recipient_id="1",               # igual ao definido no envelope
            return_url="casadoar://assinatura_concluida",
            user_name=nome,
            email=email
        )

        # 4. Gerar URL
        envelopes_api = EnvelopesApi(api_client)
        view = envelopes_api.create_recipient_view(
            DS_ACCOUNT_ID,
            envelope_id,
            recipient_view_request=view_request
        )

        return view.url

    except Exception as e:
        logging.error(f"[ERRO DOCUSIGN] {e}")
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

# Endpoint para gerar link de assinatura
# -------------------------------
@app.route("/gerar_link_assinatura", methods=["POST"])
def gerar_link_assinatura():
    """
    Espera JSON:
    {
      "envelope_id": "xxxx",
      "nome": "Fulano",
      "email": "fulano@email.com"
    }
    """
    dados = request.get_json(force=True)
    try:
        if not all(k in dados for k in ["envelope_id", "nome", "email"]):
            return jsonify({"error": "Campos obrigatórios ausentes"}), 400

        guid = str(uuid.uuid4())
        salvar_sessao_redis(guid, dados["envelope_id"], dados["nome"], dados["email"])

        link_publico = f"{request.url_root}sign/{guid}"
        return jsonify({"link_assinatura": link_publico})

    except Exception as e:
        logging.error(f"Erro ao gerar link: {str(e)}")
        return jsonify({"error": "Erro interno ao gerar link de assinatura."}), 500


# -------------------------------
# Redireciona para DocuSign
# -------------------------------
@app.route("/sign/<guid>", methods=["GET"])
def redirect_to_docusign(guid):
    """Redireciona o navegador para a URL de assinatura real"""
    sessao = buscar_sessao_redis(guid)  # ✅ corrigido
    if not sessao:
        return jsonify({"error": "Sessão não encontrada ou expirada."}), 404

    try:
        url_assinatura = gerar_link_embedded_signing(
            nome=sessao["nome"],
            email=sessao["email"],
            envelope_id=sessao["envelope_id"]
        )
        return redirect(url_assinatura, code=302)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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











