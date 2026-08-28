"""
Gerador de Documentos de GD — Exergia
App único para gerar os documentos de Geração Distribuída (Energisa-MT e
Equatorial-GO) sem precisar editar JSON ou rodar comandos manualmente.

Rodar localmente:
    pip install -r requirements.txt
    streamlit run app.py

Publicar (acesso pelo navegador de qualquer lugar):
    Veja instruções no LEIA-ME.md
"""
import io
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from datetime import date, timedelta

import streamlit as st
import pandas as pd
from docx import Document

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))

from preencher_gd import preencher as preencher_energisa
from recalc import recalc as recalc_xlsx
from office.soffice import run_soffice
from xml_cell_writer import restringir_impressao_para_pdf
from preencher_equatorial import preencher as preencher_anexo1
from preencher_docx_equatorial import (
    replace_everywhere,
    montar_substituicoes_memorial,
    montar_substituicoes_comissionamento,
    fix_tabela_gerador,
    substituir_foto_localizacao,
)

st.set_page_config(page_title="Gerador GD — Exergia", page_icon="\u2600\ufe0f", layout="wide")

MODELO_ENERGISA = BASE / "memorialgd_1.xlsm"
MODELO_ANEXO1 = BASE / "NT_00020_EQTL-06-Anexo-I-Formulario-de-Solicitacao.xlsx"
MODELO_MEMORIAL = BASE / "Memorial_Descritivo_MODELO.docx"
MODELO_COMISSIONAMENTO = BASE / "Modelo_Comissionamento_MODELO.docx"

REFERENCIA_HAKKNNER = {
    "nome": "Fulano de Tal da Silva",
    "nome_maiusculo": "FULANO DE TAL DA SILVA",
    "conta_contrato": "0.000.000.000-00",
    "endereco_uc": "Rua Exemplo, Q. 00 L. 00 S/N.",
    "bairro_cidade": "Setor Exemplo. Aragarças – GO.",
    "cep": "76240000",
    "cidade": "Aragarças",
    "coordenadas": "UTM 22K, X: 366859 Y: 8240498",
    "equip_titulo": "5 kW",
    "equip_desc": "8 módulos OSDA/ODA590-36V-MHD",
    "modalidade": "autoconsumo remoto",
    "mes_ano": "Junho – 2026",
    "estado": "Goiás",
    "fab_modulo": "OSDA",
    "modelo_modulo": "ODA590-36V-MHD",
    "telefone_cliente": "(00) 00000-0000",
    "uc_pontuado": "0.000.000.000-00",
    "data_conclusao": "24/06/2026",
    "endereco_completo": "RUA EXEMPLO, Q. 00, L. 00, S/N, CASA - RESIDENCIA\nSETOR EXEMPLO",
    "fab_inversor": "SOFAR",
    "modelo_inversor": "5KTLM-G3",
    "fab_inversor_typo_memorial": "SOAR",
}

MES_PT = ["JANEIRO", "FEVEREIRO", "MARÇO", "ABRIL", "MAIO", "JUNHO", "JULHO",
          "AGOSTO", "SETEMBRO", "OUTUBRO", "NOVEMBRO", "DEZEMBRO"]


def slug_arquivo(nome: str, uc: str) -> str:
    base = nome.strip().upper().replace(" ", "_")
    uc_limpo = uc.strip().replace(" ", "_")
    return f"{base}_UC_{uc_limpo}"


def responsavel_tecnico_form():
    with st.expander("Responsável técnico (raramente muda)"):
        c1, c2 = st.columns(2)
        nome = c1.text_input("Nome", "Alex Gomes da Silva")
        email = c2.text_input("E-mail", "alexgomes.eng.energ@gmail.com")
        c3, c4 = st.columns(2)
        telefone = c3.text_input("Telefone", "(66) 9 9964-5914")
        registro = c4.text_input("Registro profissional (CREA)", "043613")
    return {"nome": nome, "email": email, "telefone": telefone, "registro": registro}


def tabela_equipamentos(label, colunas, valores_padrao, key):
    st.caption(label)
    df = pd.DataFrame(valores_padrao)
    edited = st.data_editor(df, num_rows="dynamic", key=key, width="stretch")
    return edited.to_dict("records")


# ============================================================
# Downloads persistentes: o Streamlit reroda o script inteiro a cada
# clique (inclusive em botão de download), e antes disso os arquivos
# ficavam só numa pasta temporária que já tinha sido apagada -- por isso
# os outros downloads "sumiam". Agora o conteúdo de cada arquivo fica
# guardado em st.session_state (na memória da sessão do navegador) e os
# botões são desenhados a partir dali, então sobrevivem a qualquer clique.
# ============================================================
def _zip_bytes(itens):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for _, nome_arquivo, conteudo in itens:
            zf.writestr(nome_arquivo, conteudo)
    return buf.getvalue()


def salvar_resultado(chave_sessao, principais, separados=None, nome_zip="documentos.zip",
                      avisos=None, aviso_final=None):
    """principais / separados: lista de tuplas (rotulo, nome_arquivo, bytes)."""
    st.session_state[chave_sessao] = {
        "principais": principais,
        "separados": separados or [],
        "nome_zip": nome_zip,
        "avisos": avisos,
        "aviso_final": aviso_final,
    }


