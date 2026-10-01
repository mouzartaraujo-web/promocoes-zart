"""Módulo para integração com a API do Mercado Livre."""

import logging
import os
import time
from typing import Any

import requests
from bs4 import BeautifulSoup

from afiliados.http_utils import retry_com_backoff

logger = logging.getLogger(__name__)


class MercadoLivreError(Exception):
    """Exceção base para erros do módulo Mercado Livre."""

    pass


class ErroRedeMercadoLivre(MercadoLivreError):
    """Erro de rede ao consultar a API do Mercado Livre."""

    pass


class ErroRespostaMercadoLivre(MercadoLivreError):
    """Erro na resposta da API do Mercado Livre."""

    pass


class ErroCredenciaisMercadoLivre(MercadoLivreError):
    """Credenciais do Mercado Livre não configuradas."""

    pass


# Constantes
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
BASE_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json, text/html, */*"}

# Constantes
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
BASE_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json, text/html, */*"}

_TOKEN_CACHE: dict[str, Any] = {}


@retry_com_backoff(max_tentativas=3, backoff_base=1.0)
def _requisicao_busca_publica(termo: str, limite: int) -> dict[str, Any]:
    """
    Faz requisição para endpoint público de busca do Mercado Livre (sem OAuth).
    
    Args:
        termo: Termo de busca.
        limite: Limite de resultados.
    
    Returns:
        JSON da resposta.
    
    Raises:
        ErroRedeMercadoLivre: Falha de conexão.
        ErroRespostaMercadoLivre: Erro HTTP ou resposta inválida.
    """
    url = "https://api.mercadolibre.com/sites/MLB/search"
    params: dict[str, str | int] = {"q": termo, "limit": limite}
    headers = BASE_HEADERS.copy()
    
    try:
        resposta = requests.get(url, params=params, headers=headers, timeout=15)
        resposta.raise_for_status()
    except requests.exceptions.Timeout as exc:
        raise ErroRedeMercadoLivre("Timeout ao consultar API pública do Mercado Livre") from exc
    except requests.exceptions.ConnectionError as exc:
        raise ErroRedeMercadoLivre("Erro de conexão com a API do Mercado Livre") from exc
    except requests.exceptions.HTTPError as exc:
        corpo = exc.response.text if exc.response is not None else "sem resposta"
        logger.warning(
            "Erro HTTP na busca pública: status=%d, url=%s, resposta=%s",
            exc.response.status_code if exc.response is not None else -1,
            exc.response.url if exc.response is not None else "desconhecida",
            corpo,
        )
        raise ErroRespostaMercadoLivre(
            f"Erro HTTP na API pública: {exc.response.status_code} - {corpo}"
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise ErroRedeMercadoLivre(f"Erro na requisição pública: {exc}") from exc
    
    try:
        dados: dict[str, Any] = resposta.json()
    except ValueError as exc:
        raise ErroRespostaMercadoLivre("Resposta da API pública não é JSON válido") from exc
    
    return dados


@retry_com_backoff(max_tentativas=2, backoff_base=1.5)
def _requisicao_html_busca(termo: str) -> str:
    """
    Faz scraping leve da página de busca do Mercado Livre (HTML).
    
    Args:
        termo: Termo de busca.
    
    Returns:
        HTML da página de resultados.
    
    Raises:
        ErroRedeMercadoLivre: Falha de conexão.
        ErroRespostaMercadoLivre: Erro HTTP.
    """
    termo_encoded = termo.replace(" ", "+")
    url = f"https://lista.mercadolivre.com.br/{termo_encoded}"
    headers = BASE_HEADERS.copy()
    headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    
    try:
        resposta = requests.get(url, headers=headers, timeout=20)
        resposta.raise_for_status()
    except requests.exceptions.Timeout as exc:
        raise ErroRedeMercadoLivre("Timeout ao acessar página de busca do Mercado Livre") from exc
    except requests.exceptions.ConnectionError as exc:
        raise ErroRedeMercadoLivre("Erro de conexão com Mercado Livre") from exc
    except requests.exceptions.HTTPError as exc:
        corpo = exc.response.text if exc.response is not None else "sem resposta"
        raise ErroRespostaMercadoLivre(
            f"Erro HTTP na página de busca: {exc.response.status_code} - {corpo}"
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise ErroRedeMercadoLivre(f"Erro na requisição HTML: {exc}") from exc
    
    return resposta.text


def _parsear_html_resultados(html: str) -> list[dict]:
    """
    Extrai produtos do HTML da página de busca do Mercado Livre.
    
    Args:
        html: HTML da página de resultados.
    
    Returns:
        Lista de produtos extraídos.
    """
    soup = BeautifulSoup(html, "html.parser")
    produtos = []
    
    # Seletores para os cards de produto (atualizados para layout atual do ML)
    cards = soup.select("li.ui-search-layout__item, li.ui-search-result__wrapper, div.ui-search-result")
    
    for card in cards:
        try:
            # Link do produto
            link_elem = card.select_one("a.ui-search-link, a.ui-search-result__link, a[href*='/MLB-']")
            if not link_elem or not link_elem.get("href"):
                continue
            link = link_elem["href"]
            if "mercadolivre.com.br" not in link:
                continue
            
            # ID do produto (MLB-xxxxx)
            import re
            id_match = re.search(r"(MLB\d+)", link)
            produto_id = id_match.group(1) if id_match else None
            
            # Título
            title_elem = card.select_one("h2.ui-search-item__title, h2.poly-component__title, h2.ui-search-item__group__element")
            if not title_elem:
                title_elem = card.select_one("a.ui-search-link h2, a.ui-search-result__link h2")
            titulo = title_elem.get_text(strip=True) if title_elem else None
            
            # Preço
            price_elem = card.select_one("span.andes-money-amount__fraction, span.price-tag-fraction, div.price-tag span")
            preco = None
            if price_elem:
                preco_text = price_elem.get_text(strip=True).replace(".", "").replace(",", ".")
                try:
                    preco = float(preco_text)
                except ValueError:
                    pass
            
            if produto_id and titulo and preco is not None:
                produtos.append({
                    "id": produto_id,
                    "titulo": titulo,
                    "preco": preco,
                    "link": link,
                })
        except Exception as e:
            logger.debug("Erro ao parsear card: %s", e)
            continue
    
    return produtos


_TOKEN_CACHE: dict[str, Any] = {}


@retry_com_backoff(max_tentativas=3, backoff_base=1.0)
def _requisicao_token() -> dict[str, Any]:
    """
    Faz requisição para obter token OAuth com retry.

    Returns:
        Dicionário com access_token e expires_in.

    Raises:
        ErroCredenciaisMercadoLivre: Se credenciais não configuradas.
        ErroRedeMercadoLivre: Falha de conexão.
        ErroRespostaMercadoLivre: Erro HTTP ou resposta inválida.
    """
    client_id = os.getenv("ML_CLIENT_ID", "").strip()
    client_secret = os.getenv("ML_CLIENT_SECRET", "").strip()

    if not client_id or not client_secret:
        raise ErroCredenciaisMercadoLivre(
            "ML_CLIENT_ID e ML_CLIENT_SECRET devem estar definidos no ambiente"
        )

    url = "https://api.mercadolibre.com/oauth/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    try:
        resposta = requests.post(url, data=data, headers=headers, timeout=10)
        resposta.raise_for_status()
    except requests.exceptions.Timeout as exc:
        raise ErroRedeMercadoLivre("Timeout ao obter token OAuth") from exc
    except requests.exceptions.ConnectionError as exc:
        raise ErroRedeMercadoLivre("Erro de conexão ao obter token OAuth") from exc
    except requests.exceptions.HTTPError as exc:
        corpo = exc.response.text if exc.response is not None else "sem resposta"
        logger.error(
            "Erro HTTP ao obter token OAuth: status=%d, resposta=%s",
            exc.response.status_code if exc.response is not None else -1,
            corpo,
        )
        raise ErroRespostaMercadoLivre(
            f"Erro HTTP ao obter token: {exc.response.status_code} - {corpo}"
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise ErroRedeMercadoLivre(f"Erro na requisição de token: {exc}") from exc

    try:
        dados: dict[str, Any] = resposta.json()
    except ValueError as exc:
        logger.error("Resposta de token não é JSON válido: %s", resposta.text)
        raise ErroRespostaMercadoLivre("Resposta de token não é JSON válido") from exc

    access_token: str | None = dados.get("access_token")
    expires_in: int = dados.get("expires_in", 0)

    if not access_token:
        raise ErroRespostaMercadoLivre("Token de acesso não retornado pela API")

    return {"access_token": access_token, "expires_in": expires_in}


def _obter_token() -> str:
    """
    Obtém access token via OAuth client_credentials com cache em memória.

    Returns:
        Access token válido.

    Raises:
        ErroCredenciaisMercadoLivre: Se ML_CLIENT_ID ou ML_CLIENT_SECRET não estiverem definidos.
        ErroRedeMercadoLivre: Em caso de falha de conexão.
        ErroRespostaMercadoLivre: Se a API retornar erro.
    """
    agora = time.time()

    if _TOKEN_CACHE.get("access_token") and _TOKEN_CACHE.get("expires_at", 0) > agora + 60:
        logger.debug("Usando token OAuth em cache")
        return str(_TOKEN_CACHE["access_token"])

    logger.info("Obtendo novo token OAuth do Mercado Livre")
    dados = _requisicao_token()

    _TOKEN_CACHE["access_token"] = dados["access_token"]
    _TOKEN_CACHE["expires_at"] = agora + dados["expires_in"]

    return str(_TOKEN_CACHE["access_token"])


@retry_com_backoff(max_tentativas=3, backoff_base=1.0)
def _requisicao_busca(token: str, termo: str, limite: int) -> dict[str, Any]:
    """
    Faz requisição de busca com retry.

    Args:
        token: Access token válido.
        termo: Termo de busca.
        limite: Limite de resultados.

    Returns:
        JSON da resposta.

    Raises:
        ErroRedeMercadoLivre: Falha de conexão.
        ErroRespostaMercadoLivre: Erro HTTP ou resposta inválida.
    """
    url = "https://api.mercadolibre.com/sites/MLB/search"
    params: dict[str, str | int] = {"q": termo, "limit": limite}
    headers = {"Authorization": f"Bearer {token}"}

    try:
        resposta = requests.get(url, params=params, headers=headers, timeout=10)
        resposta.raise_for_status()
    except requests.exceptions.Timeout as exc:
        raise ErroRedeMercadoLivre("Timeout ao consultar API do Mercado Livre") from exc
    except requests.exceptions.ConnectionError as exc:
        raise ErroRedeMercadoLivre("Erro de conexão com a API do Mercado Livre") from exc
    except requests.exceptions.HTTPError as exc:
        if exc.response.status_code == 401:
            _TOKEN_CACHE.clear()
            raise ErroRespostaMercadoLivre("Token expirado ou inválido, cache limpo") from exc
        corpo = exc.response.text if exc.response is not None else "sem resposta"
        logger.error(
            "Erro HTTP na busca: status=%d, url=%s, resposta=%s",
            exc.response.status_code if exc.response is not None else -1,
            exc.response.url if exc.response is not None else "desconhecida",
            corpo,
        )
        raise ErroRespostaMercadoLivre(
            f"Erro HTTP da API: {exc.response.status_code} - {corpo}"
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise ErroRedeMercadoLivre(f"Erro na requisição: {exc}") from exc

    try:
        dados: dict[str, Any] = resposta.json()
    except ValueError as exc:
        raise ErroRespostaMercadoLivre("Resposta não é JSON válido") from exc

    return dados


def buscar_produtos(termo: str, limite: int = 20) -> list[dict]:
    """
    Busca produtos no Mercado Livre com fallback em cascata:
    1. API pública (sem OAuth)
    2. HTML scraping da página de busca
    3. API oficial com OAuth (se credenciais disponíveis)
    
    Args:
        termo: Termo de busca.
        limite: Número máximo de resultados (padrão: 20).
    
    Returns:
        Lista de dicionários com id, titulo, preco e link.
    
    Raises:
        ErroRedeMercadoLivre: Em caso de falha de conexão ou timeout.
        ErroRespostaMercadoLivre: Se todas as tentativas falharem.
    """
    logger.info("Buscando produtos no Mercado Livre: termo='%s', limite=%d", termo, limite)
    
    # 1. Tentar API pública (sem OAuth)
    try:
        logger.debug("Tentando API pública do Mercado Livre")
        dados = _requisicao_busca_publica(termo, limite)
        resultados = dados.get("results", [])
        if isinstance(resultados, list) and resultados:
            produtos = []
            for item in resultados:
                produto = {
                    "id": item.get("id"),
                    "titulo": item.get("title"),
                    "preco": item.get("price"),
                    "link": item.get("permalink"),
                }
                if all(produto.values()):
                    produtos.append(produto)
            logger.info("API pública: %d produtos válidos", len(produtos))
            if produtos:
                return produtos
    except (ErroRedeMercadoLivre, ErroRespostaMercadoLivre) as e:
        logger.warning("API pública falhou: %s", e)
    
    # 2. HTML scraping fallback
    try:
        logger.debug("Tentando HTML scraping da página de busca")
        html = _requisicao_html_busca(termo)
        produtos = _parsear_html_resultados(html)
        if produtos:
            logger.info("HTML scraping: %d produtos válidos", len(produtos))
            return produtos[:limite]
        logger.warning("HTML scraping não encontrou produtos válidos")
    except (ErroRedeMercadoLivre, ErroRespostaMercadoLivre) as e:
        logger.warning("HTML scraping falhou: %s", e)
    
    # 3. Fallback para API oficial com OAuth (se credenciais disponíveis)
    try:
        logger.debug("Tentando API oficial com OAuth")
        token = _obter_token()
        dados = _requisicao_busca(token, termo, limite)
        resultados = dados.get("results", [])
        if isinstance(resultados, list) and resultados:
            produtos = []
            for item in resultados:
                produto = {
                    "id": item.get("id"),
                    "titulo": item.get("title"),
                    "preco": item.get("price"),
                    "link": item.get("permalink"),
                }
                if all(produto.values()):
                    produtos.append(produto)
            logger.info("API OAuth: %d produtos válidos", len(produtos))
            return produtos
    except ErroCredenciaisMercadoLivre:
        logger.info("Credenciais OAuth não configuradas, pulando API oficial")
    except (ErroRedeMercadoLivre, ErroRespostaMercadoLivre) as e:
        logger.warning("API OAuth falhou: %s", e)
    
    logger.warning("Todas as tentativas de busca falharam para termo: %s", termo)
    return []
