# -------------------------------------------------------------------
# ARQUIVO: utils/pagamento.py (Versão Final e Oficial)
# DESCRIÇÃO: Este arquivo contém a lógica "pura" para se comunicar com a API da Efí.
# Ele é projetado para rodar no seu servidor (backend) e não contém
# nenhuma dependência da interface gráfica (Kivy/KivyMD).
# -------------------------------------------------------------------

# --- Imports Essenciais ---
import os
import certifi 
from efipay import EfiPay
from typing import Tuple, Optional, Dict, Any

# --- Configuração da API Efí ---
# Este caminho assume que o certificado está na mesma pasta 'utils' que este script.
# No servidor, você deve garantir que a estrutura de pastas seja a mesma.
certificado_path = os.path.join(os.path.dirname(__file__), 'producao_cert.pem')

# Dicionário de configuração UNIFICADO.
# Ele busca as chaves das variáveis de ambiente do seu servidor.
config = {
    'client_id': os.environ.get('EFI_CLIENT_ID'),
    'client_secret': os.environ.get('EFI_CLIENT_SECRET'),
    'sandbox': False,
    'certificate': certificado_path,
    # A chave correta que a biblioteca 'requests' usa para verificação SSL
    'verify_ssl': certifi.where() 
}


# --- FUNÇÃO GERAR_PIX - VERSÃO OFICIAL ---
def gerar_pix(valor_centavos: int, nome_cliente: str, cpf_cliente: str) -> Optional[Dict[str, Any]]:
    """
    Gera uma cobrança PIX e retorna um dicionário com todos os dados, incluindo o txid.
    """
    try:
        api = EfiPay(config)
        body = {
            "calendario": {"expiracao": 3600},
            "devedor": {"nome": nome_cliente, "cpf": cpf_cliente},
            "valor": {"original": f"{valor_centavos / 100:.2f}"},
            "chave": "SUA_CHAVE_PIX_CADASTRADA_NA_EFI", # Substitua pela sua chave Pix
            "solicitacaoPagador": "Pagamento de locação de ar-condicionado"
        }

        response_cobranca = api.pix_create_immediate_charge(body=body)
        if "loc" not in response_cobranca:
            raise Exception(f"Erro ao criar cobrança PIX: {response_cobranca}")

        loc_id = response_cobranca["loc"]["id"]
        response_qrcode = api.pix_generate_qrcode(params={"id": loc_id})

        # Monta um dicionário completo para retornar para a sua api.py
        resultado_final = {
            "txid": response_cobranca.get("txid"),
            "imagemQrcode": response_qrcode.get("imagemQrcode"),
            "qrcode": response_qrcode.get("qrcode")
        }
        return resultado_final

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Erro detalhado na função gerar_pix: {e}")
        return None


# --- FUNÇÃO GERAR LINK DE CARTÃO - VERSÃO OFICIAL ---
def gerar_cobranca_link_cartao(dados_cobranca: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Cria uma cobrança do tipo 'Link de Pagamento' na Efí para cartão de crédito.
    """
    try:
        api = EfiPay(config)
        valor = dados_cobranca.get("valor_centavos", 1000)
        nome_item = dados_cobranca.get("nome_item", "Serviço de Locação")

        body = {
            "items": [{
                "name": nome_item,
                "value": valor,
                "amount": 1
            }],
            "settings": {
                "payment_method": "credit_card" 
            }
        }
        
        response = api.create_one_step_link(body=body)
        
        print("Resposta da API Efí (Link de Pagamento):", response)
        return response

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Erro detalhado ao gerar link de pagamento: {e}")
        return None
