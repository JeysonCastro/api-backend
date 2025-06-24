# --- Imports Essenciais ---
import os
import hmac
import hashlib
import json
from flask import Flask, request, jsonify
from supabase import create_client, Client
import certifi 
from efipay import EfiPay
from typing import Optional, Dict, Any

# --- 1. Configuração Centralizada ---

# Carrega as credenciais seguras das Variáveis de Ambiente do servidor
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
EFI_CLIENT_ID = os.environ.get("EFI_CLIENT_ID")
EFI_CLIENT_SECRET = os.environ.get("EFI_CLIENT_SECRET")

# Configuração para a API da Efí Pay
# Assume que 'producao_cert.pem' está na mesma pasta que este script
certificado_path = os.path.join(os.path.dirname(__file__), 'producao_cert.pem')

efi_config = {
    'client_id': EFI_CLIENT_ID,
    'client_secret': EFI_CLIENT_SECRET,
    'sandbox': False,
    'certificate': certificado_path,
    # Força o uso do pacote 'certifi' para garantir a validação SSL
    'verify_ssl': certifi.where() 
}

# Inicialização dos clientes
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    app = Flask(__name__)
except Exception as e:
    print(f"ERRO CRÍTICO ao inicializar os serviços: {e}")


# --- 2. Funções de Lógica de Pagamento ---
# Estas funções agora vivem dentro do mesmo arquivo da API para simplicidade.

def gerar_pix(valor_centavos: int, nome_cliente: str, cpf_cliente: str) -> Optional[Dict[str, Any]]:
    """ Gera uma cobrança PIX e retorna um dicionário com todos os dados. """
    try:
        api = EfiPay(efi_config)
        body = {
            "calendario": {"expiracao": 3600},
            "devedor": {"nome": nome_cliente, "cpf": cpf_cliente},
            "valor": {"original": f"{valor_centavos / 100:.2f}"},
            "chave": os.environ.get("EFI_PIX_CHAVE"), # SUBSTITUA PELA SUA CHAVE PIX
            "solicitacaoPagador": "Pagamento de locação de ar-condicionado"
        }
        response_cobranca = api.pix_create_immediate_charge(body=body)
        if "loc" not in response_cobranca:
            raise Exception(f"Erro ao criar cobrança PIX: {response_cobranca}")

        loc_id = response_cobranca["loc"]["id"]
        response_qrcode = api.pix_generate_qrcode(params={"id": loc_id})

        return {
            "txid": response_cobranca.get("txid"),
            "imagemQrcode": response_qrcode.get("imagemQrcode"),
            "qrcode": response_qrcode.get("qrcode")
        }
    except Exception as e:
        print(f"Erro detalhado na função gerar_pix: {e}")
        return None

def gerar_cobranca_link_cartao(dados_cobranca: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """ Cria um link de pagamento para cartão de crédito. """
    try:
        api = EfiPay(efi_config)
        body = {
            "items": [{
                "name": nome_item,
                "value": valor,
                "amount": 1
            }],
            "settings": {
                "payment_method": "credit_card",
                # Adicionamos a propriedade obrigatória. False significa que
                # a Efí NÃO vai pedir o endereço de entrega na página de pagamento.
                "request_delivery_address": False
            }
        }
        response = api.create_one_step_link(body=body)
        return response
    except Exception as e:
        print(f"Erro detalhado na função gerar_cobranca_link_cartao: {e}")
        return None


# --- 3. Endpoints (Rotas) da API ---

@app.route('/')
def index():
    return "<h1>API da Casa do Ar está funcionando!</h1>"

@app.route('/criar_cobranca_pix', methods=['POST'])
def criar_cobranca_pix_endpoint():
    dados_pagamento = request.get_json()
    try:
        resposta_efi = gerar_pix(
            valor_centavos=dados_pagamento['valor_centavos'],
            nome_cliente=dados_pagamento['nome_cliente'],
            cpf_cliente=dados_pagamento['cpf_cliente']
        )
        if not resposta_efi:
            raise Exception("Falha ao gerar dados do PIX na API da Efí.")

        nova_instalacao = {
            'cliente_id': dados_pagamento['cliente_id'], 'ar_id': dados_pagamento['ar_id'],
            'data_instalacao_id': dados_pagamento['data_id'], 'status': 'AGUARDANDO_PAGAMENTO',
            'txid_efi': resposta_efi.get('txid')
        }
        response_db = supabase.table('instalacoes').insert(nova_instalacao).execute()
        instalacao_criada = response_db.data[0]

        return jsonify({
            'instalacao_id': instalacao_criada['id'],
            'imagemQrcode': resposta_efi.get('imagemQrcode'),
            'qrcode': resposta_efi.get('qrcode')
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/criar_link_cartao', methods=['POST'])
def criar_link_cartao_endpoint():
    dados_cobranca = request.get_json()
    try:
        resposta_efi = gerar_cobranca_link_cartao(dados_cobranca)
        if not resposta_efi or 'data' not in resposta_efi or 'link' not in resposta_efi['data']:
            raise Exception(f"Erro da API Efí ao gerar link: {resposta_efi}")
            
        link_pagamento = resposta_efi['data']['link']
        return jsonify({"payment_url": link_pagamento})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/webhook/efi', methods=['POST'])
def webhook_efi():
    assinatura_recebida = request.headers.get('x-gerencianet-signature')
    notificacao_bytes = request.data
    try:
        segredo = f"{EFI_CLIENT_ID}:{EFI_CLIENT_SECRET}".encode('utf-8')
        assinatura_esperada = hmac.new(segredo, notificacao_bytes, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(assinatura_esperada, assinatura_recebida):
            print("!!! ASSINATURA INVÁLIDA! Webhook descartado.")
            return jsonify(status="assinatura_invalida"), 401
        
        notificacao = json.loads(notificacao_bytes)
        if 'pix' in notificacao:
            txid = notificacao['pix'][0]['txid']
            supabase.table('instalacoes').update({'status': 'PAGO'}).eq('txid_efi', txid).execute()
            print(f"Instalação com txid {txid} atualizada para PAGO.")
    except Exception as e:
        print(f"!!! Erro ao processar o webhook: {e}")
    return jsonify(status="recebido"), 200

@app.route('/instalacao/status/<int:instalacao_id>', methods=['GET'])
def get_instalacao_status(instalacao_id):
    try:
        response = supabase.table('instalacoes').select('status').eq('id', instalacao_id).single().execute()
        return jsonify(response.data) if response.data else (jsonify({"error": "Instalação não encontrada"}), 404)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
