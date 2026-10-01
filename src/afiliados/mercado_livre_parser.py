"""Parser para extrair dados de produtos do Mercado Livre via HTML."""

import json
import logging
import os
import re
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from afiliados.http_utils import retry_com_backoff

load_dotenv()

logger = logging.getLogger(__name__)


class MercadoLivreParserError(Exception):
    """Exceção base para erros do parser."""

    pass


class ErroRedeParser(MercadoLivreParserError):
    """Erro de rede ao buscar a página do produto."""

    pass


class ErroParseHTML(MercadoLivreParserError):
    """Erro ao fazer parse do HTML ou extrair dados."""

    pass


class ErroURLInvalida(MercadoLivreParserError):
    """URL não é uma URL válida de produto do Mercado Livre."""

    pass


ML_PRODUCT_URL_PATTERN = re.compile(
    r"https?://(?:[^/]+\.)?mercadolivre\.com\.br/(?:[^/]+/)?MLB-?(\d+)"
)

ML_CATALOG_URL_PATTERN = re.compile(
    r"https?://(?:[^/]+\.)?mercadolivre\.com\.br/(?:[^/]+/)?p/MLB-?(\d+)"
)

ML_SHORT_URL_PATTERN = re.compile(
    r"https?://(?:[^/]+\.)?(?:ml\.com\.br|mercadolivre\.com\.br/sec)/"
)

ML_ALLOWED_DOMAINS = (
    "mercadolivre.com.br",
    "www.mercadolivre.com.br",
    "ml.com.br",
    "www.ml.com.br",
)


# Cache de token OAuth em memória
_TOKEN_CACHE: dict[str, Any] = {}


@retry_com_backoff(max_tentativas=3, backoff_base=1.0)
def _obter_token_oauth() -> str:
    """
    Obtém access token OAuth via client_credentials.

    Returns:
        Access token válido.

    Raises:
        ErroRedeParser: Em caso de falha na obtenção do token.
    """
    global _TOKEN_CACHE

    import time

    agora = time.time()

    if _TOKEN_CACHE.get("access_token") and _TOKEN_CACHE.get("expires_at", 0) > agora + 60:
        return str(_TOKEN_CACHE["access_token"])

    client_id = os.getenv("ML_CLIENT_ID", "").strip()
    client_secret = os.getenv("ML_CLIENT_SECRET", "").strip()

    if not client_id or not client_secret:
        raise ErroRedeParser("Credenciais ML_CLIENT_ID/ML_CLIENT_SECRET não configuradas")

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
    except requests.exceptions.RequestException as exc:
        raise ErroRedeParser(f"Erro ao obter token OAuth: {exc}") from exc

    try:
        data = resposta.json()
    except ValueError as exc:
        raise ErroRedeParser("Resposta de token não é JSON válido") from exc

    access_token = data.get("access_token")
    expires_in = data.get("expires_in", 0)

    if not access_token:
        raise ErroRedeParser("Token de acesso não retornado pela API")

    _TOKEN_CACHE["access_token"] = access_token
    _TOKEN_CACHE["expires_at"] = agora + expires_in

    return str(access_token)


def _headers_api() -> dict[str, str]:
    """Retorna headers para chamadas autenticadas à API do ML."""
    token = _obter_token_oauth()
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }


@retry_com_backoff(max_tentativas=3, backoff_base=1.0)
def _buscar_catalogo_produto(produto_id: str) -> dict[str, Any] | None:
    """
    Busca dados do produto de catálogo na API autenticada.

    Args:
        produto_id: ID do produto (ex: MLB70273909).

    Returns:
        Dicionário com dados do catálogo (title, pictures, attributes, etc.) ou None.
    """
    id_limpo = produto_id.replace("MLB", "")
    endpoint = f"https://api.mercadolibre.com/products/MLB{id_limpo}"

    try:
        logger.debug("Consultando catálogo: %s", endpoint)
        resposta = requests.get(endpoint, headers=_headers_api(), timeout=10)
        if resposta.status_code == 404:
            logger.debug("Produto não encontrado no catálogo: %s", endpoint)
            return None
        resposta.raise_for_status()
    except requests.exceptions.RequestException as exc:
        logger.warning("Erro ao consultar catálogo: %s", exc)
        return None

    try:
        data = resposta.json()
    except ValueError:
        logger.warning("Resposta do catálogo não é JSON válido")
        return None

    return _extrair_dados_catalogo(data)