def mostrar_downloads(chave_sessao):
    r = st.session_state.get(chave_sessao)
    if not r:
        return

    if r["avisos"]:
        st.warning("⚠️ Checagem de viabilidade do Anexo I:\n" + "\n".join(f"- {a}" for a in r["avisos"]))
    elif r["avisos"] is not None:
        st.success("Checagem de viabilidade: OK")

    st.success("Documentos gerados!")

    todos = r["principais"] + r["separados"]
    st.download_button("⬇️ Baixar tudo (.zip)", _zip_bytes(todos), r["nome_zip"],
                        type="primary", key=f"{chave_sessao}_zip_tudo")

    cols = st.columns(len(r["principais"]))
    for col, (rotulo, nome_arquivo, conteudo) in zip(cols, r["principais"]):
        col.download_button(f"⬇️ {rotulo}", conteudo, nome_arquivo, key=f"{chave_sessao}_{nome_arquivo}")

    if r["separados"]:
        st.subheader("Documentos separados")
        cols2 = st.columns(len(r["separados"]))
        for col, (rotulo, nome_arquivo, conteudo) in zip(cols2, r["separados"]):
            col.download_button(f"⬇️ {rotulo}", conteudo, nome_arquivo, key=f"{chave_sessao}_{nome_arquivo}")

    if r["aviso_final"]:
        st.info(r["aviso_final"])


# ============================================================
# ARQUIVO DE DADOS PRONTO (.txt) — upgrade pedido pelo usuário: em vez de
# preencher o formulário na mão, é possível enviar um arquivo de texto
# simples no formato "chave: valor" (um por linha) já com todos os dados,
# e o app gera os documentos direto a partir dele.
#
# Para listas (cargas, painéis, inversores, módulos), repete-se um bloco
# "[nome_da_lista]" por item, um bloco por linha em branco. Uma linha em
# branco também "fecha" o bloco atual e volta a ler campos comuns.
# ============================================================
def _float(valor, padrao=0.0):
    if valor is None or str(valor).strip() == "":
        return padrao
    try:
        return float(str(valor).strip().replace(",", "."))
    except ValueError:
        return padrao


def _int(valor, padrao=0):
    if valor is None or str(valor).strip() == "":
        return padrao
    try:
        return int(float(str(valor).strip().replace(",", ".")))
    except ValueError:
        return padrao


def _data_txt(valor, padrao=None):
    """Aceita DD/MM/AAAA ou AAAA-MM-DD; se não conseguir ler, usa `padrao`."""
    if not valor or not str(valor).strip():
        return padrao
    from datetime import datetime as _dt
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return _dt.strptime(str(valor).strip(), fmt).date()
        except ValueError:
            continue
    return padrao


def parse_arquivo_dados(texto: str) -> dict:
    """Lê o formato 'chave: valor' com blocos [secao] para tabelas.
    Retorna {"campos": {...}, "tabelas": {"secao": [ {...}, {...} ]}}."""
    campos = {}
    tabelas = {}
    secao_atual = None
    item_atual = None

    for linha_bruta in texto.splitlines():
        linha = linha_bruta.strip()
        if not linha or linha.startswith("#"):
            secao_atual = None
            item_atual = None
            continue
        if linha.startswith("[") and linha.endswith("]"):
            nome_secao = linha[1:-1].strip().lower()
            secao_atual = nome_secao
            item_atual = {}
            tabelas.setdefault(nome_secao, []).append(item_atual)
            continue
        if ":" not in linha:
            continue
        chave, valor = linha.split(":", 1)
        chave = chave.strip().lower().replace(" ", "_")
        valor = valor.strip()
        if secao_atual is not None and item_atual is not None:
            item_atual[chave] = valor
        else:
            campos[chave] = valor

    return {"campos": campos, "tabelas": tabelas}


TEMPLATE_ENERGISA_TXT = """\
# ============================================================
# MODELO — Energisa-MT — Gerador de Documentos GD
# Preencha "chave: valor", uma linha por campo. Linhas com # são só
# explicação e podem ser apagadas. Para repetir cargas/painéis/inversores,
# copie o bloco [xxx] quantas vezes precisar (um bloco por item).
# ============================================================

uc:
classe: RESIDENCIAL
titular:
logradouro:
numero:
bairro:
cidade:
uf: MT
cep:
email: não informado
celular:
cpf_cnpj:

potencia_instalada_kw:
tensao_atendimento_v: 220
tipo_conexao: MONOFÁSICO
tipo_ramal: AÉREO

# ---- Relação de carga: repita o bloco [cargas] para cada equipamento ----

[cargas]
quantidade: 2
equipamento: CHUVEIRO
pot_unitaria_w: 4500
fator_demanda: 0.82

[cargas]
quantidade: 1
equipamento: Ar Condicionado
pot_unitaria_w: 1300
fator_demanda: 0.82

# ---- Proteções e padrão ----

disjuntor_geral_a: 50
fator_potencia: 0.92
demanda_contratada_kw: 0
dps_ca_ka: 20
disjuntor_ca_a: 50
dps_cc_ka: 20
disjuntor_cc_a: 32
modalidade: Autoconsumo remoto
potencia_trafo:
numero_hastes: 6

# ---- Coordenadas e previsão de ligação ----

fuso:
coord_x:
coord_y:
data_previsao_ligacao: 15/09/2026
zona: URBANO
gd_ja_instalado: NÃO

# ---- Painéis: repita o bloco [paineis] para cada modelo ----

[paineis]
quantidade: 10
fabricante:
modelo:
area_m2:
potencia_kw:

# ---- Inversores: repita o bloco [inversores] para cada modelo ----

[inversores]
quantidade: 1
fabricante:
modelo:
potencia_kw:
tensao_nominal_v: 220

# ---- Responsável técnico (opcional — se deixar em branco, usa o padrão) ----

resp_nome:
resp_telefone:
resp_email:
"""

