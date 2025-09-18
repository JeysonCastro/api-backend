import os
from datetime import datetime
from flask import Flask, request, jsonify
import mercadopago
from supabase import create_client, Client

# -------------------------------------------------
# Configurações de ambiente
# -------------------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")  # PRODUÇÃO

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
    <html><body>
    <h1>🚀 Casa do Ar API</h1>
    <p>Servidor Flask rodando!</p>
    </body></html>
    """

# -------------------------------------------------
# PIX
# -------------------------------------------------
@app.route("/criar_cobranca_pix", methods=["POST"])
def criar_cobranca_pix():
    try:
        dados = request.json
        descricao = dados.get("descricao", "Pagamento Casa do Ar")
        valor_centavos = int(dados["valor_centavos"])
        email = dados["email"]

        # 👉 Força R$1,00 para teste
        if valor_centavos > 100:
            valor_centavos = 100

        pagamento = sdk.payment().create({
            "transaction_amount": valor_centavos / 100,
            "description": descricao,
            "payment_method_id": "pix",
            "payer": {"email": email}
        })

        resposta = pagamento["response"]

        txdata = resposta.get("point_of_interaction", {}).get("transaction_data", {})

        # Salvar no Supabase
        dados_pagamento = {
            "descricao": descricao,
            "valor_centavos": valor_centavos,
            "status": resposta.get("status"),
            "mp_id": resposta.get("id"),
            "email": email,
            "created_at": datetime.utcnow().isoformat(),
        }
        salvar_pagamento_supabase(dados_pagamento)

        return jsonify({
            "id": resposta.get("id"),
            "status": resposta.get("status"),
            "qr_code": txdata.get("qr_code"),
            "qr_code_base64": txdata.get("qr_code_base64")
        })
    except Exception as e:
        print("[ERRO PIX]", str(e))
        return jsonify({"erro": str(e)}), 500

# -------------------------------------------------
# Cartão (Checkout Pro)
# -------------------------------------------------
@app.route("/criar_preferencia_cartao", methods=["POST"])
def criar_preferencia_cartao():
    try:
        dados = request.json
        descricao = dados["descricao"]
        valor_centavos = int(dados["valor_centavos"])
        email = dados["email"]

        # 👉 Força R$1,00 para teste
        if valor_centavos > 100:
            valor_centavos = 100

        preferencia = sdk.preference().create({
            "items": [{
                "title": descricao,
                "quantity": 1,
                "currency_id": "BRL",
                "unit_price": valor_centavos / 100
            }],
            "payer": {"email": email},
            "back_urls": {
                "success": "https://casadoar.ddns.net/pagamento_sucesso",
                "failure": "https://casadoar.ddns.net/pagamento_falhou",
                "pending": "https://casadoar.ddns.net/pagamento_pendente"
            },
            "auto_return": "approved"
        })

        resposta = preferencia["response"]

        # Salvar no Supabase
        dados_pagamento = {
            "descricao": descricao,
            "valor_centavos": valor_centavos,
            "status": "init",
            "mp_preference_id": resposta.get("id"),
            "email": email,
            "created_at": datetime.utcnow().isoformat(),
        }
        salvar_pagamento_supabase(dados_pagamento)

        return jsonify({
            "id": resposta.get("id"),
            "init_point": resposta.get("init_point"),
            "sandbox_init_point": resposta.get("sandbox_init_point")
        })
    except Exception as e:
        print("[ERRO CARTAO]", str(e))
        return jsonify({"erro": str(e)}), 500

# -------------------------------------------------
# Webhook
# -------------------------------------------------
@app.route("/webhook/mp", methods=["POST"])
def webhook_mp():
    try:
        evento = request.json
        print("🔔 Webhook MP recebido:", evento)

        if "data" in evento and "id" in evento["data"]:
            pagamento_id = evento["data"]["id"]
            status = evento.get("type")

            supabase.table("pagamentos").update(
                {"status": status}
            ).eq("mp_id", pagamento_id).execute()

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
