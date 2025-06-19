from efipay import EfiPay
import os
from kivy.utils import platform
from kivy.app import App
import certifi
from typing import Tuple, Optional, Dict, Any

# Define caminho do certificado
if platform == 'android':
    # No Android, o arquivo estará no diretório privado do app
    # O Buildozer copia os arquivos para 'assets/app/'
    from android.storage import app_storage_path
    base_path = os.path.join(app_storage_path(), 'app')
    certificado_path = os.path.join(base_path, 'utils', 'producao_cert.pem')
else:
    # Para testes no PC
    certificado_path = os.path.join(os.path.dirname(__file__), 'producao_cert.pem')


config = {
    "client_id": "Client_Id_70ce5885d42738263f06a2e212657519b4f432cd",
    "client_secret": "Client_Secret_70606bd21a28a4550e06c1047b089ef7a0e8e5f3",
    "sandbox": False,
    "certificate": certificado_path,
    "timeout": 60,
    "verify_ssl": certifi.where()  # Garante verificação segura de SSL
}

def gerar_pix(valor_centavos: int, nome_cliente: str, cpf_cliente: str) -> Tuple[Optional[str], Optional[str]]:

    app = App.get_running_app()
    user_data = getattr(app, 'user_data', {})
    product_data = getattr(app, 'product_data', {})

    nome_cliente = user_data.get("nome", "Cliente Teste")
    cpf_cliente = user_data.get("cpf", "12345678909")
    valor_centavos = product_data.get("valor_centavos", 93600)

    body = {
        "calendario": {"expiracao": 3600},
        "devedor": {
            "nome": nome_cliente,
            "cpf": cpf_cliente
        },
        "valor": {
            "original": f"{valor_centavos / 100:.2f}"
        },
        "chave": "85938122015",
        "solicitacaoPagador": "Pagamento de locação de ar-condicionado"
    }

    try:
        api = EfiPay(config)
        response = api.pix_create_immediate_charge(params={}, body=body)

        if "loc" not in response:
            print("Erro: resposta da Efipay não contém 'loc'")
            print("Resposta completa:", response)
            return None, None

        loc_id = response["loc"]["id"]
        qr_code = api.pix_generate_qrcode(params={"id": loc_id})
        return qr_code["imagemQrcode"], qr_code["qrcode"]

    except Exception as e:
        print("Erro ao gerar Pix:", e)
        return None, None

def pagar_com_cartao():
    app = App.get_running_app()
    user_data = getattr(app, 'user_data', {})
    product_data = getattr(app, 'product_data', {})

    dados = {
        "nome": user_data.get("nome", "Cliente Teste"),
        "cpf": user_data.get("cpf", "12345678909"),
        "telefone": user_data.get("telefone", "00000000000"),
        "email": user_data.get("email", "email@teste.com"),
        "token": app.cartao_token,
        "parcelas": product_data.get("parcelas", 1)
    }

    valor_centavos = product_data.get("valor_centavos", 93600)

    payment_data = {
        "items": [{
            "name": "Locação de Ar-condicionado",
            "amount": 1,
            "value": valor_centavos
        }],
        "shippings": [{
            "name": "Entrega",
            "value": 0
        }],
        "payment": {
            "credit_card": {
                "installments": int(dados["parcelas"]),
                "payment_token": dados["token"],
                "billing_address": {
                    "street": "Rua Exemplo",
                    "number": 100,
                    "neighborhood": "Bairro",
                    "zipcode": "12345678",
                    "city": "Cidade",
                    "state": "SP"
                },
                "customer": {
                    "name": dados["nome"],
                    "cpf": dados["cpf"],
                    "phone_number": dados["telefone"],
                    "email": dados["email"],
                    "birth": "1990-01-01"
                }
            }
        }
    }

    try:
        api = EfiPay(config)
        resposta = api.create_charge(None, payment_data)
        return resposta
    except Exception as e:
        print("Erro ao processar cartão:", e)
        return None
