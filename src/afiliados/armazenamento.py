"""Módulo para armazenamento de ofertas no Supabase."""

import logging
import os
from datetime import datetime

import requests
from dotenv import load_dotenv

from afiliados.http_utils import retry_com_backoff

load_dotenv()

logger = logging.getLogger(__name__)


class SupabaseError(Exception):
    """Exceção base para erros do Supabase."""

    pass


class ErroCredenciaisSupabase(SupabaseError):
    """Erro: credenciais do Supabase não configuradas."""

    pass


class ErroRedeSupabase(SupabaseError):
    """Erro de rede ao acessar o Supabase."""

    pass


class ErroRespostaSupabase(SupabaseError):
    """Erro na resposta da API do Supabase."""

    pass


def _get_supabase_url() -> str:
    """Retorna SUPABASE_URL do ambiente."""
    url = os.getenv("SUPABASE_URL", "").strip()
    if url and not url.startswith(("http://", "https://")):
        logger.warning("SUPABASE_URL não tem esquema http/https: %s", url[:50])
    return url


def _get_supabase_key() -> str:
    """Retorna SUPABASE_KEY do ambiente."""
    return os.getenv("SUPABASE_KEY", "")


def _validar_credenciais() -> None:
    """Valida se as credenciais do Supabase estão configuradas."""
    if not _get_supabase_url() or not _get_supabase_key():
        raise ErroCredenciaisSupabase(
            "SUPABASE_URL e SUPABASE_KEY devem estar configuradas no .env"
        )


def _headers() -> dict:
    """Retorna headers padrão para requisições ao Supabase."""
    key = _get_supabase_key()
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal,resolution=merge-duplicates",
    }


