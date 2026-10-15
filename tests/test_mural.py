"""Mural de TV (static/tv.css e tv.js): regras que ja quebraram na TV do NOC."""
from __future__ import annotations

import re
from pathlib import Path

ESTATICO = Path(__file__).resolve().parent.parent / "app" / "static"


def _bloco(css: str, seletor: str) -> str:
    m = re.search(re.escape(seletor) + r"\s*\{(.*?)\}", css, re.S)
    assert m, seletor
    return m.group(1)


def test_mural_nao_estoura_para_a_direita_com_texto_longo_no_letreiro():
    """15/10: o aviso "Fora do padrao" do .188 (texto longo) alargou a coluna
    implicita da grade .tv ate o tamanho do texto do letreiro (nowrap) e o mural
    inteiro saiu 55px pela direita da TV. A coluna tem de ser explicita e nunca
    passar do container."""
    css = (ESTATICO / "tv.css").read_text(encoding="utf-8")
    assert "grid-template-columns: minmax(0, 1fr)" in _bloco(css, ".tv ")
    letreiro = _bloco(css, ".tv-ticker ")
    assert "min-width: 0" in letreiro and "overflow: hidden" in letreiro


def test_cartao_garante_uma_linha_de_etiquetas():
    """Em 1920x1080 (grade 3x3) a letra crescia ate nao sobrar altura para as
    etiquetas e o cartao ficava so com "+6". cabeEtiquetas() encolhe a letra
    ate a primeira linha caber - e tem de rodar antes do corte."""
    js = (ESTATICO / "tv.js").read_text(encoding="utf-8")
    assert "function cabeEtiquetas()" in js
    assert re.search(r"if \(mostraTags\) \{ cabeEtiquetas\(\); \}\s*cortaTags\(\);", js)
