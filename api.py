import os
import json
from datetime import datetime
from flask import Flask, request, jsonify
import mercadopago
from supabase import create_client, Client
from typing import Optional, Dict, Any

# -------------------------------------------------
# Configurações de ambiente
# -------------------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")  # Access Token de PRODUÇÃO do Mercado Pago

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
mp = mercadopago.SDK(MP_ACCESS_TOKEN)

app = Flask(__name__)

# -------------------------------------------------
# Funções auxiliares
# -------------------------------------------------
def salvar_pagamento_supabase(dados_pagamento: dict):
    try:
        resp = supabase.table("pagamentos").insert(dados_pagamento).execute()
        print("Pagamento salvo no Supabase:", resp.data)
    except Exception as e:
        print("Erro ao salvar pagamento no Supabase:", e)


def _ensure_data_uri_png(b64: str) -> str:
    """Adiciona prefixo data:image/png;base64, se não tiver"""
    if not b64:
        return ""
    if not b64.startswith("data:image/png;base64,"):
        return "data:image/png;base64," + b64
    return b64


@app.route("/")
def home():
    return """
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <title>Casa do Ar API</title>
        <style>
            body {
                font-family: Arial, sans-serif;
                background: #f9f9f9;
                color: #333;
                text-align: center;
                padding-top: 100px;
            }
            h1 {
                color: #ff6600;
            }
        </style>
    </head>
    <body>
        <h1>🚀 Casa do Ar API</h1>
        <p>O servidor Flask está funcionando corretamente.</p>
    </body>
    </html>
    """

# -------------------------------------------------
# Mercado Pago: PIX
# -------------------------------------------------
def gerar_pix_mp(valor_centavos: int, email_cliente: str, nome_cliente: Optional[str] = None, cpf_cliente: Optional[str] = None) -> Optional[Dict[str, Any]]:
    try:
        valor = round(valor_centavos / 100.0, 2)
        payer: Dict[str, Any] = {"email": email_cliente}
        if nome_cliente:
            payer["first_name"] = nome_cliente
        if cpf_cliente:
            payer["identification"] = {"type": "CPF", "number": cpf_cliente}

        pagamento = mp.payment().create({
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

# -------------------------------------------------
# Mercado Pago: Cartão (Checkout Pro via Preference)
# -------------------------------------------------
def gerar_preferencia_cartao_mp(valor_centavos: int, email_cliente: str, titulo_item: str = "Pagamento Casa do Ar") -> Optional[Dict[str, Any]]:
    try:
        valor = round(valor_centavos / 100.0, 2)

        payment_methods = {
            "excluded_payment_methods": [{"id": "pix"}, {"id": "bolbradesco"}],
            "excluded_payment_types": [{"id": "ticket"}],
            "installments": 12
        }

        preferencia = mp.preference().create({
            "items": [{
                "title": titulo_item,
                "quantity": 1,
                "unit_price": valor
            }],
            "payer": {"email": email_cliente},
            "payment_methods": payment_methods,
            "back_urls": {
                "success": "https://casadoar.ddns.net/pagamento_sucesso",
                "failure": "https://casadoar.ddns.net/pagamento_falhou",
                "pending": "https://casadoar.ddns.net/pagamento_pendente"
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


# -------------------------------------------------
# Rotas Flask
# -------------------------------------------------
@app.route("/criar_cobranca_pix", methods=["POST"])
def criar_cobranca_pix():
    try:
        dados = request.json
        descricao = dados.get("descricao", "Pagamento Casa do Ar")
        valor_centavos = int(dados["valor_centavos"])
        email = dados["email"]
        nome = dados.get("nome", "")
        cpf = dados.get("cpf")

        # 👉 Para testes: força valor pequeno
        if valor_centavos > 100:
            valor_centavos = 100

        resultado = gerar_pix_mp(valor_centavos, email, nome, cpf)
        if not resultado:
            return jsonify({"erro": "Falha ao criar cobrança PIX"}), 500

        # Salvar no Supabase
        dados_pagamento = {
            "descricao": descricao,
            "valor_centavos": valor_centavos,
            "status": resultado["status"],
            "mp_id": resultado["payment_id"],
            "email": email,
            "created_at": datetime.utcnow().isoformat(),
        }
        salvar_pagamento_supabase(dados_pagamento)

        return jsonify(resultado)
    except Exception as e:
        print("[ERRO PIX]", {"erro": str(e)})
        return jsonify({"erro": str(e)}), 500


@app.route("/criar_preferencia_cartao", methods=["POST"])
def criar_preferencia_cartao():
    try:
        dados = request.json
        descricao = dados["descricao"]
        valor_centavos = int(dados["valor_centavos"])
        email = dados["email"]

        # 👉 Para testes: força valor pequeno
        if valor_centavos > 100:
            valor_centavos = 100

        resultado = gerar_preferencia_cartao_mp(valor_centavos, email, descricao)
        if not resultado:
            return jsonify({"erro": "Falha ao criar preferência"}), 500

        # Salvar no Supabase
        dados_pagamento = {
            "descricao": descricao,
            "valor_centavos": valor_centavos,
            "status": "init",
            "mp_preference_id": resultado["id"],
            "email": email,
            "created_at": datetime.utcnow().isoformat(),
        }
        salvar_pagamento_supabase(dados_pagamento)

        return jsonify(resultado)
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


# -------------------------------------------------
# Webhook Mercado Pago
# -------------------------------------------------
@app.route("/webhook/mp", methods=["POST"])
def webhook_mp():
    try:
        evento = request.json
        print("🔔 Webhook MP recebido:", evento)

        if "data" in evento and "id" in evento["data"]:
            pagamento_id = evento["data"]["id"]
            status = evento.get("type")

            supabase.table("pagamentos").update({"status": status}).eq("mp_id", pagamento_id).execute()

        return jsonify({"status": "ok"}), 200
    except Exception as e:
        print("Erro no webhook:", e)
        return jsonify({"erro": str(e)}), 500


# -------------------------------------------------
# Inicialização
# -------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
