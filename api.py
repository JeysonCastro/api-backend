# app.py  — versão Mercado Pago (PIX + Cartão) para sua VM atual

import os
import json
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from flask import Flask, request, jsonify
from supabase import create_client, Client
import certifi
import mercadopago

# ---------------------------
# Variáveis de ambiente
# ---------------------------
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
MP_ACCESS_TOKEN = os.environ.get("MP_ACCESS_TOKEN")
MP_PUBLIC_KEY = os.environ.get("MP_PUBLIC_KEY")

# ---------------------------
# Inicializações
# ---------------------------
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

@app.route("/webhook/mercadopago", methods=["POST"])
def webhook_mercadopago():
    try:
        evento = request.get_json(force=True) or {}
        # Estrutura típica:
        # {
        #   "action": "payment.updated",
        #   "data": {"id": "1234567890"},
        #   "type": "payment"
        # }
        data_obj = evento.get("data", {}) or {}
        payment_id = data_obj.get("id")

        if not payment_id:
            # Alguns webhooks podem vir com topic/querystring. Opcionalmente trate aqui.
            return jsonify({"status": "sem payment_id"}), 200

        pagamento = sdk.payment().get(payment_id)
        resp = pagamento.get("response", {}) or {}
        status = resp.get("status")  # 'approved', 'pending', 'rejected', etc.
        # Você pode obter o preference_id em resp["order"]["id"], se precisar relacionar

        # Atualiza a instalação correspondente
        # 1) Tente achar por txid_efi = payment_id (se você salvou payment_id antes)
        #    Para PIX salvamos o payment_id; para cartão salvamos preference_id.
        #    Se quiser tratar cartão por payment_id, você pode, após o primeiro webhook,
        #    atualizar a instalação (buscar por preference_id) e gravar txid_efi = payment_id.
        if status == "approved":
            supabase.table("instalacoes").update({"status": "PAGO"}).eq("txid_efi", str(payment_id)).execute()
            # Caso não encontre (cartão), tente localizar por preference_id:
            order = resp.get("order") or {}
            pref_id = order.get("id")
            if pref_id:
                supabase.table("instalacoes").update({"status": "PAGO", "txid_efi": str(payment_id)}).eq("txid_efi", str(pref_id)).execute()

        elif status in ("rejected", "cancelled"):
            supabase.table("instalacoes").update({"status": "FALHA"}).eq("txid_efi", str(payment_id)).execute()

        # Você pode logar o evento completo para auditoria:
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
