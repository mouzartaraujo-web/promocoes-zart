import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

from afiliados import orquestrador

termos = [
    'notebook', 'smartphone', 'smart tv', 'fone bluetooth', 'carregador rapido',
    'air fryer', 'aspirador robo', 'parafusadeira', 'furadeira', 'ssd 1tb',
    'monitor 27', 'mouse gamer', 'teclado mecanico', 'smartwatch', 'carregador gan'
]

ofertas_total = []
for termo in termos:
    print(f'\n=== Buscando: {termo} ===')
    ofertas = orquestrador.rodar(
        termo=termo,
        limite_queda_pct=5.0,
        plataformas=['mercado_livre'],
        enviar_whatsapp=False
    )
    ofertas_total.extend(ofertas)
    print(f'Ofertas encontradas para "{termo}": {len(ofertas)}')
    for o in ofertas:
        print(f'  - {o["titulo"][:60]} | {o["queda_pct"]}% OFF | {o["produto_id"]}')

print(f'\n=== TOTAL: {len(ofertas_total)} ofertas ===')