TEMPLATE_EQUATORIAL_TXT = """\
# ============================================================
# MODELO — Equatorial-GO — Gerador de Documentos GD
# Preencha "chave: valor", uma linha por campo. Linhas com # são só
# explicação e podem ser apagadas. Para repetir módulos/inversores, copie
# o bloco [xxx] quantas vezes precisar (um bloco por item).
# ============================================================

nome:
cpf_cnpj:
celular:
endereco:
email:
cep: 76240000
municipio: Aragarças
uf: GO
bairro:
uc_existente:

tipo_ligacao: MONOFÁSICO
tensao_atendimento_v: 220
classe: Residencial
disjuntor_entrada_a: 50
carga_declarada_kw: 10
potencia_disponibilizada_kw: 10
tipo_ramal: AÉREO

fuso:
coord_x:
coord_y:
modalidade_compensacao: AUTOCONSUMO LOCAL
data_inicio_operacao: 15/09/2026

# ---- Módulos: repita o bloco [modulos] para cada modelo ----

[modulos]
potencia_w: 590
quantidade: 8
fabricante: OSDA
modelo: ODA590-36V-MHD

# ---- Inversores: repita o bloco [inversores] para cada modelo ----

[inversores]
fabricante: SOFAR
modelo: 5KTLM-G3
potencia_nominal_kw: 5
faixa_tensao_v: 220
corrente_nominal_a:
fator_potencia: 1
rendimento_pct: 98
dht_corrente_pct: 3

# ---- Responsável técnico (opcional — se deixar em branco, usa o padrão) ----

resp_nome:
resp_telefone:
resp_email:

# ---- Observação ----
# A foto de localização (opcional) continua sendo enviada separadamente,
# pelo campo de upload de imagem do formulário (seção 6).
"""


def montar_dados_energisa_de_txt(texto: str, resp_padrao: dict) -> dict:
    parsed = parse_arquivo_dados(texto)
    c = parsed["campos"]
    t = parsed["tabelas"]

    cargas = [
        {
            "quantidade": _int(item.get("quantidade")),
            "equipamento": item.get("equipamento", ""),
            "pot_unitaria_w": _float(item.get("pot_unitaria_w")),
            "fator_demanda": _float(item.get("fator_demanda")),
        }
        for item in t.get("cargas", [])
    ]
    paineis = [
        {
            "quantidade": _int(item.get("quantidade")),
            "fabricante": item.get("fabricante", ""),
            "modelo": item.get("modelo", ""),
            "area_m2": _float(item.get("area_m2")),
            "potencia_kw": _float(item.get("potencia_kw")),
        }
        for item in t.get("paineis", [])
    ]
    inversores = [
        {
            "quantidade": _int(item.get("quantidade")),
            "fabricante": item.get("fabricante", ""),
            "modelo": item.get("modelo", ""),
            "potencia_kw": _float(item.get("potencia_kw")),
            "tensao_nominal_v": item.get("tensao_nominal_v", "220"),
        }
        for item in t.get("inversores", [])
    ]

    data_prevista = _data_txt(c.get("data_previsao_ligacao")) or (date.today() + timedelta(days=15))

    resp = {
        "nome": c.get("resp_nome") or resp_padrao["nome"],
        "telefone": c.get("resp_telefone") or resp_padrao["telefone"],
        "email": c.get("resp_email") or resp_padrao["email"],
    }

    return {
        "responsavel_tecnico": resp,
        "cliente": {
            "uc": c.get("uc", ""), "classe": c.get("classe", "RESIDENCIAL"),
            "titular": c.get("titular", ""), "logradouro": c.get("logradouro", ""),
            "numero": c.get("numero", ""), "bairro": c.get("bairro", ""),
            "cidade": c.get("cidade", ""), "uf": c.get("uf", "MT"), "cep": c.get("cep", ""),
            "email": c.get("email") or "não informado", "telefone": "",
            "celular": c.get("celular", ""), "cpf_cnpj": c.get("cpf_cnpj", ""),
            "potencia_instalada_kw": _float(c.get("potencia_instalada_kw")),
            "tensao_atendimento_v": c.get("tensao_atendimento_v", "220"),
            "tipo_conexao": c.get("tipo_conexao", "MONOFÁSICO"),
            "tipo_ramal": c.get("tipo_ramal", "AÉREO"),
            "tipo_fonte_geracao": "SOLAR FOTOVOLTAICA",
            "tipo_geracao": "Empregando conversor eletrônico/inversor",
            "cargas": cargas,
            "disjuntor_geral_a": _int(c.get("disjuntor_geral_a"), 50),
            "fator_potencia": _float(c.get("fator_potencia"), 0.92),
            "demanda_contratada_kw": _float(c.get("demanda_contratada_kw")),
            "dps_ca_ka": _int(c.get("dps_ca_ka"), 20),
            "disjuntor_ca_a": _int(c.get("disjuntor_ca_a"), 50),
            "dps_cc_ka": _int(c.get("dps_cc_ka"), 20),
            "disjuntor_cc_a": _int(c.get("disjuntor_cc_a"), 32),
            "modalidade": c.get("modalidade", "Autoconsumo remoto"),
            "potencia_trafo": c.get("potencia_trafo", ""),
            "numero_hastes": _int(c.get("numero_hastes"), 6),
            "demanda_contratada_kwg": 0,
            "fuso": c.get("fuso", ""), "coord_x": c.get("coord_x", ""), "coord_y": c.get("coord_y", ""),
            "tipo_tensao": "BAIXA", "cabos_por_fase": 1,
            "bitola_fase": 10, "bitola_neutro": 10, "bitola_terra": 10,
            "gd_ja_instalado": c.get("gd_ja_instalado", "NÃO"),
            "mes_previsao_ligacao": MES_PT[data_prevista.month - 1],
            "ano_previsao_ligacao": data_prevista.year,
            "zona": c.get("zona", "URBANO"),
            "paineis": paineis, "inversores": inversores,
            "necessita_autotrafo": "NÃO", "trafo_exclusivo": "NÃO",
        },
    }