def _extrair_dados_catalogo(data: dict[str, Any]) -> dict[str, Any]:
    """Extrai campos relevantes da resposta do catálogo de produtos."""
    resultado = {}

    # Título
    if "name" in data:
        resultado["titulo"] = data["name"]

    # Imagens
    if "pictures" in data and data["pictures"]:
        for pic in data["pictures"]:
            if isinstance(pic, dict) and pic.get("url"):
                resultado["imagem"] = _normalizar_url_imagem(pic["url"])
                break
            elif isinstance(pic, str):
                resultado["imagem"] = _normalizar_url_imagem(pic)
                break

    # Marca
    if "attributes" in data:
        for attr in data["attributes"]:
            if attr.get("id") == "BRAND":
                resultado["marca"] = attr.get("value_name")
                break

    # Descrição
    if "short_description" in data and isinstance(data["short_description"], dict):
        resultado["descricao"] = data["short_description"].get("content")

    # Atributos principais
    if "attributes" in data:
        attrs = {}
        for attr in data["attributes"]:
            attrs[attr.get("id", "")] = attr.get("value_name")
        if attrs:
            resultado["atributos"] = attrs

    return resultado


@retry_com_backoff(max_tentativas=3, backoff_base=1.0)
def _buscar_itens_catalogo(produto_id: str) -> list[dict[str, Any]]:
    """
    Busca itens (anúncios) vinculados a um produto de catálogo.

    Args:
        produto_id: ID do produto de catálogo (ex: MLB70273909).

    Returns:
        Lista de itens com price, item_id, seller_id, etc.
    """
    id_limpo = produto_id.replace("MLB", "")
    endpoint = f"https://api.mercadolibre.com/products/MLB{id_limpo}/items"

    try:
        logger.debug("Consultando itens do catálogo: %s", endpoint)
        resposta = requests.get(endpoint, headers=_headers_api(), timeout=10)
        if resposta.status_code == 404:
            logger.debug("Itens não encontrados para catálogo: %s", endpoint)
            return []
        resposta.raise_for_status()
    except requests.exceptions.RequestException as exc:
        logger.warning("Erro ao consultar itens do catálogo: %s", exc)
        return []

    try:
        data: dict[str, Any] = resposta.json()
    except ValueError:
        logger.warning("Resposta de itens do catálogo não é JSON válido")
        return []

    results = data.get("results", [])
    if isinstance(results, list):
        return results
    return []


def _extrair_preco_itens_catalogo(itens: list[dict[str, Any]]) -> float | None:
    """Extrai o menor preço válido dos itens do catálogo."""
    precos = []
    for item in itens:
        price = item.get("price")
        if price is not None:
            try:
                precos.append(float(price))
            except (ValueError, TypeError):
                pass

    if not precos:
        return None

    # Retorna o menor preço (melhor oferta)
    return min(precos)


@retry_com_backoff(max_tentativas=3, backoff_base=1.0)
def _buscar_item_anuncio(produto_id: str) -> dict[str, Any] | None:
    """
    Busca dados de um anúncio (item) na API pública.

    Args:
        produto_id: ID do item (ex: MLB123456).

    Returns:
        Dicionário com title, price, thumbnail, etc. ou None.
    """
    id_limpo = produto_id.replace("MLB", "")
    endpoint = f"https://api.mercadolibre.com/items/MLB{id_limpo}"

    try:
        logger.debug("Consultando item: %s", endpoint)
        resposta = requests.get(endpoint, headers={"Accept": "application/json"}, timeout=10)
        if resposta.status_code == 404:
            return None
        resposta.raise_for_status()
    except requests.exceptions.RequestException as exc:
        logger.warning("Erro ao consultar item: %s", exc)
        return None

    try:
        data = resposta.json()
    except ValueError:
        return None

    return _extrair_dados_item(data)


