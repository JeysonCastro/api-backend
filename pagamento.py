
import os
import certifi 
from efipay import EfiPay
from typing import Tuple, Optional, Dict, Any


certificado_path = os.path.join(os.path.dirname(__file__), 'producao_cert.pem')


config = {
    'client_id': os.environ.get('EFI_CLIENT_ID'),
    'client_secret': os.environ.get('EFI_CLIENT_SECRET'),
    'sandbox': False,
    'certificate': certificado_path,

    'verify_ssl': certifi.where() 
}


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
            "chave": os.environ.get("EFI_PIX_CHAVE"), # Substitua pela sua chave Pix
            "solicitacaoPagador": "Pagamento de locação de ar-condicionado"
        }

        response_cobranca = api.pix_create_immediate_charge(body=body)
        if "loc" not in response_cobranca:
            raise Exception(f"Erro ao criar cobrança PIX: {response_cobranca}")

        loc_id = response_cobranca["loc"]["id"]
        response_qrcode = api.pix_generate_qrcode(params={"id": loc_id})

        
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