def montar_dados_equatorial_de_txt(texto: str, resp_padrao: dict) -> dict:
    parsed = parse_arquivo_dados(texto)
    c = parsed["campos"]
    t = parsed["tabelas"]

    modulos = [
        {
            "potencia_w": _float(item.get("potencia_w")),
            "quantidade": _int(item.get("quantidade")),
            "fabricante": item.get("fabricante", ""),
            "modelo": item.get("modelo", ""),
        }
        for item in t.get("modulos", [])
    ]
    inversores = [
        {
            "fabricante": item.get("fabricante", ""),
            "modelo": item.get("modelo", ""),
            "potencia_nominal_kw": _float(item.get("potencia_nominal_kw")),
            "faixa_tensao_v": item.get("faixa_tensao_v", "220"),
            "corrente_nominal_a": _float(item.get("corrente_nominal_a")),
            "fator_potencia": _float(item.get("fator_potencia"), 1.0),
            "rendimento_pct": _float(item.get("rendimento_pct"), 98.0),
            "dht_corrente_pct": _float(item.get("dht_corrente_pct"), 3.0),
        }
        for item in t.get("inversores", [])
    ]

    data_inicio = _data_txt(c.get("data_inicio_operacao")) or (date.today() + timedelta(days=15))
    uf = c.get("uf", "GO")
    endereco = c.get("endereco", "")
    bairro = c.get("bairro", "")
    uc_existente = c.get("uc_existente", "")
    celular = c.get("celular", "")

    resp_nome = c.get("resp_nome") or resp_padrao["nome"]
    resp_telefone = c.get("resp_telefone") or resp_padrao["telefone"]
    resp_email = c.get("resp_email") or resp_padrao["email"]

    return {
        "responsavel_tecnico": {
            "nome": resp_nome, "titulo_profissional": "Engenheiro de Energia",
            "registro_profissional": 436143, "uf_registro": "MT",
            "email": resp_email,
            "telefone": resp_telefone.replace(" ", "").replace("(", "").replace(")", "").replace("-", ""),
        },
        "premissas_fixas": {
            "tarifa_branca": "NÃO", "vistoria_apos_solicitacao": "NÃO",
            "autoriza_entrega_contratos": "SIM", "declara_conformidade_normas": "SIM",
            "grid_zero": "NÃO", "gratuidade_ren": "NÃO",
            "autoriza_faturas_email": "NÃO", "declara_veracidade": "SIM",
        },
        "_referencia_hakknner": REFERENCIA_HAKKNNER,
        "cliente": {
            "nome": c.get("nome", ""), "cpf_cnpj": c.get("cpf_cnpj", ""), "celular": celular,
            "telefone_fixo": "",
            "endereco": endereco, "email": c.get("email", ""), "cep": c.get("cep", "76240000"),
            "municipio": c.get("municipio", "Aragarças"),
            "uf": uf, "uf_extenso": "Goiás" if uf == "GO" else uf,
            "receber_fatura_email": "NÃO",
            "tipo_orcamento": "Orçamento de Conexão", "uc_existente": uc_existente,
            "tipo_solicitacao": "CONEXÃO DE GD EM UNIDADE CONSUMIDORA EXISTENTE SEM AUMENTO DE POTÊNCIA DISPONIBILIZADA (ver item abaixo)",
            "cargas_especiais": "NÃO", "ramo_atividade": c.get("classe", "Residencial"),
            "classe": c.get("classe", "Residencial"),
            "tipo_ligacao": c.get("tipo_ligacao", "MONOFÁSICO"),
            "tensao_atendimento_v": _int(c.get("tensao_atendimento_v"), 220),
            "carga_declarada_kw": _float(c.get("carga_declarada_kw"), 10.0),
            "disjuntor_entrada_a": _int(c.get("disjuntor_entrada_a"), 50),
            "potencia_disponibilizada_kw": _float(c.get("potencia_disponibilizada_kw"), 10.0),
            "tipo_ramal": c.get("tipo_ramal", "AÉREO"),
            "fuso": c.get("fuso", ""), "coord_x": c.get("coord_x", ""), "coord_y": c.get("coord_y", ""),
            "modalidade_compensacao": c.get("modalidade_compensacao", "AUTOCONSUMO LOCAL"),
            "data_inicio_operacao": data_inicio.isoformat(),
            "conta_contrato": uc_existente, "bairro": bairro,
            "mes_ano_documento": f"{MES_PT[data_inicio.month - 1].capitalize()} – {data_inicio.year}",
            "telefone_cliente": celular,
            "data_conclusao_geradora": data_inicio.strftime("%d/%m/%Y"),
            "endereco_completo_comissionamento": f"{endereco.upper()}\n{bairro.upper()}",
            "modulos": modulos, "inversores": inversores,
        },
    }