def _extrair_dados_item(data: dict[str, Any]) -> dict[str, Any]:
    """Extrai campos relevantes da resposta de item/anúncio."""
    resultado = {}

    if "title" in data:
        resultado["titulo"] = data["title"]

    if "price" in data and data["price"] is not None:
        resultado["preco"] = _normalizar_preco(data["price"])

    if "thumbnail" in data:
        resultado["imagem"] = _normalizar_url_imagem(data["thumbnail"])
    elif "pictures" in data and data["pictures"]:
        for pic in data["pictures"]:
            if isinstance(pic, dict) and pic.get("url"):
                resultado["imagem"] = _normalizar_url_imagem(pic["url"])
                break
            elif isinstance(pic, str):
                resultado["imagem"] = _normalizar_url_imagem(pic)
                break

    if "seller_id" in data:
        resultado["seller_id"] = data["seller_id"]

    return resultado


def _extrair_id_da_url(url: str) -> str | None:
    """Extrai o ID do produto (MLBxxxxxx) da URL."""
    match = ML_PRODUCT_URL_PATTERN.search(url)
    if match:
        return f"MLB{match.group(1)}"

    match = ML_CATALOG_URL_PATTERN.search(url)
    if match:
        return f"MLB{match.group(1)}"

    return None


def _validar_url_mercado_livre(url: str) -> None:
    """Valida se a URL é de um produto do Mercado Livre Brasil."""
    parsed = urlparse(url)
    if not parsed.netloc.endswith(ML_ALLOWED_DOMAINS):
        raise ErroURLInvalida(f"Domínio não suportado: {parsed.netloc}")

    if not ML_PRODUCT_URL_PATTERN.search(url) and not ML_CATALOG_URL_PATTERN.search(url):
        if not ML_SHORT_URL_PATTERN.search(url):
            raise ErroURLInvalida(f"URL não parece ser de produto MLB: {url}")


