# Gerador de Documentos GD — Exergia

App com tela (formulário) para gerar os documentos de Geração Distribuída
da Energisa-MT e da Equatorial-GO, sem precisar me passar os dados pelo
chat toda vez.

## O que o app faz

- **Energisa-MT**: preenche a planilha oficial (SOLICITAÇÃO, RELAÇÃO DE
  CARGA, FORMULÁRIO, MD-SOLAR, DU-SOLAR), recalcula, gera o PDF de 6
  páginas com diagrama nativo e o arquivo para a ANEEL.
- **Equatorial-GO**: preenche o Anexo I (com checagem automática de
  viabilidade), o Memorial Descritivo (com foto de localização, se você
  enviar) e o Relatório de Comissionamento — em Word e PDF.

Todas as regras e correções que validamos juntos ao longo dos projetos
(formato de tensão trifásica "220/380", CEP/CPF só dígitos, checagem de
Fast Track, etc.) já estão embutidas no app.

## Como rodar no seu computador (teste rápido)

1. Instale o [Python](https://www.python.org/downloads/) (3.10 ou mais novo) e o
   [LibreOffice](https://www.libreoffice.org/download/download/) (necessário para gerar os PDFs).
2. Abra um terminal na pasta do app e rode:
   ```
   pip install -r requirements.txt
   streamlit run app.py
   ```
3. Vai abrir automaticamente no navegador, em `http://localhost:8501`.

## Como publicar (acessar de qualquer lugar, pelo navegador)

Recomendo o **Streamlit Community Cloud** — é gratuito e leva uns 5 minutos:

1. Crie uma conta gratuita no [GitHub](https://github.com) (se ainda não tiver).
2. Crie um repositório novo (pode ser privado) e envie todos os arquivos desta pasta pra ele
   (o jeito mais fácil: no site do GitHub, "Add file" → "Upload files", arraste tudo).
3. Entre em [share.streamlit.io](https://share.streamlit.io) e faça login com sua conta do GitHub.
4. Clique em "New app", escolha o repositório que você criou, e em "Main file path" coloque `app.py`.
5. Clique em "Deploy". Na primeira vez demora alguns minutos (ele instala o LibreOffice).
6. Pronto — você recebe um link tipo `https://seu-app.streamlit.app`, acessível de qualquer
   navegador, celular incluso.

Se quiser deixar privado (só você acessar), no painel do Streamlit Cloud vá em
"Settings → Sharing" e restrinja o acesso ao seu e-mail.

## Estrutura de arquivos

```
app.py                          -> tela principal (Streamlit)
preencher_gd.py                 -> lógica de preenchimento Energisa-MT
xml_cell_writer.py              -> escrita direta no XML (preserva o diagrama)
recalc.py + office/soffice.py   -> recálculo de fórmulas via LibreOffice
preencher_equatorial.py         -> lógica de preenchimento Anexo I
preencher_docx_equatorial.py    -> lógica de preenchimento Memorial/Comissionamento
memorialgd_1.xlsm               -> modelo oficial Energisa-MT
NT_00020...xlsx                 -> modelo oficial Anexo I Equatorial-GO
Memorial_Descritivo_...docx     -> modelo de referência (Memorial)
Modelo_de_Relatorio_...docx     -> modelo de referência (Comissionamento)
requirements.txt / packages.txt -> dependências (Python / sistema)
```

## Limitações que continuam iguais ao processo manual

- As seções de **dimensionamento técnico** do Memorial Descritivo
  (disjuntor, DPS, aterramento, cabos, levantamento de carga) usam os
  valores de referência do modelo — revise antes de enviar.
- A **checagem de viabilidade** do Anexo I é automática, mas em caso de
  alerta, revise manualmente antes de enviar à Equatorial.
- Para módulos/inversores sem correspondência exata no catálogo da
  Energisa, use "^^" ou o código mais próximo — o app não escolhe isso
  sozinho.

## Manutenção

Se a Equatorial ou a Energisa mudarem o modelo oficial (nova revisão da
planilha/formulário), é só substituir o arquivo de modelo correspondente
nesta pasta (mantendo o mesmo nome) — o app usa sempre a versão mais
recente que estiver aqui.
