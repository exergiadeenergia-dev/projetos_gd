"""
Integração com o Google Drive — leitura automática da pasta do cliente.

Este módulo só LÊ arquivos (nunca cria, edita ou apaga nada no Drive) e só
enxerga as pastas que forem explicitamente compartilhadas com a conta de
serviço configurada em `st.secrets["gcp_service_account"]`. Nada acontece
sem uma ação explícita do usuário na tela (buscar pasta → escolher pasta →
"fazer levantamento automático").

Configuração necessária (uma vez só, feita pelo Alex — veja o guia enviado
junto com este arquivo):
  1. Criar um projeto no Google Cloud e ativar a "Google Drive API".
  2. Criar uma conta de serviço, gerar uma chave JSON.
  3. Colar o conteúdo do JSON em Settings → Secrets do Streamlit Cloud,
     sob a chave [gcp_service_account].
  4. Compartilhar (somente leitura) a pasta-mãe dos clientes no Drive com o
     e-mail da conta de serviço (algo como
     nome@projeto.iam.gserviceaccount.com).

Se o secret não estiver configurado, `servico_disponivel()` devolve False e
a tela mostra o aviso de configuração em vez de quebrar.
"""
from __future__ import annotations

import io
import math
import re
from dataclasses import dataclass, field

# ------------------------------------------------------------------
# Tabelas oficiais do INMETRO com todos os módulos e inversores
# certificados (fabricante, modelo e potência exatos) — fonte de verdade
# para achar o modelo certo quando só se tem a potência do equipamento.
# Links conferidos em 02/09/2026 direto no site do INMETRO; nunca
# escolhemos um modelo automaticamente — é sempre o engenheiro quem
# confirma o modelo exato aqui antes de gerar os documentos.
# ------------------------------------------------------------------
INMETRO_MODULOS_URL = (
    "https://www.gov.br/inmetro/pt-br/assuntos/regulamentacao/avaliacao-da-conformidade/"
    "programa-brasileiro-de-etiquetagem/tabelas-de-eficiencia-energetica/"
    "sistema-de-energia-fotovoltaica/componente-fotovoltaico-2013-modulo/view"
)
INMETRO_INVERSORES_ONGRID_URL = (
    "https://www.gov.br/inmetro/pt-br/assuntos/regulamentacao/avaliacao-da-conformidade/"
    "programa-brasileiro-de-etiquetagem/tabelas-de-eficiencia-energetica/"
    "sistema-de-energia-fotovoltaica/componente-fotovoltaico-inversores-on-grid/view"
)
INMETRO_HUB_URL = (
    "https://www.gov.br/inmetro/pt-br/assuntos/avaliacao-da-conformidade/"
    "programa-brasileiro-de-etiquetagem/tabelas-de-eficiencia-energetica/"
    "sistema-de-energia-fotovoltaica"
)

# ------------------------------------------------------------------
# Conversão de coordenadas (DMS -> decimal -> UTM), validada contra
# pyproj (EPSG:32722) e contra dados reais de uma ART do CREA-MT nesta
# mesma conversa. Sem dependência externa de geodésia.
# ------------------------------------------------------------------

def dms_para_decimal(graus, minutos, segundos, hemisferio) -> float:
    dd = graus + minutos / 60 + segundos / 3600
    if hemisferio.upper() in ("S", "O", "W"):
        dd = -dd
    return dd


def decimal_para_utm(lat: float, lon: float):
    """Transversa de Mercator (fórmulas de Snyder), WGS84. Devolve
    (zona, banda_mgrs, x_inteiro, y_inteiro)."""
    a = 6378137.0
    f = 1 / 298.257223563
    e2 = f * (2 - f)
    ep2 = e2 / (1 - e2)
    k0 = 0.9996
    zona = int((lon + 180) / 6) + 1
    lon0 = math.radians(-183 + 6 * zona)
    phi = math.radians(lat)
    lam = math.radians(lon)
    N = a / math.sqrt(1 - e2 * math.sin(phi) ** 2)
    T = math.tan(phi) ** 2
    C = ep2 * math.cos(phi) ** 2
    A = math.cos(phi) * (lam - lon0)
    M = a * (
        (1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256) * phi
        - (3 * e2 / 8 + 3 * e2 ** 2 / 32 + 45 * e2 ** 3 / 1024) * math.sin(2 * phi)
        + (15 * e2 ** 2 / 256 + 45 * e2 ** 3 / 1024) * math.sin(4 * phi)
        - (35 * e2 ** 3 / 3072) * math.sin(6 * phi)
    )
    x = k0 * N * (A + (1 - T + C) * A ** 3 / 6
                  + (5 - 18 * T + T ** 2 + 72 * C - 58 * ep2) * A ** 5 / 120) + 500000
    y = k0 * (M + N * math.tan(phi) * (A ** 2 / 2 + (5 - T + 9 * C + 4 * C ** 2) * A ** 4 / 24
              + (61 - 58 * T + T ** 2 + 600 * C - 330 * ep2) * A ** 6 / 720))
    if lat < 0:
        y += 10000000
    bandas = "CDEFGHJKLMNPQRSTUVWX"
    indice_banda = int((lat + 80) / 8)
    indice_banda = max(0, min(indice_banda, len(bandas) - 1))
    banda = bandas[indice_banda]
    return zona, banda, round(x), round(y)


