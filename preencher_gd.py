#!/usr/bin/env python3
"""
Preenchimento automático da planilha memorialgd (Energisa - Orçamento de Conexão GD).

Diferente da primeira versão, este script NÃO usa openpyxl para salvar o
arquivo (o openpyxl apaga as formas do diagrama unifilar e pode corromper
partes do arquivo que ele não entende). Em vez disso, edita o XML interno
do .xlsm diretamente, célula por célula, preservando 100% o restante do
arquivo (diagrama, macros, formatação).

Uso:
    python3 preencher_gd.py dados_cliente.json modelo_memorialgd.xlsm saida.xlsm
"""
import sys
import json
import re
from xml_cell_writer import preencher_xlsm

LINHA_INICIAL_CARGA = 16
MAX_CARGAS = 20
LINHA_INICIAL_PAINEL = 31
MAX_PAINEIS = 10
LINHA_INICIAL_INVERSOR = 46
MAX_INVERSORES = 10


def montar_celulas(dados: dict) -> dict:
    cliente = dados["cliente"]
    resp = dados["responsavel_tecnico"]

    # O campo CEP usa formatação numérica "00000-000": o traço é decorativo,
    # o valor por trás tem que ser só dígitos (senão a conversão numérica
    # que a Energisa usa em outras abas, como o CEP no Diagrama Unifilar,
    # quebra e fica em branco).
    cep_digitos = re.sub(r"\D", "", str(cliente["cep"]))
    cep_valor = int(cep_digitos) if cep_digitos else ""
    cpf_digitos = re.sub(r"\D", "", str(cliente["cpf_cnpj"]))
    cpf_valor = int(cpf_digitos) if cpf_digitos else ""

    solicitacao = {
        "C7": cliente["uc"],
        "I7": cliente["classe"],
        "C8": cliente["titular"],
        "C9": cliente["logradouro"],
        "C10": cliente["numero"],
        "E10": cliente["bairro"],
        "I10": cliente["uf"],
        "K10": cep_valor,
        "C11": cliente["email"],
        "I11": cliente["cidade"],
        "C12": cliente.get("telefone", ""),
        "I12": cliente["celular"],
        "C13": cpf_valor,
        "D15": cliente["potencia_instalada_kw"],
        "J15": cliente["tensao_atendimento_v"],
        "D16": cliente["tipo_conexao"],
        "D17": cliente["tipo_ramal"],
        "D20": cliente["tipo_fonte_geracao"],
        "H20": cliente["tipo_geracao"],
        "D26": resp["nome"],
        "C27": resp["telefone"],
        "G27": resp["email"],
    }

    relacao_carga = {}
    for i, item in enumerate(cliente.get("cargas", [])[:MAX_CARGAS]):
        r = LINHA_INICIAL_CARGA + i
        relacao_carga[f"B{r}"] = item["quantidade"]
        relacao_carga[f"C{r}"] = item["equipamento"]
        relacao_carga[f"F{r}"] = item["pot_unitaria_w"]
        relacao_carga[f"H{r}"] = item["fator_demanda"]

    obs_paineis = "; ".join(
        f'{p.get("quantidade","")}x {p.get("fabricante","")} {p.get("modelo","")}'
        for p in cliente.get("paineis", [])
    )
    obs_inversores = "; ".join(
        f'{i.get("quantidade","")}x {i.get("fabricante","")} {i.get("modelo","")}'
        for i in cliente.get("inversores", [])
    )

    md_solar = {
        "B15": cliente["disjuntor_geral_a"],
        "D15": cliente["fator_potencia"],
        "G15": cliente.get("demanda_contratada_kw", 0),
        "I16": cliente["dps_ca_ka"],
        "J16": cliente["disjuntor_ca_a"],
        "K16": cliente["dps_cc_ka"],
        "L16": cliente["disjuntor_cc_a"],
        "B17": cliente["modalidade"],
        "D17": (str(cliente["potencia_trafo"]).replace(".", ",") if cliente.get("potencia_trafo") not in ("", None) else ""),
        "E17": cliente.get("numero_hastes", ""),
        "G17": cliente.get("demanda_contratada_kwg", 0),
        "F19": cliente["fuso"],
        "H19": cliente["coord_x"],
        "J19": cliente["coord_y"],
        "B22": cliente["tipo_tensao"],
        "C22": cliente["cabos_por_fase"],
        "E22": cliente["bitola_fase"],
        "F22": cliente["bitola_neutro"],
        "G22": cliente["bitola_terra"],
        "H22": cliente["gd_ja_instalado"],
        "I22": cliente["mes_previsao_ligacao"],
        "J22": cliente["ano_previsao_ligacao"],
        "K22": cliente["zona"],
        "C23": "",
        "C24": cliente.get("observacoes", obs_paineis) + "\n" + cliente.get("observacoes_2", obs_inversores),
        "J58": cliente.get("necessita_autotrafo", "NÃO"),
        "J59": cliente.get("potencia_autotrafo", ""),
        "J61": cliente.get("trafo_exclusivo", "NÃO"),
        "J62": cliente.get("potencia_trafo_exclusivo", ""),
    }
    for i, p in enumerate(cliente.get("paineis", [])[:MAX_PAINEIS]):
        r = LINHA_INICIAL_PAINEL + i
        md_solar[f"C{r}"] = p["quantidade"]
        md_solar[f"D{r}"] = p["fabricante"]
        md_solar[f"G{r}"] = p["modelo"]
        md_solar[f"J{r}"] = p["area_m2"]
        md_solar[f"K{r}"] = p["potencia_kw"]
    for i, inv in enumerate(cliente.get("inversores", [])[:MAX_INVERSORES]):
        r = LINHA_INICIAL_INVERSOR + i
        md_solar[f"C{r}"] = inv["quantidade"]
        md_solar[f"D{r}"] = inv["fabricante"]
        md_solar[f"G{r}"] = inv["modelo"]
        md_solar[f"J{r}"] = inv["potencia_kw"]
        md_solar[f"L{r}"] = inv["tensao_nominal_v"]

    # Checklist fixo da aba FORMULARIO
    formulario = {}
    checklist = dados.get("formulario_checklist", {})
    for r in list(range(6, 16)) + list(range(17, 28)):
        formulario[f"K{r}"] = "X"
    formulario["K29"] = checklist.get("vistoria_apos_aprovacao", "NÃO")
    formulario["K30"] = checklist.get("renuncia_desistencia", "")
    formulario["K31"] = "X"
    formulario["K32"] = "X"

    return {
        "SOLICITACAO": solicitacao,
        "RELACAO DE CARGA": relacao_carga,
        "MD-SOLAR": md_solar,
        "FORMULARIO": formulario,
    }


def preencher(dados: dict, modelo_path: str, saida_path: str):
    valores_por_aba = montar_celulas(dados)
    preencher_xlsm(modelo_path, saida_path, valores_por_aba)
    print(f"Planilha preenchida salva em: {saida_path}")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Uso: python3 preencher_gd.py dados_cliente.json modelo.xlsm saida.xlsm")
        sys.exit(1)
    with open(sys.argv[1], encoding="utf-8") as f:
        dados = json.load(f)
    preencher(dados, sys.argv[2], sys.argv[3])
