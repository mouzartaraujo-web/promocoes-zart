#!/usr/bin/env python3
"""
Script to run the PromoZart crawler.
Called by GitHub Actions workflow.
"""

import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Read URLs from config file
with open('config/urls_monitoradas.txt', 'r') as f:
    urls = [line.strip() for line in f if line.strip() and not line.startswith('#')]

print(f'URLs carregadas: {len(urls)}')
for u in urls:
    print(f'  - {u}')

from afiliados import orquestrador

ofertas = orquestrador.rodar(
    urls_mercado_livre=urls,
    limite_queda_pct=10.0,
    enviar_whatsapp=False
)

print(f'Ofertas encontradas: {len(ofertas)}')
for o in ofertas:
    titulo = o['titulo'][:60]
    queda = o['queda_pct']
    pid = o['produto_id']
    print(f'  - {titulo} | {queda}% OFF | {pid}')