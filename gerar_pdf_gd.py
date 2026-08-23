#!/usr/bin/env python3
"""
Gera os arquivos finais (xlsm preenchido + PDF) a partir de um JSON de dados,
preservando 100% o arquivo original da Energisa (diagrama unifilar com
formas, macros, formatação) — nenhuma etapa usa openpyxl para salvar.

Uso:
    python3 gerar_pdf_gd.py dados_cliente.json memorialgd_1.xlsm saida_cliente

Gera:
    saida_cliente.xlsm  (planilha preenchida e recalculada - pode ser aberta
                         e conferida no Excel normalmente, com o diagrama
                         e as macros intactos)
    saida_cliente.pdf   (documento final pronto pra anexar no AWGPE)
"""
import sys
import json
import shutil
import subprocess
from pathlib import Path
from pypdf import PdfReader, PdfWriter

sys.path.insert(0, str(Path(__file__).parent))
from preencher_gd import preencher
from xml_cell_writer import restringir_impressao_para_pdf

sys.path.insert(0, "/mnt/skills/public/xlsx/scripts")
from office.soffice import run_soffice

# Abas que compõem o documento final entregue à Energisa, na ordem correta
PAGINAS_FINAIS = ["SOLICITACAO", "RELACAO DE CARGA", "FORMULARIO", "MD-SOLAR", "DU-SOLAR"]


def main(json_path, modelo_path, saida_base):
    xlsm_path = f"{saida_base}.xlsm"
    pdf_path = f"{saida_base}.pdf"

    with open(json_path, encoding="utf-8") as f:
        dados = json.load(f)

    # 1) Preenche a planilha editando o XML diretamente (preserva o diagrama
    #    e as macros do arquivo original da Energisa)
    preencher(dados, modelo_path, xlsm_path)

    # 2) Recalcula fórmulas (LibreOffice preserva os desenhos e o VBA)
    recalc = subprocess.run(
        [sys.executable, "/mnt/skills/public/xlsx/scripts/recalc.py", xlsm_path, "90"],
        capture_output=True, text=True
    )
    print("Recalc:", recalc.stdout.strip())

    # 3) Cópia só pra gerar o PDF: reduz a área de impressão das demais abas
    #    (Início, FSA, GD EXISTENTE, UC BENEFICIARIAS...) pra uma célula em
    #    branco, sem tocar nas abas em si nem nos desenhos.
    tmp_print = f"{saida_base}_print.xlsm"
    shutil.copyfile(xlsm_path, tmp_print)
    restringir_impressao_para_pdf(tmp_print, PAGINAS_FINAIS)

    # 4) Exporta em PDF
    outdir = str(Path(xlsm_path).resolve().parent)
    result = run_soffice([
        "--headless", "--convert-to", "pdf", "--outdir", outdir,
        str(Path(tmp_print).resolve())
    ])
    print("Conversão PDF:", result.returncode, result.stdout[-300:] if result.stdout else "")

    gerado = Path(tmp_print).with_suffix(".pdf")

    # 5) Remove páginas em branco residuais das abas suprimidas
    reader = PdfReader(str(gerado))
    writer = PdfWriter()
    for page in reader.pages:
        if page.extract_text().strip():
            writer.add_page(page)
    with open(pdf_path, "wb") as f:
        writer.write(f)

    gerado.unlink(missing_ok=True)
    Path(tmp_print).unlink(missing_ok=True)

    print(f"\nArquivos finais: {xlsm_path} / {pdf_path}")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Uso: python3 gerar_pdf_gd.py dados_cliente.json memorialgd_1.xlsm saida_cliente")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2], sys.argv[3])