COORD_DMS_RE = re.compile(r"(\d{1,3})\s*[ºo°]\s*(\d{1,2})\s*'\s*([\d.,]+)\s*''?\s*([NSEOW])")


def _normalizar(texto: str) -> str:
    return re.sub(r"\s+", " ", texto)


def extrair_coordenadas(texto: str):
    """Procura duas coordenadas em graus/min/seg (lat e lon, em qualquer
    ordem) e devolve ((zona_banda, x, y), achados_debug) ou (None, achados)."""
    achados = COORD_DMS_RE.findall(texto)
    if len(achados) < 2:
        return None, achados
    (g1, m1, s1, h1), (g2, m2, s2, h2) = achados[0], achados[1]
    try:
        lat = dms_para_decimal(int(g1), int(m1), float(s1.replace(",", ".")), h1)
        lon = dms_para_decimal(int(g2), int(m2), float(s2.replace(",", ".")), h2)
    except ValueError:
        return None, achados
    if abs(lat) > 90:
        lat, lon = lon, lat
    zona, banda, x, y = decimal_para_utm(lat, lon)
    return (f"{zona}{banda}", x, y), achados


# ------------------------------------------------------------------
# Extração de campos a partir do texto de um único arquivo. Cada função
# devolve só os campos que conseguiu reconhecer (nunca inventa valor) —
# quem chama decide o que fazer com campos que ninguém encontrou.
# ------------------------------------------------------------------

def extrair_dados_art(texto: str) -> dict:
    """ART do CREA (qualquer estado) — título/contratante, CPF, endereço,
    potência e coordenadas."""
    dados = {}
    t = _normalizar(texto)
    m = re.search(r"Contratante:\s*([A-ZÀ-Ú \.]+?)\s*CPF/CNPJ", t)
    if m:
        dados["titular"] = m.group(1).strip().title()
    m = re.search(r"CPF/CNPJ:\s*([\d\.\-/]+)", t)
    if m:
        dados["cpf_cnpj"] = re.sub(r"\D", "", m.group(1))
    m = re.search(r"Rua:\s*(.+?)\s*Complemento:", t)
    if m:
        dados["logradouro"] = m.group(1).strip().title()
    m = re.search(r"Cidade:\s*([A-ZÀ-Ú]+)\s*UF:\s*([A-Z]{2})", t)
    if m:
        dados["cidade"] = m.group(1).strip().title()
        dados["uf"] = m.group(2).strip()
    m = re.search(r"CEP:\s*([\d\.\-]+)", t)
    if m:
        dados["cep"] = re.sub(r"\D", "", m.group(1))
    m = re.search(r"([\d,]+)\s*quilowatt", t)
    if m:
        try:
            dados["potencia_instalada_kw"] = float(m.group(1).replace(",", "."))
        except ValueError:
            pass
    coord, achados = extrair_coordenadas(t)
    if coord:
        dados["fuso"], dados["coord_x"], dados["coord_y"] = coord
    return dados