# ============================================================
# ENERGISA-MT
# ============================================================
def form_energisa():
    st.header("Energisa-MT")
    resp = responsavel_tecnico_form()

    with st.expander("📄 Já tem os dados prontos? Preencha a partir de um arquivo .txt"):
        st.caption(
            "Baixe o modelo, preencha os campos num editor de texto simples "
            "(Bloco de Notas, etc.) e envie de volta aqui — os documentos são "
            "gerados direto, sem precisar preencher o formulário abaixo."
        )
        st.download_button(
            "⬇️ Baixar modelo (.txt)", TEMPLATE_ENERGISA_TXT, "modelo_energisa.txt",
            key="modelo_energisa_txt",
        )
        arquivo_txt = st.file_uploader(
            "Enviar arquivo preenchido (.txt)", type=["txt"], key="upload_energisa_txt",
        )
        if arquivo_txt is not None:
            texto = arquivo_txt.getvalue().decode("utf-8", errors="replace")
            dados_txt = montar_dados_energisa_de_txt(texto, resp)
            st.caption("Prévia dos dados lidos do arquivo:")
            st.json(dados_txt, expanded=False)
            if st.button("Gerar documentos a partir do arquivo", type="primary", key="gerar_energisa_txt"):
                if not dados_txt["cliente"]["uc"] or not dados_txt["cliente"]["titular"]:
                    st.error("O arquivo precisa ter pelo menos os campos 'uc' e 'titular' preenchidos.")
                else:
                    gerar_energisa(dados_txt)

    st.markdown("---")

    st.subheader("1. Dados da Unidade Consumidora")
    c1, c2, c3 = st.columns(3)
    uc = c1.text_input("UC")
    classe = c2.selectbox("Classe", ["RESIDENCIAL", "COMERCIAL", "INDUSTRIAL", "RURAL"])
    titular = c3.text_input("Titular")

    c1, c2, c3 = st.columns(3)
    logradouro = c1.text_input("Logradouro")
    numero = c2.text_input("Número")
    bairro = c3.text_input("Bairro")

    c1, c2, c3 = st.columns(3)
    cidade = c1.text_input("Cidade")
    uf = c2.text_input("UF", "MT")
    cep = c3.text_input("CEP")

    c1, c2, c3 = st.columns(3)
    email = c1.text_input("E-mail", "não informado")
    celular = c2.text_input("Celular")
    cpf_cnpj = c3.text_input("CPF/CNPJ")

    st.subheader("2. Dados da UC no ato da vistoria")
    c1, c2, c3, c4 = st.columns(4)
    potencia_instalada_kw = c1.number_input("Potência Instalada (kW)", min_value=0.0, step=0.1)
    tensao_atendimento_v = c2.text_input("Tensão de Atendimento (V)", "220",
                                          help='Use "220/380" para trifásico em baixa tensão')
    tipo_conexao = c3.selectbox("Tipo de Conexão", ["MONOFÁSICO", "BIFÁSICO", "TRIFÁSICO"])
    tipo_ramal = c4.selectbox("Tipo de Ramal", ["AÉREO", "SUBTERRÂNEO"])

    st.subheader("3. Relação de Carga")
    cargas = tabela_equipamentos(
        "Equipamentos (adicione/remova linhas como precisar)",
        None,
        [
            {"quantidade": 2, "equipamento": "CHUVEIRO", "pot_unitaria_w": 4500, "fator_demanda": 0.82},
            {"quantidade": 2, "equipamento": "Ar Condicionado", "pot_unitaria_w": 1300, "fator_demanda": 0.82},
            {"quantidade": 1, "equipamento": "Eletrodomésticos", "pot_unitaria_w": 2000, "fator_demanda": 0.8},
            {"quantidade": 1, "equipamento": "Eletrônicos", "pot_unitaria_w": 800, "fator_demanda": 0.8},
        ],
        "cargas_energisa",
    )

    st.subheader("4. Proteções e padrão")
    c1, c2, c3 = st.columns(3)
    disjuntor_geral_a = c1.number_input("Disjuntor geral (A)", value=50)
    fator_potencia = c2.number_input("Fator de Potência", value=0.92, step=0.01)
    demanda_contratada_kw = c3.number_input("Demanda Contratada (kW)", value=0.0)

    c1, c2, c3, c4 = st.columns(4)
    dps_ca_ka = c1.number_input("DPS CA (kA)", value=20)
    disjuntor_ca_a = c2.number_input("Disjuntor CA (A)", value=50)
    dps_cc_ka = c3.number_input("DPS CC (kA)", value=20)
    disjuntor_cc_a = c4.number_input("Disjuntor CC (A)", value=32)

    c1, c2, c3 = st.columns(3)
    modalidade = c1.selectbox("Modalidade", ["Autoconsumo remoto", "Autoconsumo local",
                                              "Geração compartilhada", "Múltiplas UCs"])
    potencia_trafo = c2.text_input("Potência Trafo (kVA) — deixe em branco se não houver")
    numero_hastes = c3.number_input("Número de hastes", value=6)

    st.subheader("5. Coordenadas e previsão de ligação")
    c1, c2, c3 = st.columns(3)
    fuso = c1.text_input("Fuso (ex: 22L)")
    coord_x = c2.text_input("X")
    coord_y = c3.text_input("Y")

    c1, c2, c3 = st.columns(3)
    data_prevista = c1.date_input("Previsão de ligação", value=date.today() + timedelta(days=15))
    zona = c2.selectbox("Zona", ["URBANO", "RURAL"])
    gd_ja_instalado = c3.selectbox("Sistema GD já instalado?", ["SIM", "NÃO"])

    st.subheader("6. Painéis")
    paineis = tabela_equipamentos(
        "Um item por modelo de painel diferente",
        None,
        [{"quantidade": 0, "fabricante": "", "modelo": "", "area_m2": 0, "potencia_kw": 0.0}],
        "paineis_energisa",
    )

    st.subheader("7. Inversores")
    inversores = tabela_equipamentos(
        "Um item por modelo de inversor diferente",
        None,
        [{"quantidade": 0, "fabricante": "", "modelo": "", "potencia_kw": 0.0, "tensao_nominal_v": 220}],
        "inversores_energisa",
    )

    gerar = st.button("Gerar documentos (Energisa-MT)", type="primary")

    if gerar:
        if not uc or not titular:
            st.error("Preencha ao menos UC e Titular.")
            return

        dados = {
            "responsavel_tecnico": {
                "nome": resp["nome"], "telefone": resp["telefone"], "email": resp["email"],
            },
            "cliente": {
                "uc": uc, "classe": classe, "titular": titular, "logradouro": logradouro,
                "numero": numero, "bairro": bairro, "cidade": cidade, "uf": uf, "cep": cep,
                "email": email, "telefone": "", "celular": celular, "cpf_cnpj": cpf_cnpj,
                "potencia_instalada_kw": potencia_instalada_kw,
                "tensao_atendimento_v": tensao_atendimento_v,
                "tipo_conexao": tipo_conexao, "tipo_ramal": tipo_ramal,
                "tipo_fonte_geracao": "SOLAR FOTOVOLTAICA",
                "tipo_geracao": "Empregando conversor eletrônico/inversor",
                "cargas": cargas,
                "disjuntor_geral_a": disjuntor_geral_a, "fator_potencia": fator_potencia,
                "demanda_contratada_kw": demanda_contratada_kw,
                "dps_ca_ka": dps_ca_ka, "disjuntor_ca_a": disjuntor_ca_a,
                "dps_cc_ka": dps_cc_ka, "disjuntor_cc_a": disjuntor_cc_a,
                "modalidade": modalidade,
                "potencia_trafo": potencia_trafo, "numero_hastes": numero_hastes,
                "demanda_contratada_kwg": 0,
                "fuso": fuso, "coord_x": coord_x, "coord_y": coord_y,
                "tipo_tensao": "BAIXA", "cabos_por_fase": 1,
                "bitola_fase": 10, "bitola_neutro": 10, "bitola_terra": 10,
                "gd_ja_instalado": gd_ja_instalado,
                "mes_previsao_ligacao": MES_PT[data_prevista.month - 1],
                "ano_previsao_ligacao": data_prevista.year,
                "zona": zona,
                "paineis": paineis, "inversores": inversores,
                "necessita_autotrafo": "NÃO", "trafo_exclusivo": "NÃO",
            },
        }
        gerar_energisa(dados)

    mostrar_downloads("energisa_resultado")