@retry_com_backoff(max_tentativas=3, backoff_base=1.0)
def _buscar_html(url: str) -> tuple[str, str]:
    """
    Faz requisição HTTP para obter o HTML da página do produto, seguindo redirects.

    Args:
        url: URL do produto no Mercado Livre.

    Returns:
        Tupla (HTML da página, URL final após redirects).

    Raises:
        ErroRedeParser: Em caso de falha de conexão, timeout ou erro HTTP.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    }

    try:
        resposta = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        resposta.raise_for_status()
    except requests.exceptions.Timeout as exc:
        raise ErroRedeParser(f"Timeout ao buscar página: {url}") from exc
    except requests.exceptions.ConnectionError as exc:
        raise ErroRedeParser(f"Erro de conexão ao buscar página: {url}") from exc
    except requests.exceptions.HTTPError as exc:
        raise ErroRedeParser(
            f"Erro HTTP {exc.response.status_code} ao buscar página: {url}"
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise ErroRedeParser(f"Erro na requisição: {exc}") from exc

    return resposta.text, str(resposta.url)


def _extrair_json_ld(soup: BeautifulSoup) -> dict[str, Any] | None:
    """
    Extrai dados do schema.org/Product do JSON-LD.

    Args:
        soup: BeautifulSoup com o HTML parseado.

    Returns:
        Dicionário com dados do produto ou None se não encontrado.
    """
    scripts = soup.find_all("script", type="application/ld+json")
    for script in scripts:
        if not script.string:
            continue
        try:
            data = json.loads(script.string)
        except json.JSONDecodeError:
            continue

        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and item.get("@type") in (
                "Product",
                "ProductGroup",
                "ItemPage",
            ):
                return item
            # BreadcrumbList pode conter o nome do produto no último item
            if isinstance(item, dict) and item.get("@type") == "BreadcrumbList":
                items_list = item.get("itemListElement", [])
                if items_list:
                    last_item = items_list[-1]
                    if isinstance(last_item, dict) and "item" in last_item:
                        item_ref = last_item["item"]
                        if isinstance(item_ref, dict) and "name" in item_ref:
                            return {"@type": "Product", "name": item_ref["name"]}
    return None


def _extrair_opengraph(soup: BeautifulSoup) -> dict[str, Any]:
    """
    Extrai dados das meta tags OpenGraph e Twitter Card como fallback.

    Args:
        soup: BeautifulSoup com o HTML parseado.

    Returns:
        Dicionário com dados extraídos (podem estar incompletos).
    """
    dados: dict[str, Any] = {}

    # OpenGraph tags
    meta_tags = soup.find_all("meta", property=re.compile(r"^og:"))
    for meta in meta_tags:
        prop = meta.get("property", "")
        content = meta.get("content", "")
        content_str = str(content) if content is not None else ""
        if prop == "og:title":
            dados["title"] = content_str
        elif prop == "og:price:amount":
            try:
                dados["price"] = float(content_str)
            except ValueError:
                pass
        elif prop == "og:image":
            dados["image"] = _normalizar_url_imagem(content_str)
        elif prop == "og:url":
            dados["url"] = content_str

    # Twitter Card tags
    twitter_tags = soup.find_all("meta", attrs={"name": re.compile(r"^twitter:")})
    for meta in twitter_tags:
        name = meta.get("name", "")
        content = meta.get("content", "")
        content_str = str(content) if content is not None else ""
        if name == "twitter:title" and "title" not in dados:
            dados["title"] = content_str
        elif name == "twitter:image" and "image" not in dados:
            dados["image"] = _normalizar_url_imagem(content_str)

    # Product-specific meta tags (ML usa estes)
    product_price = soup.find("meta", property="product:price:amount")
    if product_price and "price" not in dados:
        content = product_price.get("content", "")
        try:
            dados["price"] = float(str(content))
        except ValueError:
            pass

    return dados


def _normalizar_url_imagem(url: str | None) -> str | None:
    """Normaliza URL de imagem para HTTPS absoluto."""
    if not url:
        return None
    url = str(url).strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://"):
        return "https" + url[4:]
    if url.startswith("https://"):
        return url
    return None


def _normalizar_preco(valor: Any) -> float | None:
    """Normaliza valor de preço para float."""
    if valor is None:
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    if isinstance(valor, str):
        # Remove caracteres não numéricos exceto ponto e vírgula
        limpo = re.sub(r"[^\d.,]", "", valor)
        # Se tem vírgula, assume que é separador decimal brasileiro
        if "," in limpo and "." not in limpo:
            limpo = limpo.replace(",", ".")
        elif "," in limpo and "." in limpo:
            # Se tem ambos, o último é o separador decimal
            if limpo.rfind(",") > limpo.rfind("."):
                limpo = limpo.replace(".", "").replace(",", ".")
            else:
                limpo = limpo.replace(",", "")
        else:
            # Só tem pontos - pode ser separador de milhares
            # Se mais de um ponto, remove todos menos o último
            if limpo.count(".") > 1:
                parts = limpo.split(".")
                limpo = "".join(parts[:-1]) + "." + parts[-1]
        try:
            return float(limpo)
        except ValueError:
            return None
    return None


def _extrair_preco_com_centavos(
    soup: BeautifulSoup, seletor_fraction: str, seletor_cents: str
) -> float | None:
    """Extrai preço combinando fraction e cents de elementos separados."""
    elem_fraction = soup.select_one(seletor_fraction)
    if not elem_fraction or not elem_fraction.get_text(strip=True):
        return None

    fraction_text = elem_fraction.get_text(strip=True)
    preco_base = _normalizar_preco(fraction_text)
    if preco_base is None:
        return None

    # Tenta extrair centavos
    elem_cents = soup.select_one(seletor_cents)
    if elem_cents and elem_cents.get_text(strip=True):
        cents_text = elem_cents.get_text(strip=True)
        cents = _normalizar_preco(cents_text)
        if cents is not None and cents < 100:
            # Combina reais + centavos
            return preco_base + (cents / 100)

    return preco_base


def _extrair_dados_json_ld(produto_json: dict[str, Any]) -> dict[str, Any]:
    """Extrai campos relevantes do JSON-LD do schema.org/Product."""
    dados = {}

    # Título
    if "name" in produto_json:
        dados["titulo"] = produto_json["name"]

    # Preço
    offers = produto_json.get("offers")
    if offers:
        if isinstance(offers, list) and offers:
            offers = offers[0]
        if isinstance(offers, dict):
            price = offers.get("price") or offers.get("lowPrice") or offers.get("highPrice")
            dados["preco"] = _normalizar_preco(price)
            if "priceCurrency" in offers:
                dados["moeda"] = offers["priceCurrency"]

    # Imagem
    image = produto_json.get("image")
    if image:
        if isinstance(image, list) and image:
            dados["imagem"] = _normalizar_url_imagem(image[0])
        elif isinstance(image, str):
            dados["imagem"] = _normalizar_url_imagem(image)

    # Descrição
    if "description" in produto_json:
        dados["descricao"] = produto_json["description"]

    # Marca
    brand = produto_json.get("brand")
    if brand:
        if isinstance(brand, dict):
            dados["marca"] = brand.get("name")
        elif isinstance(brand, str):
            dados["marca"] = brand

    # SKU/MPN
    if "sku" in produto_json:
        dados["sku"] = produto_json["sku"]
    if "mpn" in produto_json:
        dados["mpn"] = produto_json["mpn"]

    return dados


def _extrair_titulo_html(soup: BeautifulSoup) -> str | None:
    """Extrai título de elementos HTML da página do produto."""
    # Seletores de título do ML (prioridade: mais específico primeiro)
    seletores = [
        "h1.ui-pdp-title",
        "h1.poly-component__title",
        "h1[itemprop='name']",
        "h1.ui-pdp-title__title",
        "h1[class*='title']",
    ]
    for seletor in seletores:
        elem = soup.select_one(seletor)
        if elem and elem.get_text(strip=True):
            return elem.get_text(strip=True)

    # Fallback: tag <title> limpando sufixos do ML
    title_tag = soup.find("title")
    if title_tag and title_tag.string:
        titulo = title_tag.string.strip()
        # Remove sufixos comuns do Mercado Livre
        sufixos = [
            " | MercadoLivre",
            " | Mercado Livre",
            " no Mercado Livre",
            " no MercadoLivre",
            " - MercadoLivre",
            " - Mercado Livre",
        ]
        for sufixo in sufixos:
            if titulo.endswith(sufixo):
                titulo = titulo[: -len(sufixo)].strip()
                break
        if titulo:
            return titulo

    return None


def _extrair_preco_html(soup: BeautifulSoup) -> float | None:
    """Extrai preço de elementos HTML da página do produto."""

    # 1. Tenta combinar fraction + cents (padrão do ML com centavos separados)
    preco = _extrair_preco_com_centavos(
        soup, ".andes-money-amount__fraction", ".andes-money-amount__cents"
    )
    if preco and preco > 0:
        return preco

    preco = _extrair_preco_com_centavos(soup, ".price-tag-fraction", ".price-tag-cents")
    if preco and preco > 0:
        return preco

    preco = _extrair_preco_com_centavos(
        soup,
        ".ui-pdp-price__part .andes-money-amount__fraction",
        ".ui-pdp-price__part .andes-money-amount__cents",
    )
    if preco and preco > 0:
        return preco

    preco = _extrair_preco_com_centavos(
        soup,
        ".ui-pdp-price__second-line .andes-money-amount__fraction",
        ".ui-pdp-price__second-line .andes-money-amount__cents",
    )
    if preco and preco > 0:
        return preco

    # 2. Seletores de preço do ML (apenas fraction, sem cents separado)
    seletores_fraction = [
        ".andes-money-amount__fraction",
        ".price-tag-fraction",
        ".ui-pdp-price__part .andes-money-amount__fraction",
        "[data-testid='price'] .andes-money-amount__fraction",
        ".poly-price__fraction",
        ".ui-pdp-price__second-line .andes-money-amount__fraction",
    ]

    for seletor in seletores_fraction:
        elem = soup.select_one(seletor)
        if elem and elem.get_text(strip=True):
            texto = elem.get_text(strip=True)
            preco = _normalizar_preco(texto)
            if preco and preco > 0:
                return preco

    # 3. Meta tags com itemprop=price
    meta_price = soup.find("meta", itemprop="price")
    if meta_price:
        content = meta_price.get("content", "")
        preco = _normalizar_preco(content)
        if preco and preco > 0:
            return preco

    # 4. Span com itemprop=price
    span_price = soup.find("span", itemprop="price")
    if span_price and span_price.get_text(strip=True):
        preco = _normalizar_preco(span_price.get_text(strip=True))
        if preco and preco > 0:
            return preco

    # 5. Elementos com data-price
    data_price = soup.find(attrs={"data-price": True})  # type: ignore[call-overload]
    if data_price:
        preco = _normalizar_preco(data_price.get("data-price", ""))
        if preco and preco > 0:
            return preco

    # 6. Meta tag product:price:amount
    meta_price = soup.find("meta", property="product:price:amount")
    if meta_price:
        content = meta_price.get("content", "")
        preco = _normalizar_preco(content)
        if preco and preco > 0:
            return preco

    return None


def _extrair_imagem_html(soup: BeautifulSoup) -> str | None:
    """Extrai imagem de elementos HTML da página do produto."""
    # Seletores de imagem do ML (prioridade: mais específico primeiro)
    seletores = [
        "img.ui-pdp-image",
        "img.poly-component__picture",
        "img.ui-pdp-gallery__figure img",
        "img[class*='gallery']",
        "img[itemprop='image']",
        "figure.ui-pdp-gallery__figure img",
    ]
    for seletor in seletores:
        elem = soup.select_one(seletor)
        if elem:
            # Tenta src, data-src, data-zoom-src
            for attr in ["src", "data-src", "data-zoom-src", "data-srcset"]:
                src = elem.get(attr)
                if src and isinstance(src, str) and src.startswith("http"):
                    # Se for srcset, pega a primeira URL
                    if attr == "data-srcset":
                        src = src.split(",")[0].split()[0]
                    return _normalizar_url_imagem(src)

    return None


def _extrair_dados_opengraph(dados_og: dict[str, Any]) -> dict[str, Any]:
    """Normaliza dados extraídos do OpenGraph e Twitter Card."""
    resultado = {}
    if "title" in dados_og:
        resultado["titulo"] = dados_og["title"]
    if "price" in dados_og:
        resultado["preco"] = dados_og["price"]
    if "image" in dados_og:
        resultado["imagem"] = _normalizar_url_imagem(dados_og["image"])
    if "url" in dados_og:
        resultado["link"] = dados_og["url"]
    return resultado


def extrair_produto_da_url(url: str) -> dict[str, Any]:
    """
    Extrai dados do produto a partir da URL da página do Mercado Livre.

    Estratégia (ordem de prioridade):
    1. API autenticada do Mercado Livre:
       - Catálogo (/p/MLB...): /products/{id} + /products/{id}/items para preço
       - Anúncio (/MLB...): /items/{id}
    2. JSON-LD (schema.org/Product, ItemPage, BreadcrumbList)
    3. Meta tags OpenGraph + Twitter Card + product:price:amount
    4. Elementos HTML específicos do ML (título, preço, imagem)
    5. Tag <title> como último recurso

    Args:
        url: URL do produto no Mercado Livre (ex: https://produto.mercadolivre.com.br/MLB-123456)

    Returns:
        Dicionário com: id, titulo, preco, imagem, link, marca, descricao (opcionais)

    Raises:
        ErroURLInvalida: Se URL não for de produto MLB válido.
        ErroRedeParser: Erro de rede ao buscar a página.
        ErroParseHTML: Se não conseguir extrair dados mínimos (título e preço).
    """
    _validar_url_mercado_livre(url)

    # Tenta extrair ID da URL inicial (pode falhar para URLs curtas/redirects)
    produto_id = _extrair_id_da_url(url)

    logger.info("Extraindo dados do produto: %s", url)

    html, final_url = _buscar_html(url)
    soup = BeautifulSoup(html, "lxml")

    # Se houve redirect ou URL inicial não tinha ID, tenta extrair da URL final
    if final_url != url or not produto_id:
        logger.debug("Redirect detectado ou URL sem ID: %s -> %s", url, final_url)
        produto_id_final = _extrair_id_da_url(final_url)
        if produto_id_final:
            produto_id = produto_id_final

    if not produto_id:
        raise ErroParseHTML(f"Não foi possível extrair ID do produto da URL final: {final_url}")

    # Detecta se é URL de catálogo (/p/MLB...)
    is_catalogo = bool(
        ML_CATALOG_URL_PATTERN.search(url) or ML_CATALOG_URL_PATTERN.search(final_url)
    )

    # Tentativa 1: API autenticada do Mercado Livre
    dados = {}
    logger.debug("Tentando API autenticada para %s (catálogo=%s)", produto_id, is_catalogo)

    if is_catalogo:
        # Para catálogo: busca dados do produto + itens para preço
        dados_catalogo = _buscar_catalogo_produto(produto_id)
        if dados_catalogo:
            logger.info("Dados de catálogo obtidos para %s", produto_id)
            dados = dados_catalogo

            # Busca itens vinculados para obter preço
            itens = _buscar_itens_catalogo(produto_id)
            if itens:
                preco_min = _extrair_preco_itens_catalogo(itens)
                if preco_min:
                    dados["preco"] = preco_min
                    logger.info("Preço mínimo do catálogo %s: R$ %.2f", produto_id, preco_min)
    else:
        # Para anúncio direto: busca item
        dados_item = _buscar_item_anuncio(produto_id)
        if dados_item:
            logger.info("Dados de item obtidos para %s", produto_id)
            dados = dados_item

    # Tentativa 2: JSON-LD schema.org/Product, ItemPage, BreadcrumbList
    if not dados.get("titulo") or not dados.get("preco") or not dados.get("imagem"):
        json_ld = _extrair_json_ld(soup)
        if json_ld:
            logger.debug("JSON-LD encontrado para %s", produto_id)
            dados_json_ld = _extrair_dados_json_ld(json_ld)
            for k, v in dados_json_ld.items():
                if k not in dados or not dados[k]:
                    dados[k] = v

    # Tentativa 3: Fallback OpenGraph + Twitter Card
    if not dados.get("titulo") or not dados.get("preco") or not dados.get("imagem"):
        logger.debug("Tentando fallback OpenGraph/Twitter para %s", produto_id)
        dados_og = _extrair_opengraph(soup)
        dados_og_norm = _extrair_dados_opengraph(dados_og)
        for k, v in dados_og_norm.items():
            if k not in dados or not dados[k]:
                dados[k] = v

    # Tentativa 4: Extração direta do HTML (elementos específicos do ML)
    if not dados.get("titulo"):
        logger.debug("Tentando extrair título do HTML para %s", produto_id)
        titulo_html = _extrair_titulo_html(soup)
        if titulo_html:
            dados["titulo"] = titulo_html

    if not dados.get("preco") or dados["preco"] <= 0:
        logger.debug("Tentando extrair preço do HTML para %s", produto_id)
        preco_html = _extrair_preco_html(soup)
        if preco_html:
            dados["preco"] = preco_html

    if not dados.get("imagem"):
        logger.debug("Tentando extrair imagem do HTML para %s", produto_id)
        imagem_html = _extrair_imagem_html(soup)
        if imagem_html:
            dados["imagem"] = imagem_html

    # Validação mínima
    if not dados.get("titulo"):
        raise ErroParseHTML(f"Título não encontrado para {produto_id}")
    if not dados.get("preco") or dados["preco"] <= 0:
        raise ErroParseHTML(f"Preço inválido ou não encontrado para {produto_id}")

    # Monta resultado final
    resultado = {
        "id": produto_id,
        "titulo": dados["titulo"],
        "preco": dados["preco"],
        "link": final_url,
    }

    # Campos opcionais
    if dados.get("imagem"):
        resultado["imagem"] = dados["imagem"]
    if dados.get("marca"):
        resultado["marca"] = dados["marca"]
    if dados.get("descricao"):
        resultado["descricao"] = dados["descricao"]
    if dados.get("sku"):
        resultado["sku"] = dados["sku"]
    if dados.get("mpn"):
        resultado["mpn"] = dados["mpn"]
    if dados.get("moeda"):
        resultado["moeda"] = dados["moeda"]
    if dados.get("atributos"):
        resultado["atributos"] = dados["atributos"]

    logger.info(
        "Produto extraído: %s | %s | R$ %.2f", produto_id, dados["titulo"][:50], dados["preco"]
    )
    return resultado


def extrair_produtos_das_urls(urls: list[str]) -> list[dict[str, Any]]:
    """
    Extrai dados de múltiplos produtos a partir de uma lista de URLs.

    Args:
        urls: Lista de URLs de produtos do Mercado Livre.

    Returns:
        Lista de dicionários com dados dos produtos (apenas os que tiveram sucesso).
    """
    produtos = []
    for url in urls:
        try:
            produto = extrair_produto_da_url(url)
            produtos.append(produto)
        except MercadoLivreParserError as e:
            logger.warning("Falha ao extrair %s: %s", url, e)
            continue
    return produtos
