"""Catalogo de versoes disponiveis: o que ja foi baixado do TDN.

Nao existe catalogo publico consultavel do TOTVS RM, e automatizar login no
portal com a credencial de alguem seria fragil e inseguro. O caminho aqui e
outro: **baixar o pacote ja e a declaracao de que aquela versao existe**. O RMon
so LE a pasta onde os downloads sao guardados e transforma o nome de cada
arquivo em "versao disponivel", que a tela compara com o que esta instalado.

O que o nome do arquivo nao revelar entra pelo cadastro manual (tela do admin).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from .inventory import compare_versions

log = logging.getLogger("rmon.packages")

# Teto de seguranca: a pasta e alimentada por gente, e uma varredura sem limite
# num diretorio errado (ou num ponto de montagem) travaria o ciclo.
MAX_ARQUIVOS = 2000
MAX_PROFUNDIDADE = 3

# Extensoes que valem como "pacote baixado"
EXTENSOES = {".exe", ".msi", ".zip", ".rar", ".7z", ".cab", ".dll"}

# Versao dentro do nome do arquivo: tres ou quatro componentes numericos.
# Pega a ULTIMA ocorrencia - "RM_12.1_pacote_12.1.2606.126.exe" e o produto
# "RM_12.1" na versao 12.1.2606.126, nao o contrario.
_VERSAO = re.compile(r"\d+(?:\.\d+){2,3}")

# Pacotes do proprio produto nao trazem o nome do item de inventario
# ("RM - versao base"); estes apelidos ligam os dois.
ALIASES = {
    "rm": "rm:rm - versao base",
    "totvsrm": "rm:rm - versao base",
    "corporerm": "rm:rm - versao base",
    "rmnet": "rm:rm - versao base",
}

# Quando o mesmo nome existe em mais de uma fonte, o instalador se refere a esta
_PREFERENCIA_FONTE = ("reg", "cst", "asm", "rm", "kb")


def match_key(nome: str) -> str:
    """Chave de casamento entre nome de arquivo e nome de item do inventario.

    Descarta tudo que nao e letra ou numero: o mesmo pacote aparece como
    `TOTVS_CES_RM_Office365_CNI` no registro e como
    `TOTVS-CES-RM-Office365-CNI` no nome do arquivo baixado.
    """
    return re.sub(r"[^a-z0-9]+", "", (nome or "").lower())


def parse_filename(nome: str) -> tuple[str, str] | None:
    """'TOTVS_CES_RM_Office365_CNI_12.1.2602.002.exe' -> (produto, versao)."""
    base = Path(nome).stem
    achados = list(_VERSAO.finditer(base))
    if not achados:
        return None
    ultimo = achados[-1]
    produto = base[:ultimo.start()].strip(" _-.")
    if not produto:
        produto = base[ultimo.end():].strip(" _-.")
    return (produto or base), ultimo.group(0)


def scan(diretorio: str) -> list[dict[str, Any]]:
    """Le o repositorio de pacotes. Nunca levanta excecao."""
    raiz = Path(diretorio)
    entradas: list[dict[str, Any]] = []
    try:
        if not raiz.is_dir():
            return []
        for caminho in sorted(raiz.rglob("*")):
            if len(entradas) >= MAX_ARQUIVOS:
                log.warning("repositorio de pacotes truncado em %d arquivos", MAX_ARQUIVOS)
                break
            try:
                relativo = caminho.relative_to(raiz)
            except ValueError:
                continue
            if len(relativo.parts) > MAX_PROFUNDIDADE or not caminho.is_file():
                continue
            if caminho.suffix.lower() not in EXTENSOES:
                continue
            partes = parse_filename(caminho.name)
            if not partes:
                continue
            produto, versao = partes
            entradas.append({
                "produto": produto[:200], "version": versao[:80],
                "arquivo": str(relativo).replace("\\", "/")[:400],
                "tamanho": caminho.stat().st_size,
                # a subpasta e uma pista de agrupamento util na tela
                "pasta": (relativo.parts[0] if len(relativo.parts) > 1 else None),
            })
    except OSError as exc:
        log.warning("nao consegui ler o repositorio de pacotes %s: %s", diretorio, exc)
    return entradas


def build_index(inventario: list[dict]) -> dict[str, str]:
    """Nome de item do inventario -> pkg_key, para casar com o nome do arquivo.

    O mesmo nome pode existir em fontes diferentes (o pacote no registro e o
    .dll na pasta Custom); vale a fonte a que o instalador se refere.
    """
    indice: dict[str, str] = {}
    for row in inventario:
        chave = match_key(row.get("name") or "")
        if not chave:
            continue
        atual = indice.get(chave)
        novo = row.get("pkg_key") or ""
        if atual is None or _peso(novo) < _peso(atual):
            indice[chave] = novo
    return indice


def _peso(pkg_key: str) -> int:
    prefixo = (pkg_key or "").split(":", 1)[0]
    return _PREFERENCIA_FONTE.index(prefixo) if prefixo in _PREFERENCIA_FONTE else 99


def resolve(produto: str, indice: dict[str, str],
            vinculos: dict[str, str] | None = None) -> str | None:
    """Descobre a que item do inventario um pacote baixado se refere.

    Ordem: vinculo feito a mao na tela, apelido conhecido, nome igual ao do
    item. Sem casamento, a entrada fica visivel no catalogo esperando alguem
    dizer a que ela pertence - melhor do que adivinhar errado e mostrar
    "atualizacao disponivel" para o item errado.
    """
    chave = match_key(produto)
    if not chave:
        return None
    for fonte in (vinculos or {}, ALIASES, indice):
        achado = fonte.get(chave)
        if achado:
            return achado
    return None


def available_by_key(catalogo: list[dict]) -> dict[str, dict]:
    """Maior versao disponivel por item do inventario."""
    melhor: dict[str, dict] = {}
    for entrada in catalogo:
        chave = entrada.get("pkg_key")
        if not chave:
            continue
        atual = melhor.get(chave)
        if atual is None or compare_versions(entrada.get("version"), atual.get("version")) > 0:
            melhor[chave] = entrada
    return melhor