def gerar_energisa(dados):
    nome_base = slug_arquivo(dados["cliente"]["titular"], dados["cliente"]["uc"])
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        xlsm_path = tmp / f"{nome_base}.xlsm"

        with st.spinner("Preenchendo planilha..."):
            preencher_energisa(dados, str(MODELO_ENERGISA), str(xlsm_path))

        with st.spinner("Recalculando fórmulas..."):
            resultado = recalc_xlsx(str(xlsm_path), 90)
        if resultado.get("total_errors", 0) > 1 or (
            resultado.get("total_errors") == 1
            and "CONFIG!Q27" not in str(resultado.get("error_summary", {}))
        ):
            st.warning(f"Atenção: erros encontrados na planilha: {resultado}")

        # Nomes das 5 abas que compõem o documento final, na ordem em que
        # aparecem, e o nome de arquivo amigável de cada uma quando baixada
        # separadamente.
        ABAS_FINAIS = ["SOLICITACAO", "RELACAO DE CARGA", "FORMULARIO", "MD-SOLAR", "DU-SOLAR"]
        NOME_ARQUIVO_ABA = {
            "SOLICITACAO": "Solicitacao",
            "RELACAO DE CARGA": "Levantamento_de_Carga",
            "FORMULARIO": "Formulario",
            "MD-SOLAR": "Memorial_Descritivo",
            "DU-SOLAR": "Diagrama_Unifilar",
        }

        from pypdf import PdfReader, PdfWriter

        def _converter_para_pdf_sem_paginas_em_branco(origem_xlsm: Path, abas: list) -> Path:
            """Copia o .xlsm preenchido, restringe a impressão às `abas`
            informadas SEM passar pelo openpyxl (que apaga as formas do
            diagrama unifilar), converte para PDF e remove páginas em branco."""
            sufixo = "-".join(a.replace(" ", "") for a in abas)
            copia = tmp / f"{origem_xlsm.stem}_{sufixo}_tmp.xlsm"
            shutil.copyfile(str(origem_xlsm), str(copia))
            restringir_impressao_para_pdf(str(copia), abas)
            run_soffice(["--headless", "--convert-to", "pdf", "--outdir", str(tmp), str(copia)])
            reader = PdfReader(str(copia.with_suffix(".pdf")))
            writer = PdfWriter()
            for page in reader.pages:
                if page.extract_text().strip():
                    writer.add_page(page)
            return writer

        with st.spinner("Gerando PDF..."):
            writer = _converter_para_pdf_sem_paginas_em_branco(xlsm_path, ABAS_FINAIS)
            pdf_final = tmp / f"{nome_base}.pdf"
            with open(pdf_final, "wb") as f:
                writer.write(f)

        with st.spinner("Gerando documentos separados..."):
            pdfs_separados = {}
            for aba in ABAS_FINAIS:
                writer_aba = _converter_para_pdf_sem_paginas_em_branco(xlsm_path, [aba])
                caminho_aba = tmp / f"{nome_base}_{NOME_ARQUIVO_ABA[aba]}.pdf"
                with open(caminho_aba, "wb") as f:
                    writer_aba.write(f)
                pdfs_separados[aba] = caminho_aba

        with st.spinner("Gerando arquivo para ANEEL..."):
            import openpyxl
            wb2 = openpyxl.load_workbook(str(xlsm_path), data_only=True)
            ws_src = wb2["SAIDA"]
            wb_out = openpyxl.Workbook()
            ws_out = wb_out.active
            ws_out.title = "SAIDA"
            for row in ws_src.iter_rows(min_row=1, max_row=2, max_col=ws_src.max_column):
                for cell in row:
                    if cell.value is not None:
                        ws_out.cell(row=cell.row, column=cell.column, value=cell.value)
            wb_out.create_sheet("Planilha1")
            aneel_path = tmp / f"{nome_base}_ANEEL.xlsx"
            wb_out.save(str(aneel_path))

        # Lê tudo para a memória AQUI DENTRO, antes da pasta temporária ser
        # apagada ao sair do "with" -- é isso que permite os downloads
        # sobreviverem a cliques subsequentes (ver salvar_resultado/
        # mostrar_downloads mais acima no arquivo).
        salvar_resultado(
            "energisa_resultado",
            principais=[
                ("Planilha (.xlsm)", xlsm_path.name, xlsm_path.read_bytes()),
                ("PDF final (todas as páginas)", pdf_final.name, pdf_final.read_bytes()),
                ("Arquivo ANEEL", aneel_path.name, aneel_path.read_bytes()),
            ],
            separados=[
                (NOME_ARQUIVO_ABA[aba].replace("_", " "), pdfs_separados[aba].name, pdfs_separados[aba].read_bytes())
                for aba in ABAS_FINAIS
            ],
            nome_zip=f"{nome_base}_documentos.zip",
        )