@retry_com_backoff(max_tentativas=3, backoff_base=1.0)
def _requisicao(
    metodo: str,
    tabela: str,
    **kwargs: object,
) -> requests.Response:
    """
    Faz requisição à REST API do Supabase com retry.

    Args:
        metodo: Método HTTP (GET, POST, PATCH, etc.)
        tabela: Nome da tabela
        **kwargs: Argumentos adicionais para requests.request

    Returns:
        Resposta da requisição

    Raises:
        ErroRedeSupabase: Em caso de falha de conexão ou timeout.
        ErroRespostaSupabase: Se a API retornar erro.
    """
    _validar_credenciais()

    base_url = _get_supabase_url()
    if not base_url.startswith(("http://", "https://")):
        raise ErroCredenciaisSupabase(
            f"SUPABASE_URL inválida: '{base_url[:50]}...'. "
            "Deve começar com http:// ou https://"
        )

    url = f"{base_url}/rest/v1/{tabela}"

    try:
        resposta = requests.request(
            metodo,
            url,
            headers=_headers(),
            timeout=10,
            **kwargs,  # type: ignore[arg-type]
        )
        resposta.raise_for_status()
    except requests.exceptions.Timeout as exc:
        raise ErroRedeSupabase("Timeout ao consultar Supabase") from exc
    except requests.exceptions.ConnectionError as exc:
        if "No connection adapters" in str(exc):
            raise ErroCredenciaisSupabase(
                f"URL do Supabase inválida: '{url[:50]}...'. "
                "Verifique se SUPABASE_URL começa com https://"
            ) from exc
        raise ErroRedeSupabase("Erro de conexão com o Supabase") from exc
    except requests.exceptions.HTTPError as exc:
        raise ErroRespostaSupabase(
            f"Erro HTTP do Supabase: {exc.response.status_code} - {exc.response.text}"
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise ErroRedeSupabase(f"Erro na requisição: {exc}") from exc

    return resposta


def buscar_preco_salvo(produto_id: str, plataforma: str = "mercado_livre") -> float | None:
    """
    Consulta o preco_atual salvo em produtos_rastreados.

    Args:
        produto_id: ID do produto no marketplace (ex: MLB123456).
        plataforma: Nome da plataforma ("mercado_livre", "shopee").

    Returns:
        preco_atual como float, ou None se não existir.
    """
    logger.debug("Buscando preço salvo: produto_id=%s, plataforma=%s", produto_id, plataforma)

    params = {
        "select": "preco_atual",
        "id": f"eq.{produto_id}",
        "plataforma": f"eq.{plataforma}",
        "limit": "1",
    }

    resposta = _requisicao("GET", "produtos_rastreados", params=params)
    dados = resposta.json()

    if not dados:
        logger.debug("Nenhum preço salvo encontrado para %s (%s)", produto_id, plataforma)
        return None

    preco = float(dados[0]["preco_atual"])
    logger.debug("Preço salvo encontrado: %.2f para %s (%s)", preco, produto_id, plataforma)
    return preco


def salvar_produto(
    produto_id: str,
    titulo: str,
    preco_novo: float,
    preco_anterior: float | None,
    link: str,
    agora: datetime,
    plataforma: str = "mercado_livre",
    imagem: str | None = None,
) -> None:
    """
    Insere ou atualiza (upsert) produto em produtos_rastreados.

    Args:
        produto_id: ID do produto no marketplace.
        titulo: Título do produto.
        preco_novo: Preço atual.
        preco_anterior: Preço anterior (pode ser None).
        link: Link do produto.
        agora: Timestamp da atualização.
        plataforma: Nome da plataforma ("mercado_livre", "shopee").
        imagem: URL da imagem do produto (opcional).
    """
    logger.debug(
        "Salvando produto: id=%s, titulo=%s, preco_novo=%.2f, plataforma=%s",
        produto_id,
        titulo[:50],
        preco_novo,
        plataforma,
    )

    payload = {
        "id": produto_id,
        "titulo": titulo,
        "preco_atual": preco_novo,
        "preco_anterior": preco_anterior,
        "link": link,
        "ultima_atualizacao": agora.isoformat(),
        "plataforma": plataforma,
    }
    if imagem:
        payload["imagem"] = imagem

    _requisicao(
        "POST",
        "produtos_rastreados",
        json=payload,
        params={"on_conflict": "id"},
    )

    logger.debug("Produto salvo com sucesso: %s (%s)", produto_id, plataforma)


def salvar_oferta(oferta: dict) -> None:
    """
    Insere uma oferta em ofertas_encontradas.

    Args:
        oferta: Dicionário com chaves:
            - produto_id (str)
            - titulo (str)
            - preco_anterior (float)
            - preco_novo (float)
            - queda_pct (float)
            - link (str, opcional)
            - plataforma (str, opcional, default: "mercado_livre")
    """
    logger.info(
        "Salvando oferta: %s | %.2f%% OFF | %s (%s)",
        oferta["titulo"][:50],
        oferta["queda_pct"],
        oferta["produto_id"],
        oferta.get("plataforma", "mercado_livre"),
    )

    payload = {
        "produto_id": oferta["produto_id"],
        "titulo": oferta["titulo"],
        "preco_anterior": oferta["preco_anterior"],
        "preco_novo": oferta["preco_novo"],
        "queda_pct": oferta["queda_pct"],
        "link": oferta.get("link"),
        "plataforma": oferta.get("plataforma", "mercado_livre"),
    }

    _requisicao("POST", "ofertas_encontradas", json=payload)

    logger.debug("Oferta salva com sucesso: %s", oferta["produto_id"])


def oferta_recente_existe(produto_id: str, plataforma: str, horas: int = 24) -> bool:
    """
    Verifica se já existe uma oferta para este produto nas últimas N horas.

    Args:
        produto_id: ID do produto.
        plataforma: Nome da plataforma.
        horas: Janela de tempo em horas (padrão: 24).

    Returns:
        True se já existe oferta recente, False caso contrário.
    """
    from datetime import datetime, timedelta

    cutoff = (datetime.now() - timedelta(hours=horas)).isoformat()

    params = {
        "select": "produto_id",
        "produto_id": f"eq.{produto_id}",
        "plataforma": f"eq.{plataforma}",
        "criado_em": f"gte.{cutoff}",
        "limit": "1",
    }

    try:
        resposta = _requisicao("GET", "ofertas_encontradas", params=params)
        dados = resposta.json()
        existe = len(dados) > 0
        if existe:
            logger.debug("Oferta recente já existe para %s (%s)", produto_id, plataforma)
        return existe
    except Exception as exc:
        logger.warning("Erro ao verificar oferta recente: %s", exc)
        return False
