"""
Ferramenta de busca na internet usando a busca embutida do Groq
(mesma chave GROQ_API_KEY, nenhuma chave nova). Tenta primeiro o
sistema "compound" (rápido); se a conta não tiver acesso a ele,
usa a busca embutida do gpt-oss (browser_search).
Sem limite de tempo (BUSCA_TIMEOUT = None no config.py).
"""

import re
from urllib.parse import urlparse

import groq
import httpx

import llm
from config import (
    SEARCH_MAX_RESULTS,
    SEARCH_MODEL,
    SEARCH_MODEL_ALTERNATIVO,
    SEARCH_TIMEOUT,
)

INSTRUCAO = (
    "You are the search module of a voice assistant. Search the web "
    "and answer in English, objectively, with only the relevant and "
    "current facts (include dates and numbers when available), in at "
    "most 5 short sentences, with no Markdown and no lists. Cite at "
    "most two sources by site name, never web addresses."
)

# Becomes True if compound reports "no access" once, to avoid spending
# one failed call on every search.
_compound_indisponivel = False

_TLDS = {
    "com","br","org","net","gov","edu","co","uk","io","app","info",
    "www","pt","en","es","m"
}

_NAMES_BONITOS = {
    "wikipedia": "Wikipédia",
    "uol": "UOL",
    "globo": "Globo",
    "g1": "G1",
    "bbc": "BBC",
    "folha": "Folha",
    "estadao": "Estadão",
    "cnn": "CNN",
    "britannica": "Britannica",
}


def _timeout():

    if SEARCH_TIMEOUT is None:
        return httpx.Timeout(None)

    return SEARCH_TIMEOUT


def _messages(query):

    return [
        {"role": "system","content": INSTRUCAO},
        {"role": "user","content": query},
    ]


def _call_compound(query):

    return llm.GROQ_CLIENT.chat.completions.create(
        model=SEARCH_MODEL,
        messages=_messages(query),
        temperature=0.2,
        max_tokens=700,
        timeout=_timeout()
    )


def _call_browser(query):

    # Reasoning tokens count toward the output limit, so the
    # output limit has some headroom and the reasoning effort is "low".
    return llm.GROQ_CLIENT.chat.completions.create(
        model=SEARCH_MODEL_ALTERNATIVO,
        messages=_messages(query),
        tools=[{"type": "browser_search"}],
        temperature=0.2,
        max_tokens=5000,
        extra_body={"reasoning_effort": "low"},
        timeout=_timeout()
    )


def _clear_text(text):
    """Tira marcas de citação (【1†L14-L25】) e espaços invisíveis."""

    text = re.sub(r"【[^】]*】","",text)
    text = re.sub(r"[\u202f\u00a0\u200b]"," ",text)
    text = text.replace("\u2011","-")

    return re.sub(r"\s+"," ",text).strip()


def _name_of_site(url):
    """mundoeducacao.uol.com.br -> UOL ; pt.wikipedia.org -> Wikipédia"""

    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return ""

    parts = [p for p in host.split(".") if p and p not in _TLDS]

    if not parts:
        return ""

    base = parts[-1]

    return _NAMES_BONITOS.get(base,base.capitalize())


def tool_search_web(query=""):

    global _compound_indisponivel

    if not query or not query.strip():

        return {
            "success": False,
            "error": "No query was provided."
        }

    query = query.strip()

    try:

        if _compound_indisponivel:

            response = _call_browser(query)

        else:

            try:

                response = _call_compound(query)

            except groq.APIStatusError as e:

                if e.status_code not in (400,403,404):
                    raise

                _compound_indisponivel = True

                print(
                    f"\n[ℹ] {SEARCH_MODEL} indisponível "
                    f"(HTTP {e.status_code}): {e.message}\n"
                    f"    Usando a busca do {SEARCH_MODEL_ALTERNATIVO} "
                    f"(browser_search)."
                )

                response = _call_browser(query)

    except groq.RateLimitError:

        return {
            "success": False,
            "error": "Groq search usage limit reached."
        }

    except groq.AuthenticationError:

        return {
            "success": False,
            "error": "The Groq key was rejected for search."
        }

    except groq.APIStatusError as e:

        return {
            "success": False,
            "error": f"Groq refused the search (HTTP {e.status_code}): "
                    f"{e.message}"
        }

    except Exception as e:

        return {
            "success": False,
            "error": f"I couldn't reach the search: {e}"
        }

    try:

        message = response.choices[0].message
        text = _clear_text(message.content or "")

    except Exception:

        message = None
        text = ""

    if not text:

        return {
            "success": False,
            "error": "The search returned no results."
        }

    # Sources consulted by Groq.
    results = []
    names = []

    for tool in getattr(message,"executed_tools",None) or []:

        achados = getattr(
            getattr(tool,"search_results",None),"results",None
        ) or []

        for r in achados[:SEARCH_MAX_RESULTS]:

            url = getattr(r,"url","") or ""
            site = _name_of_site(url)

            results.append({
                "title": getattr(r,"title","") or "",
                "url": url,
                "site": site,
            })

            if site and site not in names:
                names.append(site)

    return {
        "success": True,
        "response": text,
        "fontes": names[:3],
        "resultados": results[:3],
    }