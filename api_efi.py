# Crie um novo arquivo no seu computador (pode ser na pasta do seu backend),
# chame-o de, por exemplo, 'configura_webhook.py'

import os
import certifi
from efipay import EfiPay

# --- PREENCHA COM SUAS INFORMAÇÕES ---

# A sua chave Pix que receberá os pagamentos
SUA_CHAVE_PIX = "85938122015"

# A URL completa do seu webhook no Render
URL_DO_WEBHOOK = "https://projeto-ar-backend.onrender.com/webhook/efi" # Substitua se a sua URL for diferente

# --- CONFIGURAÇÃO UNIFICADA ---
# Agora, todas as configurações ficam em um único dicionário.

config = {
    'client_id': 'Client_Id_70ce5885d42738263f06a2e212657519b4f432cd',
    'client_secret': 'Client_Secret_70606bd21a28a4550e06c1047b089ef7a0e8e5f3',
    'sandbox': False,
    # Garanta que o seu certificado .pem está na mesma pasta que este script
    'certificate': 'producao_cert.pem',
    # --- A CORREÇÃO ESTÁ AQUI ---
    # Adicionamos a verificação SSL diretamente no dicionário principal
    'verify_ssl': certifi.where()
}

# --- FIM DA CONFIGURAÇÃO ---


print("--- Iniciando configuração do Webhook na Efí Pay ---")

try:
    # Agora a chamada é limpa, com apenas um argumento
    api = EfiPay(config)

    # Corpo da requisição: passamos a URL do nosso webhook
    body = {
        "webhookUrl": URL_DO_WEBHOOK
    }
    
    # Parâmetros: especificamos para qual chave Pix esta configuração se aplica
    params = {
        'chave': SUA_CHAVE_PIX
    }

    print(f"Enviando configuração para a chave Pix: {SUA_CHAVE_PIX}")
    print(f"URL do Webhook a ser configurada: {URL_DO_WEBHOOK}")

    # Esta é a chamada de API que configura o webhook
    response = api.pix_config_webhook(params=params, body=body)

    print("\n--- RESPOSTA DA EFI ---")
    print(response)
    print("\nSUCESSO! Seu webhook foi configurado na Efí Pay.")

except Exception as e:
    print("\n--- OCORREU UM ERRO ---")
    print(f"Não foi possível configurar o webhook. Erro: {e}")

