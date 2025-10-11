import os
import json
from datetime import datetime
from typing import Optional, Dict, Any

from flask import Flask, request, jsonify
from supabase import create_client, Client
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

# ---------------------------
# Inicializações
# ---------------------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
app = Flask(__name__)
sdk = mercadopago.SDK(MP_ACCESS_TOKEN)


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
def gerar_pix_mp(
    valor_centavos: int,
    email_cliente: str,
    nome_cliente: Optional[str] = None,
    cpf_cliente: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Cria um pagamento PIX (Payment API) e retorna os dados do QR Code."""
    try:
        valor = round(valor_centavos / 100.0, 2)

        payer: Dict[str, Any] = {"email": email_cliente}
        if nome_cliente:
            payer["first_name"] = nome_cliente
        if cpf_cliente:
            payer["identification"] = {"type": "CPF", "number": cpf_cliente}

        pagamento = sdk.payment().create(
            {
                "transaction_amount": valor,
                "description": "Pagamento Casa do Ar",
                "payment_method_id": "pix",
                "payer": payer,
            }
        )

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
def gerar_preferencia_cartao_mp(
    valor_centavos: int,
    email_cliente: str,
    titulo_item: str = "Pagamento Casa do Ar",
) -> Optional[Dict[str, Any]]:
    """Gera uma preferência do Checkout Pro (cartão)."""
    try:
        valor = round(valor_centavos / 100.0, 2)

        payment_methods = {
            "excluded_payment_methods": [{"id": "pix"}, {"id": "bolbradesco"}],
            "excluded_payment_types": [{"id": "ticket"}],  # exclui boleto
            "installments": 12,
        }

        preferencia = sdk.preference().create(
            {
                "items": [{"title": titulo_item, "quantity": 1, "unit_price": valor}],
                "payer": {"email": email_cliente},
                "payment_methods": payment_methods,
                "back_urls": {
                    "success": "casadoar://pagamento/sucesso",
                    "failure": "casadoar://pagamento/falha",
                    "pending": "casadoar://pagamento/pendente",
                },
                "auto_return": "approved",
            }
        )

        return {
            "id": preferencia["response"]["id"],
            "init_point": preferencia["response"]["init_point"],
        }

    except Exception as e:
        print(f"[MP][CARD] Erro: {e}")
        return None


# ---------------------------
# Rotas
# ---------------------------
@app.route("/")
def index():
    return "<h1>API da Casa do Ar (Mercado Pago) OK!</h1>"


@app.route("/health")
def health():
    return jsonify(ok=True, time=datetime.utcnow().isoformat())


# PIX
@app.route("/criar_cobranca_pix", methods=["POST"])
def criar_cobranca_pix_endpoint():
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

        nova_inst = {
            "user_data": dados["user_data"],
            "ar_id": dados["ar_id"],
            "data_instalacao_id": dados["data_id"],
            "status": "AGUARDANDO_PAGAMENTO",
            "txid_efi": str(resp_mp["payment_id"]),
        }
        response_db = supabase.table("instalacoes").insert(nova_inst).execute()
        inst = response_db.data[0]

        return jsonify(
            {
                "instalacao_id": inst["id"],
                "qrcode": resp_mp.get("qr_code"),
                "imagemQrcode": resp_mp.get("qr_code_base64"),
                "payment_id": resp_mp["payment_id"],
            }
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# CARTÃO
@app.route("/criar_link_cartao", methods=["POST"])
def criar_link_cartao_endpoint():
    dados = request.get_json(force=True)
    try:
        pref = gerar_preferencia_cartao_mp(
            valor_centavos=dados["valor_centavos"],
            email_cliente=dados["email"],
        )

        if not pref or not pref.get("init_point"):
            raise Exception(f"Erro ao criar preferência MP: {pref}")

        nova_inst = {
            "user_data": dados["user_data"],
            "ar_id": dados["ar_id"],
            "data_instalacao_id": dados["data_id"],
            "status": "AGUARDANDO_PAGAMENTO",
            "txid_efi": str(pref["id"]),
        }
        response_db = supabase.table("instalacoes").insert(nova_inst).execute()
        inst = response_db.data[0]

        return jsonify(
            {
                "instalacao_id": inst["id"],
                "payment_url": pref["init_point"],
                "preference_id": pref["id"],
                "public_key": MP_PUBLIC_KEY,
            }
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# STATUS
@app.route("/instalacao/status/<int:instalacao_id>", methods=["GET"])
def get_instalacao_status(instalacao_id: int):
    try:
        response = (
            supabase.table("instalacoes")
            .select("*")
            .eq("id", instalacao_id)
            .single()
            .execute()
        )
        if not response.data:
            return jsonify({"error": "Instalação não encontrada"}), 404
        return jsonify(response.data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


eventos_processados = {}

def processar_webhook_mp(evento):
    try:
        data_obj = evento.get("data", {}) or {}
        payment_id = data_obj.get("id")

        if not payment_id:
            print("[WEBHOOK MP] Ignorado: sem payment_id")
            return

        pagamento = sdk.payment().get(payment_id)
        resp = pagamento.get("response", {}) or {}
        status = resp.get("status")
        order = resp.get("order") or {}
        pref_id = order.get("id")

        print(f"[WEBHOOK MP] processando payment_id={payment_id} status={status}")

        # 🔒 Evita processar duplicados
        chave = f"{payment_id}-{status}"
        if chave in eventos_processados:
            print(f"[WEBHOOK MP] Ignorado (duplicado): {chave}")
            return
        eventos_processados[chave] = datetime.utcnow()

        # 🔧 Limpa cache antigo (mantém os últimos 100 eventos)
        if len(eventos_processados) > 100:
            eventos_processados.pop(next(iter(eventos_processados)))

        # 🔹 Busca status atual no Supabase
        result = (
            supabase.table("instalacoes")
            .select("status")
            .eq("txid_efi", str(payment_id))
            .execute()
        )

        status_atual = result.data[0]["status"] if result.data else None

        # 🔒 Impede downgrade: se já está PAGO, não muda para FALHA
        if status_atual == "PAGO" and status in ("rejected", "cancelled"):
            print(f"[WEBHOOK MP] Ignorado downgrade: {payment_id} já PAGO")
            return

        # 🟢 Atualiza status no banco
        if status == "approved":
            supabase.table("instalacoes").update(
                {"status": "PAGO"}
            ).eq("txid_efi", str(payment_id)).execute()

            if pref_id:
                supabase.table("instalacoes").update(
                    {"status": "PAGO", "txid_efi": str(payment_id)}
                ).eq("txid_efi", str(pref_id)).execute()

        elif status in ("rejected", "cancelled"):
            supabase.table("instalacoes").update(
                {"status": "FALHA"}
            ).eq("txid_efi", str(payment_id)).execute()

        print(f"[WEBHOOK MP] Finalizado: payment_id={payment_id} → {status}")

    except Exception as e:
        print(f"[WEBHOOK MP] ERRO interno: {e}")


@app.route("/webhook/mercadopago", methods=["POST", "GET"])
def webhook_mercadopago():
    try:
        if request.method == "GET":
            return jsonify({"status": "ok"}), 200

        evento = request.get_json(force=True) or {}
        print(f"[WEBHOOK MP RAW] {json.dumps(evento, indent=2)}")

        # Processa em thread para não travar o worker
        threading.Thread(target=processar_webhook_mp, args=(evento,)).start()

        # Retorna rápido para o Mercado Pago
        return jsonify({"ok": True}), 200

    except Exception as e:
        print(f"[WEBHOOK MP] ERRO geral: {e}")
        return jsonify({"error": str(e)}), 500
# ---------------------------
# Execução local
# ---------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)


