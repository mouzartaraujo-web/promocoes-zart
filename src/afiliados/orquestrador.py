"""Orquestrador principal do fluxo de captação de ofertas."""

import logging
from datetime import datetime
from typing import Any

from afiliados import armazenamento, links, mercado_livre, mercado_livre_parser, shopee, whatsapp

logger = logging.getLogger(__name__)


# Constantes de qualidade
MAX_QUEDA_PCT = 90.0  # Ignora quedas absurdas (> 90%)
MIN_PRECO = 0.01  # Preço mínimo válido


def _calcular_queda_pct(preco_anterior: float, preco_novo: float) -> float:
    """Calcula a porcentagem de queda do preço."""
    if preco_anterior <= 0:
        return 0.0
    return ((preco_anterior - preco_novo) / preco_anterior) * 100


def _validar_precos(preco_anterior: float, preco_novo: float) -> bool:
    """
    Valida se os preços são razoáveis.

    Returns:
        True se preços válidos, False caso contrário.
    """
    if preco_anterior <= MIN_PRECO or preco_novo <= MIN_PRECO:
        return False
    queda_pct = _calcular_queda_pct(preco_anterior, preco_novo)
    if queda_pct > MAX_QUEDA_PCT:
        return False
    return True


def _buscar_produtos_plataforma(
    plataforma: str, termo: str, limite: int = 20
) -> list[dict[str, Any]]:
    """
    Chama a função de busca da plataforma correspondente.

    Args:
        plataforma: Nome da plataforma ("mercado_livre", "shopee").
        termo: Termo de busca.
        limite: Limite de resultados.

    Returns:
        Lista de produtos normalizados.
    """
    if plataforma == "mercado_livre":
        return list(mercado_livre.buscar_produtos(termo, limite))
    if plataforma == "shopee":
        return list(shopee.buscar_produtos(termo, limite))
    raise ValueError(f"Plataforma desconhecida: {plataforma}")


def _extrair_produtos_urls_ml(urls: list[str]) -> list[dict[str, Any]]:
    """
    Extrai dados de produtos do Mercado Livre a partir de URLs.

    Args:
        urls: Lista de URLs de produtos do Mercado Livre.

    Returns:
        Lista de produtos normalizados com id, titulo, preco, link.
    """
    return mercado_livre_parser.extrair_produtos_das_urls(urls)


def _processar_produto(
    produto: dict[str, Any],
    plataforma: str,
    limite_queda_pct: float,
    agora: datetime,
    enviar_whatsapp: bool = False,
    preco_minimo: float | None = None,
    preco_maximo: float | None = None,
) -> dict[str, Any] | None:
    """
    Processa um produto: verifica queda de preço, gera link afiliado, salva oferta.

    Args:
        produto: Dados do produto.
        plataforma: Nome da plataforma.
        limite_queda_pct: Limite mínimo de queda percentual.
        agora: Timestamp atual.
        enviar_whatsapp: Se True, tenta enviar notificação via WhatsApp.
        preco_minimo: Preço mínimo aceitável (opcional).
        preco_maximo: Preço máximo aceitável (opcional).

    Returns:
        Dicionário da oferta se houver queda >= limite, senão None.
    """
    produto_id = produto["id"]
    preco_atual = produto["preco"]
    titulo = produto["titulo"]
    link_original = produto["link"]

    # Filtro: preço zero ou negativo
    if preco_atual <= MIN_PRECO:
        logger.debug("Produto %s ignorado: preço atual inválido (%.2f)", produto_id, preco_atual)
        return None

    preco_salvo = armazenamento.buscar_preco_salvo(produto_id, plataforma)

    # Se não há preço salvo, apenas salva o atual e não considera oferta
    if preco_salvo is None:
        armazenamento.salvar_produto(
            produto_id=produto_id,
            titulo=titulo,
            preco_novo=preco_atual,
            preco_anterior=None,
            link=link_original,
            agora=agora,
            plataforma=plataforma,
            imagem=produto.get("imagem"),
        )
        return None

    queda_pct = _calcular_queda_pct(preco_salvo, preco_atual)

    # Atualiza sempre o preço salvo
    armazenamento.salvar_produto(
        produto_id=produto_id,
        titulo=titulo,
        preco_novo=preco_atual,
        preco_anterior=preco_salvo,
        link=link_original,
        agora=agora,
        plataforma=plataforma,
        imagem=produto.get("imagem"),
    )

    # Filtro: queda não atinge o limite
    if queda_pct < limite_queda_pct:
        return None

    # Filtro: preços inválidos ou queda absurda
    if not _validar_precos(preco_salvo, preco_atual):
        logger.debug(
            "Produto %s ignorado: preços inválidos (anterior=%.2f, atual=%.2f, queda=%.1f%%)",
            produto_id,
            preco_salvo,
            preco_atual,
            queda_pct,
        )
        return None

    # Filtro: preço fora do range configurado
    if preco_minimo is not None and preco_atual < preco_minimo:
        logger.debug(
            "Produto %s ignorado: preço %.2f abaixo do mínimo %.2f",
            produto_id,
            preco_atual,
            preco_minimo,
        )
        return None
    if preco_maximo is not None and preco_atual > preco_maximo:
        logger.debug(
            "Produto %s ignorado: preço %.2f acima do máximo %.2f",
            produto_id,
            preco_atual,
            preco_maximo,
        )
        return None

    # Filtro: oferta duplicada recente (24h)
    if armazenamento.oferta_recente_existe(produto_id, plataforma, horas=24):
        logger.debug("Produto %s ignorado: oferta recente já existe", produto_id)
        return None

    # Gera link de afiliado
    link_afiliado = links.gerar_link_afiliado(produto, plataforma)

    # Cria e salva a oferta
    oferta = {
        "produto_id": produto_id,
        "titulo": titulo,
        "preco_anterior": preco_salvo,
        "preco_novo": preco_atual,
        "queda_pct": round(queda_pct, 2),
        "link": link_afiliado,
        "plataforma": plataforma,
        "imagem": produto.get("imagem"),
    }
    armazenamento.salvar_oferta(oferta)

    # Envia notificação WhatsApp se solicitado
    if enviar_whatsapp:
        try:
            whatsapp.enviar_oferta(oferta)
        except whatsapp.WhatsAppError as e:
            logger.warning("Falha ao enviar WhatsApp para oferta %s: %s", produto_id, e)

    return oferta