# ============================================================
# EQUATORIAL-GO
# ============================================================
def form_equatorial():
    st.header("Equatorial-GO")
    resp = responsavel_tecnico_form()
    st.caption("Registro Profissional (Anexo I): 436143 · Registro Memorial: 1217762256 (fixos)")

    with st.expander("📄 Já tem os dados prontos? Preencha a partir de um arquivo .txt"):
        st.caption(
            "Baixe o modelo, preencha os campos num editor de texto simples "
            "(Bloco de Notas, etc.) e envie de volta aqui — os documentos são "
            "gerados direto, sem precisar preencher o formulário abaixo. A foto "
            "de localização (opcional) continua sendo enviada separadamente logo abaixo."
        )
        st.download_button(
            "⬇️ Baixar modelo (.txt)", TEMPLATE_EQUATORIAL_TXT, "modelo_equatorial.txt",
            key="modelo_equatorial_txt",
        )
        arquivo_txt = st.file_uploader(
            "Enviar arquivo preenchido (.txt)", type=["txt"], key="upload_equatorial_txt",
        )
        foto_txt = st.file_uploader(
            "Foto de localização (opcional)", type=["png", "jpg", "jpeg"], key="upload_equatorial_foto_txt",
        )
        if arquivo_txt is not None:
            texto = arquivo_txt.getvalue().decode("utf-8", errors="replace")
            dados_txt = montar_dados_equatorial_de_txt(texto, resp)
            st.caption("Prévia dos dados lidos do arquivo:")
            st.json(dados_txt, expanded=False)
            if st.button("Gerar documentos a partir do arquivo", type="primary", key="gerar_equatorial_txt"):
                if not dados_txt["cliente"]["nome"] or not dados_txt["cliente"]["cpf_cnpj"]:
                    st.error("O arquivo precisa ter pelo menos os campos 'nome' e 'cpf_cnpj' preenchidos.")
                else:
                    gerar_equatorial(dados_txt, foto_txt)

    st.markdown("---")
    st.subheader("1. Identificação")
    c1, c2, c3 = st.columns(3)
    nome = c1.text_input("Nome completo")
    cpf_cnpj = c2.text_input("CPF/CNPJ")
    celular = c3.text_input("Celular")

    c1, c2 = st.columns(2)
    endereco = c1.text_input("Endereço (rua, número, complemento)")
    email = c2.text_input("E-mail")

    c1, c2, c3, c4 = st.columns(4)
    cep = c1.text_input("CEP", "76240000")
    municipio = c2.text_input("Município", "Aragarças")
    uf = c3.text_input("UF", "GO")
    bairro = c4.text_input("Bairro")

    uc_existente = st.text_input("UC (se já existir)")

    st.subheader("2. Características técnicas")
    c1, c2, c3 = st.columns(3)
    tipo_ligacao = c1.selectbox("Tipo de Ligação", ["MONOFÁSICO", "BIFÁSICO", "TRIFÁSICO"])
    tensao_atendimento_v = c2.number_input("Tensão de Atendimento (V)", value=220)
    classe = c3.selectbox("Classe", ["Residencial", "Comercial", "Industrial", "Rural"])

    c1, c2, c3 = st.columns(3)
    disjuntor_entrada_a = c1.number_input("Disjuntor de Entrada (A)", value=50)
    carga_declarada_kw = c2.number_input("Carga Declarada (kW)", value=10.0)
    potencia_disponibilizada_kw = c3.number_input("Potência Disponibilizada — PD (kW)", value=10.0)

    tipo_ramal = st.selectbox("Tipo de Ramal", ["AÉREO", "SUBTERRÂNEO"])

    st.subheader("3. Coordenadas e modalidade")
    c1, c2, c3 = st.columns(3)
    fuso = c1.text_input("Fuso (ex: 22L)")
    coord_x = c2.text_input("X")
    coord_y = c3.text_input("Y")

    c1, c2 = st.columns(2)
    modalidade_compensacao = c1.selectbox("Modalidade de Compensação",
                                           ["AUTOCONSUMO LOCAL", "AUTOCONSUMO REMOTO"])
    data_inicio = c2.date_input("Data de início de operação", value=date.today() + timedelta(days=15))

    st.subheader("4. Módulos")
    modulos = tabela_equipamentos(
        "Um item por modelo de módulo diferente",
        None,
        [{"potencia_w": 0, "quantidade": 0, "fabricante": "", "modelo": ""}],
        "modulos_equatorial",
    )

    st.subheader("5. Inversor(es)")
    st.caption("Busque o datasheet real do fabricante para corrente/rendimento — não estime.")
    inversores = tabela_equipamentos(
        "Um item por modelo de inversor diferente",
        None,
        [{"fabricante": "", "modelo": "", "potencia_nominal_kw": 0.0, "faixa_tensao_v": 220,
          "corrente_nominal_a": 0.0, "fator_potencia": 1.0, "rendimento_pct": 98.0, "dht_corrente_pct": 3.0}],
        "inversores_equatorial",
    )

    st.subheader("6. Foto de localização (opcional)")
    foto = st.file_uploader("Print do mapa / foto de satélite com a marcação do imóvel", type=["png", "jpg", "jpeg"])

    gerar = st.button("Gerar documentos (Equatorial-GO)", type="primary")

    if gerar:
        if not nome or not cpf_cnpj:
            st.error("Preencha ao menos Nome e CPF/CNPJ.")
            return

        dados = {
            "responsavel_tecnico": {
                "nome": resp["nome"], "titulo_profissional": "Engenheiro de Energia",
                "registro_profissional": 436143, "uf_registro": "MT",
                "email": resp["email"], "telefone": resp["telefone"].replace(" ", "").replace("(", "").replace(")", "").replace("-", ""),
            },
            "premissas_fixas": {
                "tarifa_branca": "NÃO", "vistoria_apos_solicitacao": "NÃO",
                "autoriza_entrega_contratos": "SIM", "declara_conformidade_normas": "SIM",
                "grid_zero": "NÃO", "gratuidade_ren": "NÃO",
                "autoriza_faturas_email": "NÃO", "declara_veracidade": "SIM",
            },
            "_referencia_hakknner": REFERENCIA_HAKKNNER,
            "cliente": {
                "nome": nome, "cpf_cnpj": cpf_cnpj, "celular": celular, "telefone_fixo": "",
                "endereco": endereco, "email": email, "cep": cep, "municipio": municipio,
                "uf": uf, "uf_extenso": "Goiás" if uf == "GO" else uf,
                "receber_fatura_email": "NÃO",
                "tipo_orcamento": "Orçamento de Conexão", "uc_existente": uc_existente,
                "tipo_solicitacao": "CONEXÃO DE GD EM UNIDADE CONSUMIDORA EXISTENTE SEM AUMENTO DE POTÊNCIA DISPONIBILIZADA (ver item abaixo)",
                "cargas_especiais": "NÃO", "ramo_atividade": classe, "classe": classe,
                "tipo_ligacao": tipo_ligacao, "tensao_atendimento_v": tensao_atendimento_v,
                "carga_declarada_kw": carga_declarada_kw, "disjuntor_entrada_a": disjuntor_entrada_a,
                "potencia_disponibilizada_kw": potencia_disponibilizada_kw, "tipo_ramal": tipo_ramal,
                "fuso": fuso, "coord_x": coord_x, "coord_y": coord_y,
                "modalidade_compensacao": modalidade_compensacao,
                "data_inicio_operacao": data_inicio.isoformat(),
                "conta_contrato": uc_existente, "bairro": bairro,
                "mes_ano_documento": f"{MES_PT[data_inicio.month-1].capitalize()} – {data_inicio.year}",
                "telefone_cliente": celular,
                "data_conclusao_geradora": data_inicio.strftime("%d/%m/%Y"),
                "endereco_completo_comissionamento": f"{endereco.upper()}\n{bairro.upper()}",
                "modulos": modulos, "inversores": inversores,
            },
        }
        gerar_equatorial(dados, foto)

    mostrar_downloads("equatorial_resultado")


