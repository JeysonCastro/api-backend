import os
import hmac
import hashlib
import json
from flask import Flask, request, jsonify
from supabase import create_client, Client
from pagamento import gerar_cobranca_link_cartao  # Importa a função de pagamento

# --- Configuração ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
# Credenciais da Efí para validar o webhook
EFI_CLIENT_ID = os.environ.get("EFI_CLIENT_ID")
EFI_CLIENT_SECRET = os.environ.get("EFI_CLIENT_SECRET")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
app = Flask(__name__)

# --- Endpoints da API ---

@app.route('/criar_cobranca_pix', methods=['POST'])
def criar_cobranca_pix():
    """
    Endpoint chamado pelo app Kivy para iniciar um pagamento.
    Ele cria a cobrança na Efí e salva um registro de 'instalação' no nosso banco.
    """
    dados_pagamento = request.get_json()
    # Ex: dados_pagamento = {'cliente_id': 1, 'ar_id': 2, 'data_id': 5, 'valor_centavos': 145000}

    # TODO: Importar a sua função `gerar_pix` do `utils/pagamento.py`
    # Por enquanto, simulamos a lógica aqui.
    try:
        # 1. Chamar a API da Efí para criar a cobrança (você já tem essa lógica)
        # Supondo que a resposta da Efí tenha 'txid', 'imagemQrcode', e 'qrcode'
        resposta_efi = {"txid": "txid_exemplo_12345", "imagemQrcode": "base64...", "qrcode": "pixcopiacola..."}
        txid_efi = resposta_efi['txid']

        # 2. Salvar o agendamento no nosso banco de dados com status 'AGUARDANDO'
        nova_instalacao = {
            'cliente_id': dados_pagamento['cliente_id'],
            'ar_id': dados_pagamento['ar_id'],
            'data_instalacao_id': dados_pagamento['data_id'],
            'status': 'AGUARDANDO',
            'txid_efi': txid_efi # MUITO IMPORTANTE: salvamos o txid
        }
        response_db = supabase.table('instalacoes').insert(nova_instalacao).execute()
        instalacao_criada = response_db.data[0]

        # 3. Retornar os dados do Pix e o ID da nossa instalação para o app Kivy
        return jsonify({
            'instalacao_id': instalacao_criada['id'],
            'imagemQrcode': resposta_efi['imagemQrcode'],
            'qrcode': resposta_efi['qrcode']
        })

    except Exception as e:
        print(f"!!! Erro ao criar cobrança: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/webhook/efi', methods=['POST'])
def webhook_efi():
    """
    Recebe e VALIDA o webhook da Efí Pay.
    """
    assinatura_recebida = request.headers.get('x-gerencianet-signature')
    notificacao_bytes = request.data

    print(">>> Webhook recebido! Validando assinatura...")

    # 1. Validação da Assinatura (Passo de Segurança Crucial)
    try:
        # Concatena o client_id e client_secret para formar o segredo
        segredo = f"{EFI_CLIENT_ID}:{EFI_CLIENT_SECRET}".encode('utf-8')
        # Cria a assinatura esperada usando HMAC-SHA256
        assinatura_esperada = hmac.new(segredo, notificacao_bytes, hashlib.sha256).hexdigest()

        if not hmac.compare_digest(assinatura_esperada, assinatura_recebida):
            print("!!! ASSINATURA INVÁLIDA! Webhook descartado.")
            return jsonify(status="assinatura_invalida"), 401 # 401 Unauthorized

        print(">>> Assinatura válida!")
        notificacao = json.loads(notificacao_bytes)

        # 2. Processar a Notificação
        if 'pix' in notificacao:
            txid = notificacao['pix'][0]['txid']
            print(f"Pagamento confirmado para o txid: {txid}")
            
            # ATUALIZA O BANCO DE DADOS
            supabase.table('instalacoes').update({'status': 'PAGO'}).eq('txid_efi', txid).execute()
            print(f"Instalação com txid {txid} atualizada para PAGO.")

    except Exception as e:
        print(f"!!! Erro ao processar o webhook: {e}")
        return jsonify(status="error", message=str(e)), 200

    # Retorna 200 OK para a Efí saber que recebemos com sucesso.
    return jsonify(status="recebido"), 200

@app.route('/instalacao/status/<int:instalacao_id>', methods=['GET'])
def get_instalacao_status(instalacao_id):
    """
    Este endpoint é para o seu app Kivy perguntar o status de um pagamento.
    """
    try:
        # Busca no Supabase o status da instalação com o ID fornecido
        response = supabase.table('instalacoes').select('status').eq('id', instalacao_id).single().execute()
        
        if response.data:
            return jsonify(response.data)
        else:
            return jsonify({"error": "Instalação não encontrada"}), 404
    except Exception as e:
        print(f"!!! Erro ao buscar status: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/criar_link_cartao', methods=['POST'])
def criar_link_cartao():
    """
    Cria uma cobrança 'Link de Pagamento' na Efí e retorna a URL para o app.
    """
    # Recebe os dados do app (cliente, produto, etc.)
    dados_cobranca = request.get_json()

    # Importa a função do seu arquivo de pagamentos
    # from utils.pagamento import gerar_cobranca_link_cartao
    
    try:
        # Chama a função que interage com a API da Efí
        # Você passaria os dados necessários, como valor, nome do item, etc.
        resposta_efi = gerar_cobranca_link_cartao(dados_cobranca)

        if not resposta_efi or resposta_efi.get('code') != 200:
            raise Exception(f"Erro da API Efí: {resposta_efi}")
            
        link_pagamento = resposta_efi['data']['link']
        charge_id = resposta_efi['data']['charge_id']

        # Opcional, mas recomendado: Salve o charge_id no seu banco de dados
        # para poder reconciliar o pagamento quando o webhook chegar.
        # supabase.table('instalacoes').update({'charge_id': charge_id}).eq('id', dados_cobranca['instalacao_id']).execute()

        print(f"Link de pagamento gerado com sucesso: {link_pagamento}")
        return jsonify({"payment_url": link_pagamento})

    except Exception as e:
        print(f"!!! Erro ao criar link de pagamento: {e}")
        return jsonify({"error": str(e)}), 500

# Esta parte não é usada pelo Render, mas é útil para testar localmente
if __name__ == "__main__":
    # O Render usa um servidor diferente (Gunicorn), ele ignora esta parte.
    app.run(debug=True, port=5001)