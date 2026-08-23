#!/usr/bin/env python3
"""
Escreve valores de célula diretamente no XML interno do .xlsm (sem passar
por openpyxl), preservando 100% os desenhos (formas do diagrama unifilar),
VBA e qualquer outra parte do arquivo que o openpyxl não sabe reconstruir.

Um arquivo .xlsm é um .zip contendo, entre outras coisas:
    xl/workbook.xml            -> lista de abas e seus r:id
    xl/_rels/workbook.xml.rels -> mapeia r:id -> xl/worksheets/sheetN.xml
    xl/worksheets/sheetN.xml   -> as células da aba (o que editamos aqui)
    xl/drawings/...            -> as formas/desenhos (não tocamos)
    xl/vbaProject.bin          -> as macros (não tocamos)
"""
import re
import shutil
import zipfile
from lxml import etree

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NSMAP = {None: NS}


def _col_letters(coord: str) -> str:
    return re.match(r"([A-Z]+)(\d+)", coord).group(1)


def _row_num(coord: str) -> int:
    return int(re.match(r"([A-Z]+)(\d+)", coord).group(2))


def _col_to_num(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n


class SheetMap:
    """Descobre qual worksheets/sheetN.xml corresponde a cada nome de aba."""

    def __init__(self, xlsm_path: str):
        with zipfile.ZipFile(xlsm_path) as z:
            wb_xml = z.read("xl/workbook.xml")
            rels_xml = z.read("xl/_rels/workbook.xml.rels")

        wb_root = etree.fromstring(wb_xml)
        rels_root = etree.fromstring(rels_xml)

        r_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        pkg_ns = "http://schemas.openxmlformats.org/package/2006/relationships"

        rid_to_target = {}
        for rel in rels_root:
            rid_to_target[rel.get("Id")] = rel.get("Target")

        self.name_to_path = {}
        for sheet in wb_root.find(f"{{{NS}}}sheets"):
            name = sheet.get("name")
            rid = sheet.get(f"{{{r_ns}}}id")
            target = rid_to_target[rid]
            self.name_to_path[name] = "xl/" + target if not target.startswith("xl/") else target

    def path_for(self, sheet_name: str) -> str:
        return self.name_to_path[sheet_name]


def set_cell(sheetdata_el, coord: str, value):
    """Cria/edita a célula `coord` dentro de <sheetData>, mantendo a ordem
    correta de linhas e colunas (exigida pelo formato OOXML)."""
    row_num = _row_num(coord)
    col_letters = _col_letters(coord)
    col_num = _col_to_num(col_letters)

    # localizar ou criar a <row>
    row_el = None
    insert_before_row = None
    for row in sheetdata_el:
        r = int(row.get("r"))
        if r == row_num:
            row_el = row
            break
        if r > row_num:
            insert_before_row = row
            break
    if row_el is None:
        row_el = etree.Element(f"{{{NS}}}row", r=str(row_num))
        if insert_before_row is not None:
            insert_before_row.addprevious(row_el)
        else:
            sheetdata_el.append(row_el)

    # localizar ou criar a <c>
    cell_el = None
    insert_before_cell = None
    for cell in row_el:
        c_col = _col_to_num(_col_letters(cell.get("r")))
        if c_col == col_num:
            cell_el = cell
            break
        if c_col > col_num:
            insert_before_cell = cell
            break
    if cell_el is None:
        cell_el = etree.Element(f"{{{NS}}}c", r=coord)
        if insert_before_cell is not None:
            insert_before_cell.addprevious(cell_el)
        else:
            row_el.append(cell_el)

    # limpar conteúdo anterior (mas manter o atributo de estilo "s", se houver)
    for child in list(cell_el):
        cell_el.remove(child)
    if "t" in cell_el.attrib:
        del cell_el.attrib["t"]

    if value is None or value == "":
        return  # célula fica vazia (só com estilo, se tinha)

    if isinstance(value, bool):
        cell_el.set("t", "b")
        v = etree.SubElement(cell_el, f"{{{NS}}}v")
        v.text = "1" if value else "0"
    elif isinstance(value, (int, float)):
        v = etree.SubElement(cell_el, f"{{{NS}}}v")
        v.text = str(value)
    else:
        cell_el.set("t", "inlineStr")
        is_el = etree.SubElement(cell_el, f"{{{NS}}}is")
        t_el = etree.SubElement(is_el, f"{{{NS}}}t")
        t_el.text = str(value)
        t_el.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")


def preencher_xlsm(modelo_path: str, saida_path: str, valores_por_aba: dict):
    """
    valores_por_aba: {"NOME DA ABA": {"C7": "valor", "D15": 15, ...}, ...}
    """
    shutil.copyfile(modelo_path, saida_path)
    sm = SheetMap(saida_path)

    # ler o zip inteiro em memória, trocar só os sheetN.xml necessários
    with zipfile.ZipFile(saida_path) as z:
        items = {name: z.read(name) for name in z.namelist()}
        infos = {name: info for name, info in ((i.filename, i) for i in z.infolist())}

    for aba, celulas in valores_por_aba.items():
        path = sm.path_for(aba)
        root = etree.fromstring(items[path])
        sheetdata = root.find(f"{{{NS}}}sheetData")
        for coord, value in celulas.items():
            set_cell(sheetdata, coord, value)
        items[path] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)

    with zipfile.ZipFile(saida_path, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, data in items.items():
            zout.writestr(infos[name], data)


def restringir_impressao_para_pdf(xlsm_path: str, abas_a_manter: list):
    """
    Para gerar o PDF final só com as páginas certas (sem tocar nas abas em
    si), reduz a área de impressão de todas as OUTRAS abas que têm uma
    _xlnm.Print_Area definida para uma única célula em branco. Isso evita
    que o LibreOffice inclua, na exportação, abas como "Início", as FSA,
    "GD EXISTENTE" ou "UC BENEFICIARIAS" que não fazem parte do documento
    final entregue à Energisa.
    """
    with zipfile.ZipFile(xlsm_path) as z:
        wb_xml = z.read("xl/workbook.xml")
        others = {n: z.read(n) for n in z.namelist() if n != "xl/workbook.xml"}
        infos = {i.filename: i for i in z.infolist()}

    root = etree.fromstring(wb_xml)
    sheets_el = root.find(f"{{{NS}}}sheets")
    nomes_por_indice = [s.get("name") for s in sheets_el]

    # Esconde qualquer aba fora da lista final que esteja visível e sem área
    # de impressão definida (ex: "Ativar Macros") - senão o LibreOffice
    # exporta a aba inteira usando quebra de página automática.
    for sheet_el in sheets_el:
        if sheet_el.get("name") not in abas_a_manter and sheet_el.get("state") != "hidden":
            sheet_el.set("state", "hidden")

    defined_names = root.find(f"{{{NS}}}definedNames")
    cobertas = set()
    for dn in defined_names.findall(f"{{{NS}}}definedName"):
        if dn.get("name") != "_xlnm.Print_Area":
            continue
        idx = int(dn.get("localSheetId"))
        nome_aba = nomes_por_indice[idx]
        cobertas.add(nome_aba)
        if nome_aba not in abas_a_manter:
            dn.text = f"'{nome_aba}'!$ZZ$9000"

    # Abas sem NENHUMA área de impressão definida (ex: "Ativar Macros",
    # CONFIG, SAIDA, INVERSOR-MODULO) também precisam de uma, senão o
    # LibreOffice às vezes as inclui mesmo ocultas.
    for idx, nome_aba in enumerate(nomes_por_indice):
        if nome_aba in abas_a_manter or nome_aba in cobertas:
            continue
        novo = etree.SubElement(defined_names, f"{{{NS}}}definedName")
        novo.set("name", "_xlnm.Print_Area")
        novo.set("localSheetId", str(idx))
        novo.text = f"'{nome_aba}'!$ZZ$9000"

    wb_xml_novo = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)

    with zipfile.ZipFile(xlsm_path, "w", zipfile.ZIP_DEFLATED) as zout:
        zout.writestr(infos["xl/workbook.xml"], wb_xml_novo)
        for name, data in others.items():
            zout.writestr(infos[name], data)