def extrair_dados_conta_energisa(texto: str) -> dict:
    dados = {}
    tu = texto.upper()
    if "MONOFASICO" in tu or "MONOFÁSICO" in tu:
        dados["tipo_conexao"] = "MONOFÁSICO"
    elif "BIFASICO" in tu or "BIFÁSICO" in tu:
        dados["tipo_conexao"] = "BIFÁSICO"
    elif "TRIFASICO" in tu or "TRIFÁSICO" in tu:
        dados["tipo_conexao"] = "TRIFÁSICO"
    m = re.search(r"Lim\.\s*Min\.\s*[:\.]?\s*(\d+)\s*Lim\.\s*Max\.\s*[:\.]?\s*(\d+)", texto)
    if m:
        minimo, maximo = int(m.group(1)), int(m.group(2))
        media = (minimo + maximo) / 2
        dados["tensao_atendimento_v"] = "127" if abs(media - 127) < abs(media - 220) else "220"
    if "RURAL" in tu:
        dados["zona"] = "RURAL"
    elif "URBANO" in tu:
        dados["zona"] = "URBANO"
    m = re.search(r"\b(\d{8,13})\b\s*-?\s*(?:UC|UNIDADE CONSUMIDORA)", tu)
    if m:
        dados["uc"] = m.group(1)
    return dados


def extrair_dados_genericos(texto: str) -> dict:
    """Regras que não dependem do tipo de documento — CPF/CNPJ e CEP soltos
    no texto, usados como reforço quando a ART não é encontrada (comum em
    projetos Equatorial-GO, cujo formato de documentos ainda não foi
    validado com dados reais nesta ferramenta)."""
    dados = {}
    m = re.search(r"CPF[\s/]*CNPJ\s*[:\-]?\s*([\d\.\-/]{11,18})", texto, re.IGNORECASE)
    if m:
        digitos = re.sub(r"\D", "", m.group(1))
        if len(digitos) in (11, 14):
            dados["cpf_cnpj"] = digitos
    m = re.search(r"CEP\s*[:\-]?\s*(\d{2}\.?\d{3}-?\d{3})", texto, re.IGNORECASE)
    if m:
        dados["cep"] = re.sub(r"\D", "", m.group(1))
    return dados


CAMPOS_TIPO = {
    "energisa": (extrair_dados_art, extrair_dados_conta_energisa, extrair_dados_genericos),
    "equatorial": (extrair_dados_art, extrair_dados_genericos),
}


def extrair_campos_arquivo(texto: str, tipo: str) -> dict:
    """Roda todos os extratores relevantes pro tipo de projeto sobre o
    texto de UM arquivo e devolve um dict só com o que foi reconhecido."""
    resultado = {}
    for extrator in CAMPOS_TIPO.get(tipo, ()):
        try:
            achado = extrator(texto)
        except Exception:
            achado = {}
        for k, v in achado.items():
            if v not in (None, "") and k not in resultado:
                resultado[k] = v
    return resultado


# ------------------------------------------------------------------
# Acesso ao Google Drive (conta de serviço, somente leitura).
# ------------------------------------------------------------------

ESCOPO_LEITURA = ["https://www.googleapis.com/auth/drive.readonly"]

