import os
import json
import threading
from datetime import datetime
from flask import Flask, request, jsonify
import mercadopago
from supabase import create_client, Client

# -------------------------------------------------
# Configurações de ambiente
# -------------------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")  # Access Token de PRODUÇÃO do Mercado Pago

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
sdk = mercadopago.SDK(MP_ACCESS_TOKEN)

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
# Rotas de Pagamento
# -------------------------------------------------

@app.route("/criar_cobranca_pix", methods=["POST"])
def criar_cobranca_pix():
    try:
        dados = request.json
        descricao = dados["descricao"]
        valor_centavos = int(dados["valor_centavos"])

        # 👉 Para testes, força valor pequeno (R$ 1,00)
        if valor_centavos > 100:
            valor_centavos = 100

        pagamento_data = {
            "transaction_amount": valor_centavos / 100,
            "description": descricao,
            "payment_method_id": "pix",
            "payer": {
                "email": dados["email"],
                "first_name": dados.get("nome", ""),
            },
        }

        pagamento = sdk.payment().create(pagamento_data)
        resposta = pagamento["response"]

        # Salvar no Supabase
        dados_pagamento = {
            "descricao": descricao,
            "valor_centavos": valor_centavos,
            "status": resposta.get("status"),
            "mp_id": resposta.get("id"),
            "email": dados["email"],
            "created_at": datetime.utcnow().isoformat(),
        }
        salvar_pagamento_supabase(dados_pagamento)

        return jsonify(resposta)
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


@app.route("/criar_preferencia_cartao", methods=["POST"])
def criar_preferencia_cartao():
    try:
        dados = request.json
        descricao = dados["descricao"]
        valor_centavos = int(dados["valor_centavos"])

        # 👉 Para testes, força valor pequeno (R$ 1,00)
        if valor_centavos > 100:
            valor_centavos = 100

        preferencia_data = {
            "items": [
                {
                    "title": descricao,
                    "quantity": 1,
                    "unit_price": valor_centavos / 100,
                    "currency_id": "BRL",
                }
            ],
            "payer": {
                "email": dados["email"],
                "name": dados.get("nome", ""),
            },
            "back_urls": {
                "success": "https://casadoar.ddns.net/pagamento_sucesso",
                "failure": "https://casadoar.ddns.net/pagamento_falhou",
                "pending": "https://casadoar.ddns.net/pagamento_pendente",
            },
            "auto_return": "approved",
        }

        preferencia = sdk.preference().create(preferencia_data)
        resposta = preferencia["response"]

        # Salvar no Supabase
        dados_pagamento = {
            "descricao": descricao,
            "valor_centavos": valor_centavos,
            "status": "init",
            "mp_preference_id": resposta.get("id"),
            "email": dados["email"],
            "created_at": datetime.utcnow().isoformat(),
        }
        salvar_pagamento_supabase(dados_pagamento)

        return jsonify(resposta)
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

        # Atualizar status no Supabase
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