def gerar_equatorial(dados, foto_upload):
    nome_base = slug_arquivo(dados["cliente"]["nome"], dados["cliente"]["uc_existente"] or "SEMUC")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        with st.spinner("Preenchendo Anexo I..."):
            xlsx_path = tmp / f"{nome_base}_AnexoI.xlsx"
            avisos = preencher_anexo1(dados, str(MODELO_ANEXO1), str(xlsx_path))
            resultado = recalc_xlsx(str(xlsx_path), 90)

        with st.spinner("Gerando Memorial Descritivo..."):
            doc = Document(str(MODELO_MEMORIAL))
            replace_everywhere(doc, montar_substituicoes_memorial(dados))
            qtd = sum(m["quantidade"] for m in dados["cliente"]["modulos"])
            pot = sum(m["potencia_w"] * m["quantidade"] for m in dados["cliente"]["modulos"]) / 1000
            fix_tabela_gerador(doc, qtd, pot)
            if foto_upload is not None:
                foto_path = tmp / "foto_local.png"
                foto_path.write_bytes(foto_upload.getvalue())
                substituir_foto_localizacao(doc, str(foto_path))
            memorial_path = tmp / f"{nome_base}_Memorial.docx"
            doc.save(str(memorial_path))

        with st.spinner("Gerando Relatório de Comissionamento..."):
            doc2 = Document(str(MODELO_COMISSIONAMENTO))
            replace_everywhere(doc2, montar_substituicoes_comissionamento(dados))
            comiss_path = tmp / f"{nome_base}_Comissionamento.docx"
            doc2.save(str(comiss_path))

        with st.spinner("Convertendo para PDF..."):
            run_soffice(["--headless", "--convert-to", "pdf", "--outdir", str(tmp), str(xlsx_path)])
            run_soffice(["--headless", "--convert-to", "pdf", "--outdir", str(tmp), str(memorial_path)])
            run_soffice(["--headless", "--convert-to", "pdf", "--outdir", str(tmp), str(comiss_path)])

            from pypdf import PdfReader, PdfWriter
            anexo_pdf_full = xlsx_path.with_suffix(".pdf")
            reader = PdfReader(str(anexo_pdf_full))
            writer = PdfWriter()
            for i in [1, 2, 3, 4]:
                if i < len(reader.pages):
                    writer.add_page(reader.pages[i])
            anexo_pdf = tmp / f"{nome_base}_AnexoI.pdf"
            with open(anexo_pdf, "wb") as f:
                writer.write(f)

        salvar_resultado(
            "equatorial_resultado",
            principais=[
                ("Anexo I (.xlsx)", xlsx_path.name, xlsx_path.read_bytes()),
                ("Anexo I (.pdf)", anexo_pdf.name, anexo_pdf.read_bytes()),
                ("Memorial (.docx)", memorial_path.name, memorial_path.read_bytes()),
                ("Memorial (.pdf)", memorial_path.with_suffix(".pdf").name,
                 memorial_path.with_suffix(".pdf").read_bytes()),
                ("Comissionamento (.docx)", comiss_path.name, comiss_path.read_bytes()),
                ("Comissionamento (.pdf)", comiss_path.with_suffix(".pdf").name,
                 comiss_path.with_suffix(".pdf").read_bytes()),
            ],
            nome_zip=f"{nome_base}_documentos.zip",
            avisos=avisos,
            aviso_final="Lembrete: as seções de dimensionamento do Memorial (disjuntor, DPS, "
                        "aterramento, cabos, levantamento de carga) usam os valores de referência — revise manualmente.",
        )


# ============================================================
# MAIN
# ============================================================
st.title("☀️ Gerador de Documentos GD — Exergia")
concessionaria = st.sidebar.radio("Concessionária", ["Energisa-MT", "Equatorial-GO"])
st.sidebar.markdown("---")
st.sidebar.caption("Preenchimento automático de projetos de Geração Distribuída, "
                    "com as regras e validações já testadas no fluxo de produção da Exergia.")

if concessionaria == "Energisa-MT":
    form_energisa()
else:
    form_equatorial()
