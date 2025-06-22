# No seu arquivo utils/pagamento.py

# --- Imports Essenciais (SEM KIVY) ---
import os
import certifi 
from efipay import EfiPay
from typing import Tuple, Optional, Dict, Any

# --- Configuração da API Efí ---
# Agora, o caminho para o certificado é simples. Ele assume que o arquivo
# 'producao_cert.pem' está nesta mesma pasta 'utils'.
certificado_path = os.path.join(os.path.dirname(__file__), 'producao_cert.pem')

# Dicionário de configuração UNIFICADO
# Lembre-se de preencher com suas chaves reais de produção.
# Essas chaves devem ser configuradas como variáveis de ambiente no seu servidor.
config = {
    'client_id': os.environ.get('EFI_CLIENT_ID'),
    'client_secret': os.environ.get('EFI_CLIENT_SECRET'),
    'sandbox': False,
    'certificate': certificado_path,
    'verify_ssl': certifi.where() # A chave correta é 'verify_ssl'
}


# --- FUNÇÃO GERAR_PIX - PURA ---
def gerar_pix(valor_centavos: int, nome_cliente: str, cpf_cliente: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        # A inicialização agora usa o 'config' unificado
        api = EfiPay(config)

        body = {
            "calendario": {"expiracao": 3600},
            "devedor": {
                "nome": nome_cliente,
                "cpf": cpf_cliente
            },
            "valor": {
                "original": f"{valor_centavos / 100:.2f}"
            },
            "chave": "SUA_CHAVE_PIX_CADASTRADA_NA_EFI",
            "solicitacaoPagador": "Pagamento de locação de ar-condicionado"
        }

        response = api.pix_create_immediate_charge(body=body)

        if "loc" not in response:
            print("Erro: resposta da Efipay não contém 'loc'. Resposta:", response)
            return None, None

        loc_id = response["loc"]["id"]
        qr_code = api.pix_generate_qrcode(params={"id": loc_id})
        return qr_code.get("imagemQrcode"), qr_code.get("qrcode")

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Erro detalhado ao gerar Pix: {e}")
        return None, None


# --- FUNÇÃO GERAR LINK DE CARTÃO - PURA ---
def gerar_cobranca_link_cartao(dados_cobranca: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        # A inicialização agora usa o 'config' unificado
        api = EfiPay(config)

        valor = dados_cobranca.get("valor_centavos", 1000)
        nome_item = dados_cobranca.get("nome_item", "Serviço de Locação")

        body = {
            "items": [{
                "name": nome_item,
                "value": valor,
                "amount": 1
            }],
            "settings": {"payment_method": "credit_card"}
        }
        
        response = api.create_one_step_link(body=body)
        
        print("Resposta da API Efí (Link de Pagamento):", response)
        return response

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Erro detalhado ao gerar link de pagamento: {e}")
        return None