MIME_PASTA = "application/vnd.google-apps.folder"
MIME_TEXTO_EXTRAIVEL = {
    "application/pdf",
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

MAX_ARQUIVOS = 25
MAX_PROFUNDIDADE = 3
MAX_CARACTERES_POR_ARQUIVO = 60_000


def servico_disponivel() -> bool:
    try:
        import streamlit as st
        return "gcp_service_account" in st.secrets
    except Exception:
        return False


def _get_service():
    import streamlit as st
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    info = dict(st.secrets["gcp_service_account"])
    credenciais = service_account.Credentials.from_service_account_info(info, scopes=ESCOPO_LEITURA)
    return build("drive", "v3", credentials=credenciais, cache_discovery=False)


def extrair_id_pasta(texto: str) -> str:
    """Aceita um link completo do Drive ou já o ID puro."""
    texto = texto.strip()
    m = re.search(r"/folders/([a-zA-Z0-9_-]{10,})", texto)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([a-zA-Z0-9_-]{10,})", texto)
    if m:
        return m.group(1)
    return texto


def buscar_pastas(servico, nome: str, pasta_pai_id: str | None = None, limite: int = 15):
    """Busca pastas cujo nome contém `nome`. Só enxerga o que foi
    compartilhado com a conta de serviço."""
    termo = nome.replace("'", "\\'")
    partes = [f"mimeType = '{MIME_PASTA}'", "trashed = false", f"name contains '{termo}'"]
    if pasta_pai_id:
        partes.append(f"'{pasta_pai_id}' in parents")
    query = " and ".join(partes)
    resp = servico.files().list(
        q=query,
        fields="files(id, name, parents)",
        pageSize=limite,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    return resp.get("files", [])


def listar_arquivos_pasta(servico, pasta_id: str, _profundidade: int = 0, _total: list | None = None):
    """Lista (recursivamente, até MAX_PROFUNDIDADE) os arquivos dentro de
    uma pasta e suas subpastas. Devolve uma lista de dicts
    {id, name, mimeType, path}, limitada a MAX_ARQUIVOS."""
    if _total is None:
        _total = []
    if _profundidade > MAX_PROFUNDIDADE or len(_total) >= MAX_ARQUIVOS:
        return _total
    resp = servico.files().list(
        q=f"'{pasta_id}' in parents and trashed = false",
        fields="files(id, name, mimeType)",
        pageSize=100,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    for item in resp.get("files", []):
        if len(_total) >= MAX_ARQUIVOS:
            break
        if item["mimeType"] == MIME_PASTA:
            listar_arquivos_pasta(servico, item["id"], _profundidade + 1, _total)
        else:
            _total.append(item)
    return _total


def baixar_texto_arquivo(servico, arquivo: dict) -> str:
    """Baixa/exporta um arquivo e devolve seu texto. Devolve string vazia
    para tipos que não sabemos extrair texto (imagens, etc.)."""
    mime = arquivo["mimeType"]
    file_id = arquivo["id"]
    try:
        if mime == "application/vnd.google-apps.document":
            dados = servico.files().export(fileId=file_id, mimeType="text/plain").execute()
            return dados.decode("utf-8", errors="ignore") if isinstance(dados, bytes) else str(dados)

        if mime == "application/vnd.google-apps.spreadsheet":
            dados = servico.files().export(fileId=file_id, mimeType="text/csv").execute()
            return dados.decode("utf-8", errors="ignore") if isinstance(dados, bytes) else str(dados)

        if mime not in MIME_TEXTO_EXTRAIVEL:
            return ""

        conteudo = servico.files().get_media(fileId=file_id).execute()

        if mime == "application/pdf":
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(conteudo))
            partes = []
            for pagina in reader.pages:
                partes.append(pagina.extract_text() or "")
            return "\n".join(partes)

        if mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            from docx import Document
            doc = Document(io.BytesIO(conteudo))
            return "\n".join(p.text for p in doc.paragraphs)

        if mime == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(conteudo), data_only=True)
            partes = []
            for aba in wb.worksheets:
                for linha in aba.iter_rows(values_only=True):
                    partes.append(" ".join(str(v) for v in linha if v is not None))
            return "\n".join(partes)

    except Exception as exc:  # nunca deixa 1 arquivo problemático travar o levantamento inteiro
        return f"[erro ao ler {arquivo.get('name')}: {exc}]"

    return ""


@dataclass
class ResultadoLevantamento:
    campos: dict = field(default_factory=dict)
    fontes: dict = field(default_factory=dict)  # campo -> nome do arquivo onde foi achado
    arquivos_lidos: list = field(default_factory=list)
    arquivos_ignorados: list = field(default_factory=list)
    erros: list = field(default_factory=list)


def levantamento_pasta(pasta_id: str, tipo: str) -> ResultadoLevantamento:
    """Função principal: lista os arquivos da pasta do cliente, extrai
    texto de cada um e reconhece os campos possíveis. NUNCA sobrescreve um
    campo já encontrado em um arquivo anterior — o primeiro arquivo em que
    um dado aparece "ganha", e fica registrado em `fontes` para o usuário
    conferir de onde veio cada valor antes de confiar nele."""
    servico = _get_service()
    resultado = ResultadoLevantamento()

    arquivos = listar_arquivos_pasta(servico, pasta_id)
    for arquivo in arquivos:
        nome = arquivo["name"]
        if arquivo["mimeType"] not in MIME_TEXTO_EXTRAIVEL:
            resultado.arquivos_ignorados.append(nome)
            continue

        texto = baixar_texto_arquivo(servico, arquivo)
        if texto.startswith("[erro ao ler"):
            resultado.erros.append(texto)
            continue
        if not texto.strip():
            resultado.arquivos_ignorados.append(nome)
            continue

        resultado.arquivos_lidos.append(nome)
        texto = texto[:MAX_CARACTERES_POR_ARQUIVO]
        achados = extrair_campos_arquivo(texto, tipo)
        for campo, valor in achados.items():
            if campo not in resultado.campos:
                resultado.campos[campo] = valor
                resultado.fontes[campo] = nome

    return resultado