def rodar(
    termo: str = "",
    limite_queda_pct: float = 10.0,
    plataformas: list[str] | None = None,
    enviar_whatsapp: bool = False,
    preco_minimo: float | None = None,
    preco_maximo: float | None = None,
    urls_mercado_livre: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Executa uma rodada de busca e detecção de ofertas.

    Args:
        termo: Termo de busca (usado em plataformas que não sejam ML por URL).
        limite_queda_pct: Porcentagem mínima de queda para considerar oferta (padrão: 10.0).
        plataformas: Lista de plataformas para buscar via API (padrão: ["mercado_livre"]).
        enviar_whatsapp: Se True, envia notificações via WhatsApp para cada oferta (padrão: False).
        preco_minimo: Preço mínimo aceitável para ofertas (opcional).
        preco_maximo: Preço máximo aceitável para ofertas (opcional).
        urls_mercado_livre: Lista de URLs de produtos do Mercado Livre para extrair dados via HTML parsing.
                           Se fornecido, ignora busca por termo no ML e usa estas URLs.

    Returns:
        Lista de ofertas encontradas nesta rodada.
    """
    if plataformas is None:
        plataformas = ["mercado_livre"]

    logger.info(
        "Iniciando rodada: termo='%s', queda_min=%.1f%%, plataformas=%s, whatsapp=%s, preco_min=%s, preco_max=%s, urls_ml=%d",
        termo,
        limite_queda_pct,
        plataformas,
        enviar_whatsapp,
        preco_minimo,
        preco_maximo,
        len(urls_mercado_livre) if urls_mercado_livre else 0,
    )

    agora = datetime.now()
    ofertas_encontradas = []

    # 1. Expirar ofertas antigas (preço normalizado ou sem atualização por 48h)
    try:
        expiradas = armazenamento.expirar_ofertas_desatualizadas(horas_sem_atualizacao=48)
        if expiradas > 0:
            logger.info("Expiradas %d ofertas antigas/normalizadas", expiradas)
    except Exception as exc:
        logger.warning("Erro ao expirar ofertas antigas: %s", exc)

    ofertas_encontradas = []

    # Processa URLs do Mercado Livre se fornecidas
    if urls_mercado_livre:
        try:
            produtos_ml = _extrair_produtos_urls_ml(urls_mercado_livre)
        except mercado_livre_parser.MercadoLivreParserError as e:
            logger.warning("Erro ao extrair produtos do ML via URLs: %s", e)
            produtos_ml = []

        for produto in produtos_ml:
            try:
                oferta = _processar_produto(
                    produto,
                    "mercado_livre",
                    limite_queda_pct,
                    agora,
                    enviar_whatsapp,
                    preco_minimo,
                    preco_maximo,
                )
                if oferta:
                    ofertas_encontradas.append(oferta)
            except (armazenamento.SupabaseError, links.PlataformaNaoImplementada) as e:
                logger.warning("Erro ao processar produto %s: %s", produto.get("id"), e)
                continue

        # Remove mercado_livre das plataformas para não buscar via API também
        plataformas = [p for p in plataformas if p != "mercado_livre"]

    # Processa demais plataformas via API (busca por termo)
    for plataforma in plataformas:
        try:
            produtos = _buscar_produtos_plataforma(plataforma, termo)
        except (mercado_livre.MercadoLivreError, shopee.ShopeeError) as e:
            logger.warning("Erro ao buscar produtos no %s: %s", plataforma, e)
            continue

        for produto in produtos:
            try:
                oferta = _processar_produto(
                    produto,
                    plataforma,
                    limite_queda_pct,
                    agora,
                    enviar_whatsapp,
                    preco_minimo,
                    preco_maximo,
                )
                if oferta:
                    ofertas_encontradas.append(oferta)
            except (armazenamento.SupabaseError, links.PlataformaNaoImplementada) as e:
                logger.warning("Erro ao processar produto %s: %s", produto.get("id"), e)
                continue

    logger.info("Rodada concluída: %d ofertas encontradas", len(ofertas_encontradas))
    return ofertas_encontradas
