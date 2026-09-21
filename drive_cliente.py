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


COORD_DMS_RE = re.compile(
    r"(\d{1,3})\s*[ºo°]\s*(\d{1,2})\s*['′]\s*([\d.,]+)\s*(?:''|\"|″)?\s*([NSEOW])"
)


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
    potência e coordenadas.

    Testado contra duas ARTs reais de estados diferentes nesta conversa, e
    os dois formatos são BEM diferentes um do outro:
      - CREA-MT: nome do contratante em CAIXA ALTA, rótulo "Rua:" separado
        pro logradouro, "Cidade: X UF: Y" com rótulo de UF à parte, e a
        ORDEM em que os rótulos aparecem no texto extraído do PDF (que
        segue a ordem interna do PDF, não necessariamente a ordem visual
        da página) intercala campos de um jeito que uma extração "até o
        próximo rótulo tal" pode andar longe demais e pegar um pedaço de
        campo errado no meio do caminho.
      - CREA-GO: nome do contratante em Caixa Alta E Baixa (Title Case),
        sem nenhum rótulo "Rua:" (o logradouro vem solto, logo depois do
        CPF/CNPJ do contratante), bairro com rótulo "Bairro:", e cidade
        e UF juntos num só campo ("Cidade: ARAGARÇAS-GO", sem "UF:"
        separado), além da potência vindo em CAIXA ALTA e no plural
        ("QUILOWATTS").

    Por isso todo campo de texto livre aqui (contratante, logradouro,
    bairro) usa um "lookahead" com a lista de próximos rótulos conhecidos
    (nunca um único rótulo fixo como "Complemento:") — pára no PRIMEIRO
    desses rótulos que aparecer, então nunca "atravessa" um campo inteiro
    não relacionado só porque o rótulo esperado ficou longe demais no
    texto extraído do PDF. Se nenhum rótulo conhecido aparecer depois, o
    campo simplesmente não é preenchido — nunca inventa um corte no meio
    do texto."""
    dados = {}
    t = _normalizar(texto)

    # Nome do contratante — aceita CAIXA ALTA (CREA-MT) ou Title Case
    # (CREA-GO); pára no primeiro rótulo conhecido que vier depois, nunca
    # tenta adivinhar um corte se nenhum desses aparecer.
    m = re.search(
        r"Contratante:\s*([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\s\.]*?)\s*"
        r"(?=CPF/CNPJ|Data\s+de\s+In[íi]cio|C[óo]digo:|Proprietário|\d\.\s)",
        t,
    )
    if m:
        dados["titular"] = m.group(1).strip().title()
    m = re.search(r"CPF/CNPJ:\s*([\d\.\-/]+)", t)
    if m:
        dados["cpf_cnpj"] = re.sub(r"\D", "", m.group(1))

    # Logradouro — duas variantes, testadas contra as duas ARTs reais:
    # 1) rótulo "Rua:" explícito (CREA-MT) — pára no primeiro rótulo de
    #    endereço conhecido que vier depois (nunca no "Complemento:" mais
    #    distante do documento, que foi o bug original).
    m = re.search(
        r"Rua:\s*([A-Za-zÀ-ÿ0-9°º\s\.\-]+?)\s*"
        r"(?=Cidade:|Bairro:|Complemento:|N[úu]mero:)",
        t,
    )
    if m:
        dados["logradouro"] = m.group(1).strip().title()
    else:
        # 2) sem rótulo "Rua:" (CREA-GO) — o logradouro vem solto, logo
        #    depois do CPF/CNPJ do contratante, terminando em ", Nº ...
        #    Bairro:".
        m = re.search(
            r"CPF/CNPJ:\s*[\d\.\-/]+\s*([A-Za-zÀ-ÿ0-9°º\s\.]+?)\s*,\s*"
            r"N[ºo°]\s*\S+\s*Bairro:",
            t,
        )
        if m:
            dados["logradouro"] = m.group(1).strip().title()

    # Bairro — mesma lógica do logradouro: pára no primeiro rótulo
    # conhecido, nunca faz a captura "andar" até um "CEP:"/"Rua:"/"Cidade:"
    # distante demais.
    m = re.search(
        r"Bairro:\s*([A-Za-zÀ-ÿ0-9°º\s\.\-]+?)\s*"
        r"(?=CEP:|Rua:|Cidade:|N[úu]mero:|Complemento:)",
        t,
    )
    if m:
        dados["bairro"] = m.group(1).strip().title()

    # Cidade/UF — "Cidade: X UF: Y" (CREA-MT, cidade pode ter espaço, ex.:
    # "BARRA DO GARÇAS") ou "Cidade: X-UF" junto, sem rótulo "UF:" à parte
    # (CREA-GO, ex.: "ARAGARÇAS-GO").
    m = re.search(r"Cidade:\s*([A-ZÀ-Ú][A-ZÀ-Ú\s]*?)\s*UF:\s*([A-Z]{2})\b", t)
    if m:
        dados["cidade"] = m.group(1).strip().title()
        dados["uf"] = m.group(2).strip()
    else:
        m = re.search(r"Cidade:\s*([A-ZÀ-Ú][A-ZÀ-Ú\s]*?)-([A-Z]{2})\b", t)
        if m:
            dados["cidade"] = m.group(1).strip().title()
            dados["uf"] = m.group(2).strip()

    m = re.search(r"CEP:\s*([\d\.\-]+)", t)
    if m:
        dados["cep"] = re.sub(r"\D", "", m.group(1))
    # "quilowatt(s)" — CREA-MT usa minúsculo/singular, CREA-GO usa
    # "QUILOWATTS" maiúsculo/plural; aceita os dois.
    m = re.search(r"([\d,\.]+)\s*quilowatts?", t, re.IGNORECASE)
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
    # Aceita tanto "CPF/CNPJ" quanto "CNPJ/CPF" (já vimos os dois rótulos
    # em documentos reais) — mas pula qualquer ocorrência logo depois de
    # "Autor do Projeto", que em Projetos Executivos/Diagramas Unifilares
    # marca o CPF do ENGENHEIRO responsável, não do cliente. Pegar esse
    # CPF por engano seria pior que não achar nenhum: pareceria conferido
    # sem ser.
    for m in re.finditer(r"C(?:PF[\s/]*CNPJ|NPJ[\s/]*CPF)\s*[:\-]?\s*([\d\.\-/,]{11,18})", texto, re.IGNORECASE):
        janela_antes = texto[max(0, m.start() - 200): m.start()]
        if re.search(r"autor\s+do\s+projeto", janela_antes, re.IGNORECASE):
            continue
        digitos = re.sub(r"\D", "", m.group(1))
        if len(digitos) in (11, 14):
            dados["cpf_cnpj"] = digitos
            break
    # CEP solto — mas pulando qualquer ocorrência logo depois da palavra
    # "Integrador", que em pedidos de kit (ex.: Solfácil) marca o endereço
    # da PRÓPRIA integradora/revenda, não o do cliente. Pegar esse CEP por
    # engano seria pior que não achar nenhum: pareceria conferido sem ser.
    for m in re.finditer(r"CEP\s*[:\-]?\s*(\d{2}\.?\d{3}-?\d{3})", texto, re.IGNORECASE):
        janela_antes = texto[max(0, m.start() - 200): m.start()]
        if re.search(r"integrador", janela_antes, re.IGNORECASE):
            continue
        dados["cep"] = re.sub(r"\D", "", m.group(1))
        break
    return dados


def extrair_endereco_livre(texto: str) -> dict:
    """Reconhece uma linha de endereço "solta" (sem rótulo por pedaço),
    no formato visto nos "espelhos" de conta:
        LOGRADOURO, NUMERO - BAIRRO - [CIDADE] CEP
    (o pedaço da cidade é opcional — às vezes não aparece, só o CEP direto
    depois do segundo traço). Só preenche o que consegue separar com
    segurança pelas vírgulas/traços; nunca tenta adivinhar onde um pedaço
    termina e o outro começa se a pontuação não deixar claro."""
    dados = {}
    m = re.search(
        r"\b([A-ZÀ-Ú][A-ZÀ-Ú0-9º°.\s]*?),\s*([A-Z0-9/]+)\s*-\s*([A-ZÀ-Ú0-9º°.\s]+?)\s*-\s*\.?\s*"
        r"([A-ZÀ-Ú][A-ZÀ-Ú\s]*?)?\s*(\d{5}-?\d{3}|\d{8})\b",
        texto.upper(),
    )
    if not m:
        return dados
    logradouro, numero, bairro, cidade, cep = m.groups()
    dados["logradouro"] = logradouro.strip().title()
    dados["numero"] = numero.strip()
    dados["bairro"] = bairro.strip().title()
    if cidade and cidade.strip():
        dados["cidade"] = cidade.strip().title()
    dados["cep"] = re.sub(r"\D", "", cep)
    return dados


def extrair_endereco_bloco_cep(texto: str) -> dict:
    """Outro layout de 'espelho de conta' — diferente do `extrair_endereco_livre`
    acima. Aqui o logradouro e o número vêm juntos numa linha, às vezes
    seguidos de um código interno da concessionária (matrícula, código de
    leitura etc.) que a gente NÃO tenta identificar — como não dá pra saber
    com certeza o que aquele número representa sem confirmação, ele fica de
    fora de qualquer campo, seguindo a regra de nunca adivinhar. O bairro
    fecha essa linha depois de um traço. Numa linha seguinte vem "CEP" com
    rótulo explícito, o número do CEP e a cidade depois de outro traço.
    Exemplo real (anonimizado, conferido nesta conversa):
        RUA CARAJAS 286 1111805172000 - CENTRO
        CEP 78600000 - BARRA DO GARCAS
    """
    dados = {}
    tu = texto.upper()
    # A linha de logradouro/número/bairro (primeiro regex abaixo) é frouxa
    # o bastante pra combinar com texto que não tem nada a ver com endereço
    # (ex.: legendas de um desenho CAD tipo "PADRÃO DE ENTRADA...32 A -
    # QUADRO DE PROTEÇÃO") — por isso só aceita esse achado quando o
    # documento TAMBÉM tem a linha de "CEP ... - CIDADE" no formato exato
    # deste layout; sem ela, não é seguro assumir que esse é mesmo um
    # "espelho de conta" nesse formato.
    m_cep = re.search(
        r"\bCEP\s*[:\-]?\s*(\d{5}-?\d{3}|\d{8})\s*-\s*([A-ZÀ-Ú][A-ZÀ-Ú\s]*?)\s*(?:\n|$)",
        tu,
    )
    if not m_cep:
        return dados
    dados["cep"] = re.sub(r"\D", "", m_cep.group(1))
    dados["cidade"] = m_cep.group(2).strip().title()
    m = re.search(
        r"\b([A-ZÀ-Ú][A-ZÀ-Ú\s\.]*?)\s+(\d+)(?:\s+\d{4,})?\s*-\s*"
        r"([A-ZÀ-Ú][A-ZÀ-Ú0-9º°.\s]*?)\s*(?:\n|$)",
        tu,
    )
    if m:
        dados["logradouro"] = m.group(1).strip().title()
        dados["numero"] = m.group(2).strip()
        dados["bairro"] = m.group(3).strip().title()
    return dados


def extrair_contato_livre(texto: str) -> dict:
    """Celular e e-mail soltos no texto, sem rótulo — como aparecem em
    alguns modelos de conta/espelho, e também num documento "cartão de
    contato" curto (coordenadas + e-mail + celular, cada um em seu próprio
    parágrafo, separados por linhas em branco — formato real confirmado
    nesta conversa). Um número de 11 dígitos (DDD + celular, 9 na frente)
    sozinho numa linha, com o e-mail por perto. "Por perto" ignora linhas
    em branco entre os dois — olha a vizinhança pelas linhas de conteúdo
    mais próximas, não pela posição bruta no texto, porque documentos como
    esse "cartão" costumam ter cada dado em seu próprio parágrafo com uma
    linha vazia entre eles. Só reconhece o número como celular quando um
    e-mail aparece por perto, exatamente pra não confundir com um CPF
    solto de 11 dígitos (que não tem essa relação com e-mail por perto) —
    CPF continua sendo reconhecido só com o rótulo explícito em
    `extrair_dados_genericos`."""
    dados = {}
    linhas = [l.strip() for l in texto.split("\n") if l.strip()]
    email_re = re.compile(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]+")
    for i, linha in enumerate(linhas):
        vizinhas = " ".join(linhas[max(0, i - 1): i + 2])
        m_email = email_re.search(linha)
        if m_email and "email" not in dados:
            dados["email"] = m_email.group(0)
        so_digitos = re.sub(r"\D", "", linha)
        if (
            "celular" not in dados
            and len(so_digitos) == 11
            and so_digitos[2] == "9"
            and so_digitos == re.sub(r"[\s()\-.]", "", linha)  # linha é só o telefone
            and email_re.search(vizinhas)
        ):
            dados["celular"] = so_digitos
    return dados


def extrair_coordenadas_livres(texto: str) -> dict:
    """Coordenadas soltas no texto (fora do formato de ART) — mesma
    extração/validação DMS -> UTM já usada pra ART, mas roda sobre qualquer
    documento (espelho de conta, fatura etc.)."""
    dados = {}
    coord, _achados = extrair_coordenadas(_normalizar(texto))
    if coord:
        dados["fuso"], dados["coord_x"], dados["coord_y"] = coord
    return dados


def _sem_duplicatas(linhas: list) -> list:
    """Remove linhas de equipamento EXATAMENTE iguais, mantendo a ordem —
    evita duplicar uma linha quando o mesmo trecho do documento acaba lido
    duas vezes por caminhos diferentes (ex.: o mesmo parágrafo aparece no
    texto vetorial normal E de novo via OCR de página inteira, quando o
    PDF tem um carimbo que precisa de OCR — ver `baixar_texto_arquivo`)."""
    vistos = set()
    resultado = []
    for linha in linhas:
        chave = tuple(sorted(linha.items()))
        if chave not in vistos:
            vistos.add(chave)
            resultado.append(linha)
    return resultado


def extrair_equipamentos_pedido(texto: str) -> dict:
    """Quantidade e potência de módulos/inversores num 'Pedido' de kit
    solar (ex.: Solfácil) — ex.: "15x MODULO BIFACIAL 600W ... Disponível"
    e "1x INVERSOR 6KW ... 220V ... Disponível". Marca e modelo ficam
    SEMPRE em branco aqui, de propósito — mesmo aparecendo escritos no
    pedido, a regra combinada é que marca/modelo de módulo e inversor são
    sempre conferidos manualmente pelo engenheiro nas tabelas do INMETRO,
    nunca preenchidos sozinhos pelo app. Só os números objetivos (quantos,
    quantos W/kW) vêm automáticos.

    Só isso — de propósito. Esse tipo de documento é um pedido de compra
    junto ao integrador, então ele TEM um "endereço de entrega" e uma
    "potência do kit", mas nenhum dos dois é confiável como dado do
    projeto: o endereço de entrega às vezes é diferente de onde o sistema
    vai ser instalado (pode ser a própria integradora, um depósito etc.),
    e por isso a gente NUNCA usa esse endereço pros campos de Logradouro/
    Bairro/Cidade/UF/CEP do formulário — quem manda nisso é a conta de
    energia (ART/espelho/fatura), não o pedido do kit."""
    paineis, inversores = [], []
    for m in re.finditer(
        r"(\d+)\s*x\s*M[ÓO]DULO\b.*?(\d+)\s*W\b.*?Dispon[íi]vel", texto, re.IGNORECASE | re.DOTALL
    ):
        qtd, watts = int(m.group(1)), int(m.group(2))
        paineis.append({"quantidade": qtd, "fabricante": "", "modelo": "",
                         "area_m2": 0, "potencia_kw": round(watts / 1000, 3)})
    for m in re.finditer(
        r"(\d+)\s*x\s*INVERSOR\b.*?(\d+(?:[.,]\d+)?)\s*KW\b.*?(\d{2,3})\s*V\b.*?Dispon[íi]vel",
        texto, re.IGNORECASE | re.DOTALL,
    ):
        qtd = int(m.group(1))
        kw = float(m.group(2).replace(",", "."))
        v = m.group(3)
        inversores.append({"quantidade": qtd, "fabricante": "", "modelo": "",
                            "potencia_kw": kw, "tensao_nominal_v": v})
    return {"paineis": _sem_duplicatas(paineis), "inversores": _sem_duplicatas(inversores)}


def extrair_projeto_executivo(texto: str) -> dict:
    """Campos do "Projeto Executivo" (diagrama unifilar de um SFCR —
    Sistema Fotovoltaico Conectado à Rede) — o próprio desenho técnico do
    projeto. Esse tipo de arquivo normalmente só é legível via OCR, porque
    os dados (UC, interessado, endereço etc.) ficam num print/imagem colado
    na página, não em texto vetorial de verdade — é por isso que
    `baixar_texto_arquivo` roda OCR extra pra páginas assim (ver
    `_tem_imagem_grande`), mesmo quando o PDF já tem algum texto normal.

    OCR num desenho CAD de múltiplas colunas embaralha a ordem de leitura —
    já confirmamos nesta mesma extração que os prefixos numerados ("01 —",
    "05 —" etc.) aparecem fora de ordem/duplicados, e que dígitos isolados
    podem vir trocados (ex.: "37,8V" lido como "57,8V"). Por segurança, essa
    função só tenta os campos com RÓTULO próprio, curto e isolado o
    suficiente pra não depender da ordem/posição no texto — nunca as
    especificações elétricas densas (Imp/Isc/Vmp/Voc/eficiência) e nunca
    marca/modelo de módulo/inversor (mesma regra de sempre: isso é
    conferido manualmente pelo engenheiro nas tabelas do INMETRO, nunca
    preenchido sozinho pelo app, mesmo estando escrito no documento)."""
    dados = {}

    # "N da UC:" às vezes sai truncado E duplicado no OCR (ex.: "N da
    # UC:1.081.277.017-0 — N da UC:1.081.277.017-00") — fica com a
    # ocorrência de MAIS dígitos, que é a versão completa.
    ucs = re.findall(r"N\s*da\s*UC\s*[:\-]?\s*([\d\.\-]+)", texto, re.IGNORECASE)
    if ucs:
        melhor = max(ucs, key=lambda s: len(re.sub(r"\D", "", s)))
        digitos = re.sub(r"\D", "", melhor)
        if digitos:
            dados["uc"] = digitos

    m = re.search(r"Tipo\s*de\s*Conex[ãa]o\s*[:\-]?\s*(Mono|Bi|Tri)f[áa]sic[oa]", texto, re.IGNORECASE)
    if m:
        prefixo = m.group(1).upper()
        dados["tipo_conexao"] = {"MONO": "MONOFÁSICO", "BI": "BIFÁSICO", "TRI": "TRIFÁSICO"}[prefixo]

    # "Interessado:" (com dois-pontos, seguido do nome) é o dado real.
    # "Interessado(a)" — sem dois-pontos, com "(a)" logo depois — é só o
    # rótulo vazio da linha de assinatura no carimbo do desenho; o `:`
    # exigido logo após a palavra já evita confundir os dois.
    m = re.search(r"Interessado\s*:\s*([^\n]+)", texto, re.IGNORECASE)
    if m:
        nome = m.group(1).strip(" .")
        if nome:
            dados["titular"] = nome.title()

    # "Local:" — formato próprio deste documento, diferente dos outros
    # layouts de endereço já tratados: "RUA Carajas, 286 — Centro. — Barra
    # do Garças" (a UF costuma continuar na linha seguinte do OCR, onde o
    # risco de embaralhamento é maior — por segurança não tentamos capturar
    # a UF aqui; o formulário já usa "MT" como padrão).
    m = re.search(r"\bLocal\s*:\s*([^\n]+)", texto, re.IGNORECASE)
    if m:
        m2 = re.match(
            r"\s*(.+?),\s*(\d+[A-Za-z]?)\s*[—–-]\s*(.+?)\.?\s*[—–-]\s*(.+?)\s*$",
            m.group(1),
        )
        if m2:
            dados["logradouro"] = m2.group(1).strip().title()
            dados["numero"] = m2.group(2).strip()
            dados["bairro"] = m2.group(3).strip().title()
            dados["cidade"] = m2.group(4).strip().title()

    # ---- Variante "Diagrama Unifilar" (ex.: modelo usado nos projetos
    # Equatorial-GO) — carimbo próprio, diferente do formato acima. Também
    # só é legível via OCR (mesmo motivo: título do desenho colado como
    # imagem), mas o carimbo aqui é limpo e cada rótulo aparece uma única
    # vez, sem o embaralhamento de colunas visto no diagrama CAD do outro
    # formato — por isso dá pra confiar num pouco mais de campo aqui.
    m = re.search(r"UNIDADE\s+CONSUMIDORA\s*\(N[ºo°]\)\s*[:\-]?\s*([\d\.\-]+)", texto, re.IGNORECASE)
    if m and "uc" not in dados:
        digitos = re.sub(r"\D", "", m.group(1))
        if digitos:
            dados["uc"] = digitos

    m = re.search(r"CIDADE\s+DA\s+OBRA\s*[:\-]?\s*([^\n]+?)\s*(?:SETOR\s+DA\s+OBRA|$)", texto, re.IGNORECASE)
    if m and "cidade" not in dados:
        cidade = m.group(1).strip(" .")
        if cidade:
            dados["cidade"] = cidade.title()

    # "SETOR DA OBRA" é o nome que documentos de Goiás costumam dar ao que,
    # nos outros formatos, chamamos de "bairro" — mapeado direto pro mesmo
    # campo do formulário.
    m = re.search(r"SETOR\s+DA\s+OBRA\s*[:\-]?\s*([^\n]+)", texto, re.IGNORECASE)
    if m and "bairro" not in dados:
        bairro = m.group(1).strip(" .")
        if bairro:
            dados["bairro"] = bairro.title()

    # "PROPRIETARIO:" com o nome (e, logo depois, o CNPJ/CPF) é o titular
    # real deste carimbo — bem distinto do "AUTOR DO PROJETO:" (o
    # engenheiro responsável, nunca o cliente). Exigir o rótulo
    # "PROPRIETARIO" imediatamente antes evita qualquer confusão entre os
    # dois nomes que aparecem no mesmo carimbo.
    m = re.search(
        r"PROPRIETARIO\s*:?\s*\n*\s*([A-ZÀ-Ú][A-Za-zÀ-ÿ\s]+?)\s*CNPJ\s*/?\s*CPF\s*[:\-]?\s*([\d,\.\-/]{11,18})",
        texto, re.IGNORECASE,
    )
    if m:
        nome = m.group(1).strip(" .")
        if nome and "titular" not in dados:
            dados["titular"] = nome.title()
        digitos = re.sub(r"\D", "", m.group(2))
        if len(digitos) in (11, 14) and "cpf_cnpj" not in dados:
            dados["cpf_cnpj"] = digitos

    # "POTÊNCIA TOTAL INSTALADA/INVERSORES" é a potência que a
    # concessionária efetivamente aprova (a do inversor, não a dos
    # módulos) — é esse número que corresponde ao campo "Potência
    # Instalada" do formulário. Só usa o "POTÊNCIA:" mais genérico do
    # carimbo se o rótulo específico não aparecer.
    m = (
        re.search(r"POT[ÊE]NCIA\s+TOTAL\s+INSTALADA\s*/\s*INVERSORES\s*[:\-]?\s*(\d+(?:[.,]\d+)?)\s*kWp?", texto, re.IGNORECASE)
        or re.search(r"\bPOT[ÊE]NCIA\s*[:\-]\s*(\d+(?:[.,]\d+)?)\s*kW\b", texto, re.IGNORECASE)
    )
    if m and "potencia_instalada_kw" not in dados:
        try:
            dados["potencia_instalada_kw"] = float(m.group(1).replace(",", "."))
        except ValueError:
            pass

    return dados


def extrair_equipamentos_projeto_executivo(texto: str) -> dict:
    """Quantidade, potência e — só para este tipo de documento, por
    decisão explícita do usuário — marca/modelo de módulos/inversores a
    partir do Projeto Executivo/Diagrama Unifilar. Diferente do pedido de
    kit de um revendedor (onde marca/modelo seguem SEMPRE em branco, pra
    confirmação manual nas tabelas do INMETRO), este é o próprio desenho
    técnico final assinado pelo engenheiro responsável — o usuário confirmou
    que esses dados podem ser considerados corretos aqui.

    Mesmo assim, o texto costuma vir de OCR (o carimbo é colado como
    imagem) e um desenho CAD multi-coluna pode embaralhar a ordem de
    leitura — então só tenta padrões que dá pra CONFERIR contra si mesmos
    ou que vêm de uma frase estruturada única e isolada, nunca monta linha
    "quase certa" juntando pedaços de lugares diferentes do texto:

    - Frase-lista ("1. 10 Módulos Fotovoltaicos de 600 Wp, marca LEAPTON,
      modelo LP182-182-M-72-NB;" / "2. 1 Inversores marca SOFAR, um modelo
      5KTLM-G3, potência pico AC 5 kW") — quantidade, marca e modelo saem
      todos da MESMA frase, numa lista numerada e pontuada; é o padrão
      preferido porque não depende de juntar rótulos espalhados pela
      página.
    - "NN — Marca/Modelo:" numa linha e o valor "FABRICANTE/MODELO" na
      seguinte (formato do carimbo SFCR/Energisa-MT) — módulo: cruza com a
      conta "qtd * unit = total" pra confirmar a quantidade; inversor: exige
      que a mesma sequência seja seguida por "Tipo:"+"Potência:" (só bate
      no bloco do inversor, não no do módulo nem no do DPS) e só assume 1
      inversor quando a potência dele bate exatamente com a "Potência
      Nominal" do sistema informada em outro ponto do documento — nunca
      quando os dois números não conferem entre si.
    - "15 * 600 Wp = 9 kWp" — variante sem marca/modelo, mas com a própria
      conta como conferência: se quantidade × potência unitária não bater
      com o total informado (tolerância pequena, só pra arredondamento), o
      achado é descartado inteiro.
    - Inversor por rótulos separados (FABRICANTE:/MODELO:/POTÊNCIA NOMINAL
      CA:, ou os rótulos genéricos Quantidade/Potência/Tensão de saída) —
      usado só como último reforço quando nada acima aparece; exige que os
      rótulos relevantes apareçam TODOS antes de montar a linha, nunca
      completa com um valor não confirmado.

    Tudo que essa função encontra continua marcado como OCR
    (`via_ocr=True` em quem chama) pra pedir atenção redobrada na
    conferência, mesmo com marca/modelo agora preenchidos."""
    paineis, inversores = [], []

    # --- frase-lista: "N Módulos Fotovoltaicos de P Wp, marca M, modelo D" ---
    for m in re.finditer(
        r"(\d+)\s*M[óo]dulos?\s*Fotovoltaicos?\s*de\s*(\d+(?:[.,]\d+)?)\s*Wp\s*,?\s*"
        r"marca\s+([A-ZÀ-Ú][\w\-]*)\s*,?\s*modelo\s+([A-Za-z0-9][\w\-\.]*)",
        texto, re.IGNORECASE,
    ):
        qtd = int(m.group(1))
        try:
            watts_unit = float(m.group(2).replace(",", "."))
        except ValueError:
            continue
        paineis.append({"quantidade": qtd, "fabricante": m.group(3).strip().upper(),
                         "modelo": m.group(4).strip().upper(),
                         "area_m2": 0, "potencia_kw": round(watts_unit / 1000, 3)})

    # --- frase-lista: "N Inversores marca M, [um] modelo D, potência pico AC P kW" ---
    for m in re.finditer(
        r"(\d+)\s*Inversores?\s*marca\s+([A-ZÀ-Ú][\w\-]*)\s*,?\s*(?:um\s+)?modelo\s+([A-Za-z0-9][\w\-\.]*)\s*,?\s*"
        r"pot[êe]ncia\s*pico\s*AC\s*(\d+(?:[.,]\d+)?)\s*kW",
        texto, re.IGNORECASE,
    ):
        qtd = int(m.group(1))
        try:
            kw = float(m.group(4).replace(",", "."))
        except ValueError:
            continue
        inversores.append({"quantidade": qtd, "fabricante": m.group(2).strip().upper(),
                            "modelo": m.group(3).strip().upper(),
                            "potencia_kw": kw, "tensao_nominal_v": ""})

    # --- rótulo "Marca/Modelo:" (formato do carimbo SFCR/Energisa-MT) ---
    # Um desenho CAD multi-coluna faz o OCR embaralhar o texto: entre o
    # rótulo e o valor real pode entrar um pedaço de outra coluna (ex.:
    # "Marca/Modelo:\n1000V ) Leapton /LP182-...") e a potência unitária
    # (Wp) quase nunca fica logo depois — normalmente vem de outra parte
    # da página. Por isso a busca aqui é em duas etapas independentes, sem
    # exigir que tudo fique junto:
    #   1) acha o par "Palavra / Palavra-com-número" mais próximo depois do
    #      rótulo (dentro de uma janela curta, ignorando o que vier antes
    #      dele) — funciona mesmo com lixo de OCR pelo meio, porque exige
    #      a barra "/" explícita entre marca e modelo, que não aparece por
    #      acaso em texto aleatório;
    #   2) só aceita esse achado como dado do MÓDULO ou do INVERSOR (nunca
    #      do DPS/protetor de surto — que também usa o mesmo rótulo "Marca
    #      /Modelo:" no mesmo carimbo) se o rótulo vier no formato numerado
    #      "NN — Marca/Modelo:" (com um número do item logo antes do
    #      traço). Testado contra um SFCR real (cliente Maria Geogirna
    #      Rodrigues) onde isso importa de verdade: o carimbo do DPS usa
    #      "Marca/Modelo:" SOZINHO, sem número na frente, enquanto módulo e
    #      inversor sempre têm o número do item ("01 — Marca/Modelo:").
    #      Uma tentativa anterior excluía o DPS procurando a palavra "DPS"
    #      nos 60 caracteres antes do rótulo — mas o OCR desse mesmo
    #      documento embaralha a ordem de leitura a ponto de "DPS" aparecer
    #      perto do rótulo do INVERSOR (excluindo o inversor por engano) e
    #      longe do rótulo do próprio DPS (deixando o DPS passar por
    #      engano) — os dois erros aconteceram ao mesmo tempo no mesmo
    #      arquivo. Exigir o número resolve isso sem depender da ordem de
    #      leitura embaralhada.
    #   A quantidade nunca é advinhada aqui: só usa o achado se, em
    #   QUALQUER outro ponto do mesmo texto, existir uma conta "qtd * unit
    #   = total" batendo exatamente com essa potência unitária — a mesma
    #   conferência por aritmética já usada na variante sem marca/modelo
    #   logo abaixo.
    _candidatos_marca_modelo = list(re.finditer(
        r"[O0]?\d{1,2}\s*[-—–]\s*Marca\s*/?\s*Modelo\s*:\s*", texto, re.IGNORECASE
    ))

    def _valor_marca_modelo(pos_fim: int):
        janela = texto[pos_fim: pos_fim + 150]
        m = re.search(r"([A-Za-zÀ-ÿ]{3,})\s*/\s*([\w][\w\-—–]{2,}?)(?=[\s.;,)]|$)", janela)
        if not m:
            return None
        # Um modelo de equipamento real sempre tem letra E número juntos
        # (ex.: "TS585S8T-144GANT", "MIN10000TL-X", "LP182-182-M-72-NB").
        # Um valor só de letras ou só de números aqui é sinal de que o OCR
        # embaralhou texto de outra parte da página (título do desenho,
        # data, "Segue para...") pro lado do rótulo — descarta em vez de
        # arriscar um dado errado (confirmado com o mesmo SFCR real: sem
        # esse filtro o rótulo do módulo casava com "Setembro/2028", do
        # carimbo de data do desenho).
        modelo = m.group(2)
        if not (re.search(r"[A-Za-z]", modelo) and re.search(r"\d", modelo)):
            return None
        return m

    # --- mesmo carimbo numerado ("01 — Marca/Modelo:", "02 — Potência
    # nominal:", "03 — Quantidade:" ...), mas versão SEM ruído de OCR entre
    # os rótulos (texto digitado/copiado direto, não escaneado) — como
    # aqui a "Quantidade:" e a "Potência nominal:" aparecem legíveis logo
    # depois do par marca/modelo, não precisa da conta de conferência: lê
    # os dois valores direto, cada um do seu próprio rótulo. A janela fica
    # travada até o PRÓXIMO "Marca/Modelo:" (ou uma extensão pequena, se
    # não houver um próximo) — pra nunca vazar um número do bloco do
    # inversor pro do módulo ou vice-versa.
    if not paineis and _candidatos_marca_modelo:
        cand = _candidatos_marca_modelo[0]
        fim_janela = _candidatos_marca_modelo[1].start() if len(_candidatos_marca_modelo) > 1 else cand.end() + 500
        mv = _valor_marca_modelo(cand.end())
        if mv:
            janela = texto[cand.end(): fim_janela]
            m_qtd = re.search(r"Quantidade\s*[:\-]?\s*(\d+)\b", janela, re.IGNORECASE)
            m_pot = re.search(r"Pot[êeé]ncia(?:\s*nominal)?\s*[:\-]?\s*(\d+(?:[.,]\d+)?)\s*Wp?\b", janela, re.IGNORECASE)
            if m_qtd and m_pot:
                try:
                    watts_unit = float(m_pot.group(1).replace(",", "."))
                except ValueError:
                    watts_unit = None
                if watts_unit:
                    paineis.append({
                        "quantidade": int(m_qtd.group(1)),
                        "fabricante": mv.group(1).strip().upper(),
                        "modelo": mv.group(2).strip(" -—–").upper().replace("—", "-").replace("–", "-"),
                        "area_m2": 0, "potencia_kw": round(watts_unit / 1000, 3),
                    })

    # --- mesmo formato, mas OCR ruidoso e "Quantidade:" ilegível — usa a
    # conta "qtd * unit = total" (achada em qualquer ponto do texto) como
    # conferência da quantidade em vez de ler o rótulo direto.
    if not paineis and _candidatos_marca_modelo:
        mv = _valor_marca_modelo(_candidatos_marca_modelo[0].end())
        if mv:
            fabricante = mv.group(1).strip().upper()
            modelo = mv.group(2).strip(" -—–").upper().replace("—", "-").replace("–", "-")
            for m2 in re.finditer(
                r"(\d+)\s*[\*x]\s*(\d+)\s*Wp?\s*=\s*(\d+(?:[.,]\d+)?)\s*kWp?",
                texto, re.IGNORECASE,
            ):
                qtd, watts_unit = int(m2.group(1)), int(m2.group(2))
                try:
                    total_informado = float(m2.group(3).replace(",", "."))
                except ValueError:
                    continue
                total_calculado = qtd * watts_unit / 1000
                if abs(total_calculado - total_informado) <= max(0.05, total_informado * 0.02):
                    paineis.append({
                        "quantidade": qtd, "fabricante": fabricante, "modelo": modelo,
                        "area_m2": 0, "potencia_kw": round(watts_unit / 1000, 3),
                    })
                    break

    # --- variante sem marca/modelo, só com a conta de conferência ---
    if not paineis:
        for m in re.finditer(
            r"(\d+)\s*[\*x]\s*(\d+)\s*Wp?\s*=\s*(\d+(?:[.,]\d+)?)\s*kWp?",
            texto, re.IGNORECASE,
        ):
            qtd, watts_unit = int(m.group(1)), int(m.group(2))
            try:
                total_informado = float(m.group(3).replace(",", "."))
            except ValueError:
                continue
            total_calculado = qtd * watts_unit / 1000
            if abs(total_calculado - total_informado) <= max(0.05, total_informado * 0.02):
                paineis.append({"quantidade": qtd, "fabricante": "", "modelo": "",
                                 "area_m2": 0, "potencia_kw": round(watts_unit / 1000, 3)})

    # --- mesmo carimbo numerado, versão SEM ruído de OCR — lê "Quantidade:"
    # e "Potencia:" direto dos rótulos do bloco do inversor (o último
    # candidato não-DPS), sem precisar da conferência aritmética. Janela
    # travada a ~500 caracteres depois do rótulo (não tem um próximo
    # "Marca/Modelo:" depois do inversor pra travar nele).
    if not inversores and _candidatos_marca_modelo:
        cand = _candidatos_marca_modelo[-1]
        mv = _valor_marca_modelo(cand.end())
        if mv:
            janela = texto[cand.end(): cand.end() + 500]
            m_qtd = re.search(r"Quantidade\s*[:\-]?\s*(\d+)\b", janela, re.IGNORECASE)
            m_pot = re.search(r"Pot[êeé]ncia\s*[:\-]?\s*(\d+(?:[.,]\d+)?)\s*kW\b", janela, re.IGNORECASE)
            if m_qtd and m_pot:
                try:
                    potencia_unit = float(m_pot.group(1).replace(",", "."))
                except ValueError:
                    potencia_unit = None
                if potencia_unit:
                    inversores.append({
                        "quantidade": int(m_qtd.group(1)),
                        "fabricante": mv.group(1).strip().upper(),
                        "modelo": mv.group(2).strip(" -—–").upper().replace("—", "-").replace("–", "-"),
                        "potencia_kw": potencia_unit, "tensao_nominal_v": "",
                    })

    # --- rótulo "Marca/Modelo:" pro INVERSOR (mesmo carimbo SFCR/
    # Energisa-MT), mas OCR ruidoso e "Quantidade:" ilegível — usa os
    # mesmos candidatos já filtrados acima (exclui o DPS), pega o ÚLTIMO em
    # vez do primeiro: na ordem real do texto é sempre módulo, DPS,
    # inversor — então, tirando o DPS, o último que sobra é o inversor. Só
    # assume 1 inversor quando isso é CONFIRMÁVEL: a potência dele bate
    # exatamente com a "Potência Nominal" do sistema informada em outro
    # ponto do documento (um único inversor já cobre a potência total).
    # Nunca advinha quantidade quando os dois números não batem — nesse
    # caso o inversor simplesmente não entra na lista, pra confirmação
    # manual.
    if not inversores and len(_candidatos_marca_modelo) > 1:
        mv = _valor_marca_modelo(_candidatos_marca_modelo[-1].end())
        if mv:
            fabricante = mv.group(1).strip().upper()
            modelo = mv.group(2).strip(" -—–").upper().replace("—", "-").replace("–", "-")
            m_pot = re.search(
                r"Pot[êeé]ncia\s*:\s*(\d+(?:[.,]\d+)?)\s*kW",
                texto[_candidatos_marca_modelo[-1].end(): _candidatos_marca_modelo[-1].end() + 200],
                re.IGNORECASE,
            )
            m_total = re.search(r"Pot[êeé]ncia\s*Nominal\s*:\s*(\d+(?:[.,]\d+)?)\s*kW", texto, re.IGNORECASE)
            if m_pot and m_total:
                try:
                    potencia_unit = float(m_pot.group(1).replace(",", "."))
                    potencia_total = float(m_total.group(1).replace(",", "."))
                except ValueError:
                    potencia_unit = potencia_total = None
                if potencia_unit and potencia_total and abs(potencia_total - potencia_unit) <= max(0.05, potencia_total * 0.02):
                    inversores.append({
                        "quantidade": 1, "fabricante": fabricante, "modelo": modelo,
                        "potencia_kw": potencia_unit, "tensao_nominal_v": "",
                    })

    # --- inversor por rótulos separados (reforço, só se nada acima achou) ---
    if not inversores:
        m_qtd = re.search(r"Quantidade\s*[:\-]?\s*(\d+)\b", texto, re.IGNORECASE)
        m_pot = re.search(r"Pot[êeé]ncia(?:\s*Nominal)?(?:\s*CA)?\s*[:\-]?\s*(\d+(?:[.,]\d+)?)\s*k?W\b", texto, re.IGNORECASE)
        m_tensao = re.search(r"Tens[ãa]o\s*de\s*sa[íi]da\s*[:\-]?\s*(\d{2,3})\s*V\b", texto, re.IGNORECASE)
        m_fab = re.search(r"FABRICANTE\s*[:\-]?\s*([A-ZÀ-Ú][\w\-]*)", texto, re.IGNORECASE)
        # (?<!Marca/) evita casar o "Modelo:" que faz parte do rótulo
        # combinado "Marca/Modelo:" tratado nos blocos acima — aqui o
        # rótulo tem que ser "MODELO:" sozinho, senão o fabricante do
        # módulo pode vazar pra dentro do inversor por engano.
        m_mod = re.search(r"(?<!Marca/)(?<!Marca /)\bMODELO\s*[:\-]?\s*([A-Za-z0-9][\w\-\.]*)", texto, re.IGNORECASE)
        if m_qtd and m_pot and m_tensao:
            potencia = float(m_pot.group(1).replace(",", "."))
            if potencia > 100:  # veio em W, não kW (ex.: "POTÊNCIA NOMINAL CA: 5000")
                potencia = round(potencia / 1000, 3)
            inversores.append({
                "quantidade": int(m_qtd.group(1)),
                "fabricante": m_fab.group(1).strip().upper() if m_fab else "",
                "modelo": m_mod.group(1).strip().upper() if m_mod else "",
                "potencia_kw": potencia,
                "tensao_nominal_v": m_tensao.group(1),
            })

    return {"paineis": _sem_duplicatas(paineis), "inversores": _sem_duplicatas(inversores)}


def eh_documento_fabricante_equipamento(texto: str) -> bool:
    """Detecta documentos que descrevem o EQUIPAMENTO em si — registro/
    certificado do Inmetro ou datasheet técnico de módulo/inversor —, nunca
    o cliente ou a instalação. Esses documentos sempre têm o endereço,
    e-mail e telefone da FABRICANTE (ou de quem registrou o equipamento no
    Inmetro), nunca do cliente — foi exatamente isso que causou o bug
    reportado pelo usuário: o CEP e o e-mail do fabricante (SOFARSOLAR e
    Leapton, respectivamente) foram parar nos campos de endereço/contato do
    formulário porque os extratores soltos de endereço/e-mail/CEP rodavam
    sobre QUALQUER arquivo da pasta, sem levar em conta o tipo de
    documento.

    Regra do usuário, confirmada nesta conversa: endereço sempre vem da ART
    ou da conta de energia; localização/e-mail/telefone sempre vêm do
    "cartão de contato" do cliente; specs/quantidade de equipamento sempre
    vêm do diagrama + datasheet + registro do Inmetro. Ou seja, um datasheet
    ou registro do Inmetro é uma fonte válida de EQUIPAMENTO, mas nunca de
    endereço/contato — por isso esta função só é usada pra pular os
    extratores soltos de endereço/contato/CEP (`extrair_endereco_livre`,
    `extrair_endereco_bloco_cep`, `extrair_contato_livre`,
    `extrair_coordenadas_livres`, `extrair_dados_genericos`); a extração de
    equipamentos (`extrair_equipamentos_projeto_executivo`,
    `extrair_equipamentos_pedido`) continua rodando normalmente sobre esses
    arquivos, exatamente como antes.

    Os marcadores abaixo são conferidos com o texto real de um registro do
    Inmetro e de um datasheet (Leapton) nesta mesma conversa."""
    tu = texto.upper()
    # Página de consulta de registro do Inmetro (registro.inmetro.gov.br) —
    # cabeçalho fixo, sempre presente nesse formato.
    if (
        "REGISTRO.INMETRO.GOV.BR" in tu
        or "AVALIAÇÃO DA CONFORMIDADE" in tu
        or "AVALIACAO DA CONFORMIDADE" in tu
        or "PROGRAMA DE AVALIAÇÃO DA CONFORMIDADE" in tu
    ):
        return True
    # Datasheet técnico de módulo/inversor — exige pelo menos 2 marcadores
    # (não só 1), pra não confundir com um documento qualquer que por
    # acaso menciona um único termo técnico em comum.
    marcadores = (
        "ELECTRICAL PARAMETERS", "MECHANICAL DIAGRAMS", "TEMPERATURE CHARACTERISTICS",
        "PACKING CONFIGURATION", "OPERATING TEMPERATURE", "STANDARD TEST CONDITIONS",
        "TEMPERATURE COEFFICIENT", "MPPT VOLTAGE RANGE",
    )
    return sum(1 for marcador in marcadores if marcador in tu) >= 2


CAMPOS_TIPO = {
    # Repare que não tem nenhum extrator de "Pedido de kit solar" aqui — de
    # propósito. Desse tipo de documento só se aproveita a lista de
    # equipamentos (via `extrair_equipamentos_pedido`, chamado à parte em
    # `levantamento_pasta`); endereço/UC/titular sempre vêm da conta de
    # energia (ART/espelho/fatura) ou do Projeto Executivo, nunca do pedido
    # do kit.
    # `extrair_projeto_executivo` roda logo depois da ART — antes dos
    # extratores de padrão solto (endereço/contato/genérico) — porque os
    # rótulos dele são específicos e menos propensos a "casar" com texto
    # que não tem nada a ver (um risco real: já vimos um desenho CAD cheio
    # de legendas técnicas enganar o regex frouxo de endereço solto).
    "energisa": (
        extrair_dados_art, extrair_projeto_executivo, extrair_dados_conta_energisa,
        extrair_endereco_livre, extrair_endereco_bloco_cep, extrair_contato_livre,
        extrair_coordenadas_livres, extrair_dados_genericos,
    ),
    "equatorial": (
        extrair_dados_art, extrair_projeto_executivo, extrair_endereco_livre,
        extrair_endereco_bloco_cep, extrair_contato_livre, extrair_coordenadas_livres,
        extrair_dados_genericos,
    ),
}


# Extratores que só podem confiar no "cartão de contato" — o Google Doc
# solto (coordenadas + e-mail + celular, cada um em seu próprio parágrafo)
# que o usuário confirmou que SEMPRE está na pasta do cliente e que deve
# ser a ÚNICA fonte de localização/e-mail/telefone. Rodar esses dois
# extratores sobre QUALQUER outro documento é sempre arriscado: eles não
# têm rótulo nenhum pra se guiar (são "soltos" de propósito, pra reconhecer
# o formato do cartão de contato), então pegam o primeiro e-mail/número que
# aparecer no texto — e documentos legítimos e necessários pra outros
# campos, como a própria ART, costumam ter um e-mail institucional no
# rodapé (ex.: "atendimento@creago.org.br", do CREA-GO) que não é o do
# cliente. Isso já causou dois bugs reais nesta conversa: primeiro com
# e-mail de fabricante de equipamento (resolvido restringindo os
# documentos de fabricante — ver `eh_documento_fabricante_equipamento`),
# depois com o e-mail institucional do próprio CREA-GO vindo da ART.
# Restringir esses dois extratores só ao cartão de contato resolve a
# causa raiz de vez, em vez de ficar bloqueando um domínio de e-mail
# problemático por vez.
EXTRATORES_SO_CARTAO_CONTATO = (extrair_contato_livre, extrair_coordenadas_livres)


def extrair_campos_arquivo(texto: str, tipo: str, eh_cartao_contato: bool = False) -> dict:
    """Roda todos os extratores relevantes pro tipo de projeto sobre o
    texto de UM arquivo e devolve um dict só com o que foi reconhecido.
    `eh_cartao_contato` deve ser True só pro Google Doc de contato do
    cliente — nesse (e só nesse) arquivo os extratores soltos de
    e-mail/celular/coordenadas rodam; em qualquer outro arquivo (mesmo a
    ART, mesmo a conta de energia) eles ficam de fora, pra nunca pegar um
    e-mail/telefone que não seja o do cliente."""
    resultado = {}
    for extrator in CAMPOS_TIPO.get(tipo, ()):
        if extrator in EXTRATORES_SO_CARTAO_CONTATO and not eh_cartao_contato:
            continue
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
# Fotos/prints (foto de documento, print de WhatsApp, nota escaneada) — lidos
# via OCR. Texto de OCR é mais sujeito a erro que um PDF/Doc digitado, então
# todo campo achado nesses arquivos fica marcado "(OCR)" na fonte, pra pedir
# atenção redobrada na conferência.
MIME_IMAGEM = {"image/jpeg", "image/png"}
MAX_BYTES_IMAGEM_OCR = 15 * 1024 * 1024  # não roda OCR em foto gigante

# Muitos "espelhos de conta"/comprovantes chegam pro cliente como uma FOTO
# (tirada com o celular, às vezes exportada como PDF por um app de scanner)
# em vez de um PDF gerado digitalmente. Nesses casos o pypdf não acha texto
# nenhum (ou quase nada) — a página é só uma imagem dentro do PDF. Quando
# isso acontece, a gente trata a página como foto e roda OCR nela também,
# em vez de simplesmente desistir do arquivo.
LIMIAR_TEXTO_PDF_OK = 40  # caracteres não-espaço; abaixo disso, tenta OCR
MAX_PAGINAS_OCR_PDF = 6  # nunca faz OCR em PDF gigante (custo/tempo)

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


def _ocr_imagem(conteudo: bytes) -> str:
    """OCR de uma foto/print (PIL + pytesseract, português+inglês). Exige
    `tesseract-ocr` e `tesseract-ocr-por` instalados no sistema (packages.txt)
    e `pytesseract`+`Pillow` no requirements.txt — se não estiverem
    disponíveis, propaga a exceção e quem chamou trata como erro do arquivo,
    sem travar o levantamento dos demais arquivos da pasta."""
    from PIL import Image
    import pytesseract
    img = Image.open(io.BytesIO(conteudo))
    try:
        return pytesseract.image_to_string(img, lang="por+eng")
    except pytesseract.TesseractError:
        # idioma "por" pode não estar instalado no servidor — tenta só inglês
        # em vez de falhar o arquivo inteiro.
        return pytesseract.image_to_string(img, lang="eng")


# Renderizar uma folha de projeto grande (ex.: prancha A1/A0 de "Diagrama
# Unifilar", ~33x23 polegadas) a 250dpi pode passar de 40 megapixels — o
# OCR disso sozinho já levou mais de 50s numa página real testada nesta
# conversa, e isso somado a vários arquivos assim na mesma pasta é o tipo
# de uso de CPU sustentado que o Streamlit Cloud (plano gratuito) detecta
# e reage reduzindo a CPU do app ("throttling"), deixando tudo mais lento
# e capaz de derrubar a leitura no meio. Por isso o lado maior da imagem
# nunca passa deste limite — abaixo disso o dpi pedido continua valendo
# (folhas A4 comuns, por exemplo, não são afetadas nem de longe).
#
# CUIDADO: um teste real (nesta mesma conversa) mostrou que baixar o dpi
# de propósito, como forma "rotineira" de economizar tempo, piora a OCR o
# suficiente pra ler número errado — em 150dpi (numa folha de 33"), um "5"
# virou "S" no modelo de um inversor ("5KTLM-G3" -> "SKTLM-G3") e um "9"
# virou "2" num CPF, o que é sério (dado errado que PARECE confirmado) e,
# de quebra, ainda escapa da deduplicação por igualdade exata
# (`_sem_duplicatas`), aparecendo como linha duplicada. E o ganho de tempo
# nem foi tão grande assim (~40s contra ~51s a 250dpi de verdade) — não
# compensa o risco. Por isso este teto fica ALTO: só existe como rede de
# segurança pra folha absurdamente grande ou PDF com imagem em dpi nativo
# muito alto (evita estourar memória/CPU num caso extremo), não como jeito
# de acelerar o caso comum. Numa folha real de 33x23" a 250dpi (a maior
# testada nesta conversa) dá ~8275px de lado maior — este teto fica bem
# acima disso de propósito, pra não interferir na resolução desses casos.
MAX_PIXELS_LADO_MAIOR_OCR = 9000


def _pixmap_ocr(pagina, dpi_alvo: int = 250, clip=None):
    """`get_pixmap` com um teto de resolução — usa `dpi_alvo` até o lado
    maior da imagem bater `MAX_PIXELS_LADO_MAIOR_OCR`; a partir daí reduz o
    dpi efetivo pra não estourar isso, custe o que custar em nitidez (uma
    OCR mais “grossa” ainda é melhor que travar/estourar CPU)."""
    rect = clip if clip is not None else pagina.rect
    lado_maior_pt = max(rect.width, rect.height, 1)
    lado_maior_px_no_dpi_alvo = lado_maior_pt / 72 * dpi_alvo
    if lado_maior_px_no_dpi_alvo > MAX_PIXELS_LADO_MAIOR_OCR:
        dpi_alvo = max(72, int(MAX_PIXELS_LADO_MAIOR_OCR / (lado_maior_pt / 72)))
    if clip is not None:
        return pagina.get_pixmap(dpi=dpi_alvo, clip=clip)
    return pagina.get_pixmap(dpi=dpi_alvo)


def _ocr_pdf_escaneado(conteudo: bytes, paginas: list | None = None) -> str:
    """Renderiza página(s) do PDF como imagem (PyMuPDF, sem depender de
    binário externo tipo poppler) e roda OCR em cada uma. Sempre OCRa a
    página INTEIRA (nunca um recorte) — um teste real já mostrou que
    recortar só a região da imagem grande detectada PERDE dado quando o
    carimbo/tabela é "texto vetorizado" (letras desenhadas como forma
    dentro do PDF, sem código de caractere — comum em CAD exportado):
    isso não é uma imagem raster nenhuma, então nem aparece nos bboxes de
    imagem, só a página inteira rasterizada + OCR recupera.

    `paginas`: quando o chamador já sabe QUAIS páginas têm chance de conter
    dado novo (ex.: só as que têm uma imagem grande colada — ver
    `_pdf_tem_imagem_grande`), passa a lista de índices aqui pra não gastar
    CPU OCRando as demais páginas do documento à toa (elas já têm o texto
    vetorial normal, que o pypdf/PyMuPDF já leram certinho). Se `None`
    (caso do PDF "todo escaneado", onde não dá pra saber de antemão qual
    página tem o quê), OCRa as primeiras `MAX_PAGINAS_OCR_PDF` em ordem."""
    import fitz  # PyMuPDF
    doc = fitz.open(stream=conteudo, filetype="pdf")
    if paginas is not None:
        indices = [i for i in paginas if 0 <= i < len(doc)][:MAX_PAGINAS_OCR_PDF]
    else:
        indices = list(range(min(len(doc), MAX_PAGINAS_OCR_PDF)))
    partes = []
    for i in indices:
        pix = _pixmap_ocr(doc[i], dpi_alvo=250)
        partes.append(_ocr_imagem(pix.tobytes("png")))
    return "\n".join(partes)


# Alguns PDFs "de verdade" (com texto vetorial normal, ex.: um desenho CAD
# de "Projeto Executivo") têm TAMBÉM uma imagem colada (print/foto de uma
# tabela de dados) grande o bastante pra conter a informação mais
# importante da página — e nem pypdf nem o `get_text()` do PyMuPDF
# enxergam o que está dentro de uma imagem. Esse caso não cai no fallback
# de "PDF escaneado" acima porque o texto total já não é escasso (o
# restante da página tem texto vetorial normal) — precisa de uma checagem
# própria.
FRACAO_MINIMA_IMAGEM_PAGINA = 0.20  # cobre boa parte da página (scan comum)
# Alternativa em tamanho ABSOLUTO — necessária pra folhas grandes (ex.: uma
# prancha de projeto A1/A0), onde uma imagem colada com uma tabela de dados
# legível (poucas polegadas de lado, mas com boa resolução de pixel) fica
# pequena PERTO do total da folha e não bateria o critério de fração acima.
# Validado contra um "Projeto Executivo" real: imagem 1378x961px colada
# numa área de ~6,4x4,5 polegadas (461x322pt) de uma folha de ~33x23
# polegadas — passa nos dois critérios abaixo, mas ficaria bem abaixo de
# 20% da área da folha.
LARGURA_MINIMA_PT = 150
ALTURA_MINIMA_PT = 150
PIXELS_MINIMOS_IMAGEM = 300 * 300


def _bboxes_imagens_grandes(pagina, fracao_minima: float = FRACAO_MINIMA_IMAGEM_PAGINA) -> list:
    """Devolve os bboxes das imagens da página grandes o bastante — em
    fração da página OU em tamanho absoluto com resolução decente — pra
    ser, provavelmente, uma tabela/bloco de dados colado como print em vez
    de desenhado como texto. Usado tanto pra DECIDIR se roda OCR extra
    quanto, quando roda, pra saber ONDE recortar (ver `_ocr_regioes_imagem_
    grande` — recortar só essas regiões, em vez de OCRar a página inteira,
    evita duplicar o texto vetorial que o pypdf/PyMuPDF já leu direito)."""
    try:
        area_pagina = pagina.rect.width * pagina.rect.height
    except Exception:
        return []
    if area_pagina <= 0:
        return []
    achados = []
    for img in pagina.get_images(full=True):
        xref = img[0]
        largura_px, altura_px = img[2], img[3]
        try:
            bboxes = pagina.get_image_rects(xref)
        except Exception:
            continue
        for bbox in bboxes:
            area_img = bbox.width * bbox.height
            grande = area_img / area_pagina >= fracao_minima or (
                bbox.width >= LARGURA_MINIMA_PT
                and bbox.height >= ALTURA_MINIMA_PT
                and largura_px * altura_px >= PIXELS_MINIMOS_IMAGEM
            )
            if grande:
                achados.append(bbox)
    return achados


def _tem_imagem_grande(pagina) -> bool:
    return bool(_bboxes_imagens_grandes(pagina))


def _paginas_com_imagem_grande(conteudo: bytes) -> list:
    """Índices das páginas (até MAX_PAGINAS_OCR_PDF) que têm uma imagem
    colada grande o bastante pra ser um carimbo/tabela de dados — usado
    tanto pra DECIDIR se vale rodar OCR extra quanto pra saber EM QUAIS
    páginas rodar (ver `_ocr_pdf_escaneado`), evitando OCR desnecessário
    nas páginas que só têm texto vetorial normal."""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=conteudo, filetype="pdf")
        achadas = []
        for i, pagina in enumerate(doc):
            if i >= MAX_PAGINAS_OCR_PDF:
                break
            if _tem_imagem_grande(pagina):
                achadas.append(i)
        return achadas
    except Exception:
        return []


def _pdf_tem_imagem_grande(conteudo: bytes) -> bool:
    return bool(_paginas_com_imagem_grande(conteudo))


# Tamanho mínimo (largura × altura, em pixels) pra uma imagem embutida no
# PDF contar como candidata a "foto de localização" — abaixo disso é mais
# provável ser um logo/carimbo/assinatura do que um print de mapa. Visto
# num Diagrama Unifilar real desta conversa: o print de satélite tinha
# 2645×1479px, a assinatura do engenheiro (que aparece duas vezes na
# mesma página) tinha só 562×238px — bem abaixo do limiar.
MIN_PIXELS_IMAGEM_LOCALIZACAO = 400 * 300


def _eh_diagrama_com_imagem_situacao(texto: str) -> bool:
    """Reconhece o Diagrama Unifilar/Bloco pelo título fixo do carimbo —
    esse é o tipo de documento que, na prática, sempre traz colado o print
    do mapa/satélite ("SITUAÇÃO DA UNIDADE CONSUMIDORA") junto com o
    desenho técnico. Usado só pra decidir em qual arquivo vale a pena
    procurar essa imagem (evita abrir e escanear TODO PDF da pasta atrás
    de imagens à toa)."""
    tu = texto.upper()
    return "DIAGRAMA UNIFILAR" in tu or "SITUAÇÃO DA UNIDADE CONSUMIDORA" in tu


def extrair_imagem_situacao_diagrama(conteudo_pdf: bytes) -> bytes | None:
    """Acha e devolve (em PNG) a maior imagem colada no Diagrama Unifilar —
    na prática, sempre o print de mapa/satélite ("SITUAÇÃO DA UNIDADE
    CONSUMIDORA"), nunca a assinatura do engenheiro ou outro carimbo
    pequeno, porque esses são sempre muito menores (confirmado com um
    Diagrama Unifilar real nesta conversa: o print de mapa tinha ~13x mais
    área em pixels que a assinatura). Usada pra preencher automaticamente
    a "foto de localização" do Memorial Descritivo (Equatorial-GO) sem
    depender do usuário subir separadamente uma imagem que ele já tem
    colada nesse outro arquivo. Devolve None se não achar nenhuma imagem
    grande o bastante (`MIN_PIXELS_IMAGEM_LOCALIZACAO`) — nunca inventa
    um recorte da própria página como se fosse a foto."""
    try:
        import fitz  # PyMuPDF
        from PIL import Image

        doc = fitz.open(stream=conteudo_pdf, filetype="pdf")
        melhor = None  # (area_px, bytes_png)
        for pagina in doc:
            for info in pagina.get_images(full=True):
                xref = info[0]
                try:
                    base = doc.extract_image(xref)
                except Exception:
                    continue
                largura, altura = base.get("width", 0), base.get("height", 0)
                area = largura * altura
                if area < MIN_PIXELS_IMAGEM_LOCALIZACAO:
                    continue
                if melhor is not None and area <= melhor[0]:
                    continue
                try:
                    # Converte pra PNG (o slot de imagem do modelo do
                    # Memorial Descritivo é PNG — ver `substituir_foto_
                    # localizacao`), mesmo que a imagem original seja JPEG.
                    img = Image.open(io.BytesIO(base["image"])).convert("RGB")
                    saida = io.BytesIO()
                    img.save(saida, format="PNG")
                    melhor = (area, saida.getvalue())
                except Exception:
                    continue
        return melhor[1] if melhor else None
    except Exception:
        return None



def baixar_texto_arquivo(servico, arquivo: dict) -> tuple[str, bool]:
    """Baixa/exporta um arquivo e devolve (texto, veio_de_ocr). Devolve
    string vazia para tipos que não sabemos extrair texto."""
    mime = arquivo["mimeType"]
    file_id = arquivo["id"]
    try:
        if mime == "application/vnd.google-apps.document":
            dados = servico.files().export(fileId=file_id, mimeType="text/plain").execute()
            texto = dados.decode("utf-8", errors="ignore") if isinstance(dados, bytes) else str(dados)
            return texto, False

        if mime == "application/vnd.google-apps.spreadsheet":
            dados = servico.files().export(fileId=file_id, mimeType="text/csv").execute()
            texto = dados.decode("utf-8", errors="ignore") if isinstance(dados, bytes) else str(dados)
            return texto, False

        if mime not in MIME_TEXTO_EXTRAIVEL and mime not in MIME_IMAGEM:
            return "", False

        conteudo = servico.files().get_media(fileId=file_id).execute()

        if mime == "application/pdf":
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(conteudo))
            partes = []
            for pagina in reader.pages:
                partes.append(pagina.extract_text() or "")
            texto = "\n".join(partes)
            if len(re.sub(r"\s", "", texto)) < LIMIAR_TEXTO_PDF_OK:
                # quase nenhum texto veio do PDF — provavelmente é uma
                # foto/scan salvo como PDF, não um PDF "de verdade". Tenta
                # OCR nas páginas antes de desistir do arquivo.
                try:
                    texto_ocr = _ocr_pdf_escaneado(conteudo)
                except Exception:
                    return texto, False
                if len(texto_ocr.strip()) > len(texto.strip()):
                    return texto_ocr, True
                return texto, False

            # o PDF tem texto vetorial de verdade (não é um scan) — mas
            # pode ter, além disso, um carimbo/tabela de dados que nenhum
            # extrator de texto normal enxerga: seja uma imagem colada
            # (print/foto) ou texto "vetorizado" (letras desenhadas como
            # forma, sem código de caractere nenhum — comum em carimbo de
            # CAD exportado). `_pdf_tem_imagem_grande` só detecta o primeiro
            # caso, mas na prática o segundo também acontece (já confirmado
            # com um documento real desta mesma conversa); por segurança
            # rodamos OCR na página inteira sempre que uma imagem grande é
            # detectada, mesmo sem saber ao certo qual dos dois é o motivo.
            # Isso pode reler (via OCR) um trecho que o texto vetorial já
            # tinha capturado — por isso o texto OCR é só ACRESCENTADO
            # (nunca substitui o vetorial) e, do lado de quem consome esse
            # texto, listas de equipamento passam por `_sem_duplicatas`
            # antes de virar linha de tabela, pra essa releitura não virar
            # linha duplicada. E, pra economizar CPU (ver `MAX_PIXELS_LADO_
            # MAIOR_OCR` acima), só OCRamos as páginas que de fato têm uma
            # imagem grande colada — as demais páginas do mesmo documento já
            # tiveram o texto vetorial lido certinho, não precisam de OCR.
            try:
                paginas_alvo = _paginas_com_imagem_grande(conteudo)
                if paginas_alvo:
                    texto_ocr = _ocr_pdf_escaneado(conteudo, paginas=paginas_alvo)
                    if texto_ocr.strip():
                        return texto + "\n" + texto_ocr, True
            except Exception:
                pass
            return texto, False

        if mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            from docx import Document
            doc = Document(io.BytesIO(conteudo))
            return "\n".join(p.text for p in doc.paragraphs), False

        if mime == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(conteudo), data_only=True)
            partes = []
            for aba in wb.worksheets:
                for linha in aba.iter_rows(values_only=True):
                    partes.append(" ".join(str(v) for v in linha if v is not None))
            return "\n".join(partes), False

        if mime in MIME_IMAGEM:
            if len(conteudo) > MAX_BYTES_IMAGEM_OCR:
                return "", False
            return _ocr_imagem(conteudo), True

    except Exception as exc:  # nunca deixa 1 arquivo problemático travar o levantamento inteiro
        return f"[erro ao ler {arquivo.get('name')}: {exc}]", False

    return "", False


@dataclass
class ResultadoLevantamento:
    campos: dict = field(default_factory=dict)
    fontes: dict = field(default_factory=dict)  # campo -> nome do arquivo onde foi achado
    arquivos_lidos: list = field(default_factory=list)
    arquivos_ignorados: list = field(default_factory=list)
    erros: list = field(default_factory=list)
    # Linhas de equipamento (pedido de kit OU Projeto Executivo/Diagrama
    # Unifilar). MESMA regra do `campos` escalar: o primeiro arquivo em que
    # a lista de painéis (ou de inversores) aparece "ganha" — nunca soma
    # com o que um arquivo seguinte também encontrar. Antes disso, dois
    # arquivos da mesma pasta mencionando o mesmo painel físico (ex.: o
    # pedido do kit E o Projeto Executivo) faziam ele aparecer duas vezes
    # na tabela — `_sem_duplicatas` só pega repetição dentro do MESMO
    # arquivo (ex.: o mesmo trecho lido via texto vetorial e via OCR), não
    # o mesmo equipamento relatado por dois arquivos diferentes.
    paineis: list = field(default_factory=list)
    inversores: list = field(default_factory=list)
    fonte_paineis: str = ""
    fonte_inversores: str = ""
    # Print de mapa/satélite recortado automaticamente do Diagrama Unifilar
    # (ver `extrair_imagem_situacao_diagrama`) — usado pra pré-preencher a
    # "foto de localização" do Memorial Descritivo sem o usuário precisar
    # subir de novo uma imagem que ele já colou nesse outro arquivo. Mesma
    # regra de "primeiro arquivo que achar, ganha" dos demais campos.
    imagem_localizacao: bytes | None = None
    fonte_imagem_localizacao: str = ""


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
        if arquivo["mimeType"] not in MIME_TEXTO_EXTRAIVEL and arquivo["mimeType"] not in MIME_IMAGEM:
            resultado.arquivos_ignorados.append(nome)
            continue

        texto, via_ocr = baixar_texto_arquivo(servico, arquivo)
        if texto.startswith("[erro ao ler"):
            resultado.erros.append(texto)
            continue
        if not texto.strip():
            resultado.arquivos_ignorados.append(nome)
            continue

        texto = texto[:MAX_CARACTERES_POR_ARQUIVO]
        # Documento de fabricante do equipamento (registro Inmetro,
        # datasheet) — endereço/e-mail/telefone/CEP soltos aí são sempre da
        # fabricante, nunca do cliente, então os extratores de
        # endereço/contato/genérico NEM RODAM sobre esse texto. A extração
        # de equipamentos (logo abaixo) continua normal.
        eh_fabricante = eh_documento_fabricante_equipamento(texto)
        # "Cartão de contato" do cliente — o único arquivo de onde
        # e-mail/celular/coordenadas soltos podem vir (ver
        # `EXTRATORES_SO_CARTAO_CONTATO`). Identificado pelo tipo do
        # arquivo (Google Doc), não pelo conteúdo: é assim que esse
        # arquivo sempre chega na pasta do cliente.
        eh_cartao_contato = arquivo["mimeType"] == "application/vnd.google-apps.document"
        tag_fonte = " (fabricante do equipamento — não usado p/ endereço/contato)" if eh_fabricante else ""
        resultado.arquivos_lidos.append(f"{nome} (OCR){tag_fonte}" if via_ocr else f"{nome}{tag_fonte}")
        achados = {} if eh_fabricante else extrair_campos_arquivo(texto, tipo, eh_cartao_contato)
        for campo, valor in achados.items():
            if campo not in resultado.campos:
                resultado.campos[campo] = valor
                resultado.fontes[campo] = f"{nome} (OCR — confira com atenção redobrada)" if via_ocr else nome

        # As duas extrações de equipamento abaixo, ao contrário de
        # `extrair_campos_arquivo` (que já protege cada extrator
        # individualmente), não tinham essa proteção — um erro inesperado
        # aqui (num arquivo só) travava a função inteira e o Streamlit
        # descartava TUDO que já tinha sido lido dos arquivos anteriores
        # (campos incluídos), já que `painel_busca_drive` só salva o
        # resultado depois que `levantamento_pasta` retorna com sucesso.
        # Isolado por arquivo, do mesmo jeito, pra um problema aqui nunca
        # apagar os campos que já tinham sido reconhecidos certinho.
        #
        # E, assim como os campos escalares, o PRIMEIRO arquivo que achar a
        # lista de painéis (ou de inversores) ganha — não soma com o que um
        # arquivo seguinte também encontrar (isso duplicava o mesmo
        # equipamento físico quando, por exemplo, o pedido do kit e o
        # Projeto Executivo do mesmo cliente mencionavam o mesmo painel).
        # Por isso pula a chamada inteira assim que os dois já estiverem
        # preenchidos — nem gasta tempo tentando reconhecer de novo.
        if not resultado.paineis or not resultado.inversores:
            try:
                equipamentos = extrair_equipamentos_pedido(texto)
                if not resultado.paineis and equipamentos["paineis"]:
                    resultado.paineis = equipamentos["paineis"]
                    resultado.fonte_paineis = nome
                if not resultado.inversores and equipamentos["inversores"]:
                    resultado.inversores = equipamentos["inversores"]
                    resultado.fonte_inversores = nome
            except Exception as exc:
                resultado.erros.append(f"[erro ao reconhecer equipamentos de pedido em {nome}: {exc}]")

        if not resultado.paineis or not resultado.inversores:
            try:
                equipamentos_pe = extrair_equipamentos_projeto_executivo(texto)
                if not resultado.paineis and equipamentos_pe["paineis"]:
                    resultado.paineis = equipamentos_pe["paineis"]
                    resultado.fonte_paineis = nome
                if not resultado.inversores and equipamentos_pe["inversores"]:
                    resultado.inversores = equipamentos_pe["inversores"]
                    resultado.fonte_inversores = nome
            except Exception as exc:
                resultado.erros.append(f"[erro ao reconhecer equipamentos de projeto executivo em {nome}: {exc}]")

        # Print de mapa/satélite colado no Diagrama Unifilar — reaproveita
        # pra pré-preencher a "foto de localização" do Memorial Descritivo,
        # sem o usuário precisar subir de novo uma imagem que ele já tem
        # nesse outro arquivo. Só tenta em PDF (é onde essa imagem colada
        # existe) reconhecido pelo carimbo do Diagrama Unifilar, e só baixa
        # os bytes brutos do arquivo (2º download, só desse arquivo) quando
        # essas condições batem — não baixa à toa pra todo PDF da pasta.
        if (
            resultado.imagem_localizacao is None
            and arquivo["mimeType"] == "application/pdf"
            and _eh_diagrama_com_imagem_situacao(texto)
        ):
            try:
                conteudo_pdf = servico.files().get_media(fileId=arquivo["id"]).execute()
                imagem = extrair_imagem_situacao_diagrama(conteudo_pdf)
                if imagem:
                    resultado.imagem_localizacao = imagem
                    resultado.fonte_imagem_localizacao = nome
            except Exception as exc:
                resultado.erros.append(f"[erro ao recortar imagem de localização de {nome}: {exc}]")

    return resultado
