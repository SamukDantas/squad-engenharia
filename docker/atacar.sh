#!/bin/sh
# Orquestra o toolset contra $ALVO e grava relatórios em /out para o nó
# parsear. Cada ferramenta é isolada com `|| true`: uma falhar (ex.: alvo sem
# formulário para o sqlmap) não pode abortar as outras — o veredito vem do
# conjunto de relatórios, não do exit code deste script.
set -u
ALVO="${ALVO:?ALVO nao definido}"
ZAP="${ZAP:-0}"
echo ">>> atacar.sh alvo=$ALVO zap=$ZAP"

# nuclei: CVEs, misconfig, exposições. JSONL com severidade por linha — é a
# fonte principal de severidade do veredito.
echo ">>> nuclei..."
nuclei -u "$ALVO" -jsonl -o /out/nuclei.jsonl -silent \
    -severity low,medium,high,critical || true

# nikto: misconfig de servidor web (headers, métodos, arquivos expostos).
echo ">>> nikto..."
perl /opt/nikto/program/nikto.pl -h "$ALVO" -maxtime 120s \
    -output /out/nikto.txt -Format txt || true

# ffuf: descoberta de endpoints não documentados. Wordlist empacotada.
echo ">>> ffuf..."
ffuf -u "$ALVO/FUZZ" -w /opt/wordlist.txt -mc 200,301,302,401,403 \
    -o /out/ffuf.json -of json -s || true

# sqlmap: SQLi. Sem alvo específico de formulário, faz crawl raso e testa o que
# achar — barulhento, então cabe no fim e com teto de tempo.
echo ">>> sqlmap..."
python3 /opt/sqlmap/sqlmap.py -u "$ALVO" --crawl=1 --batch --level=1 --risk=1 \
    --timeout=10 --retries=1 2>&1 | tee /out/sqlmap.txt || true

# ZAP baseline: opcional, pesado.
if [ "$ZAP" = "1" ] && command -v zap-baseline.py >/dev/null 2>&1; then
    echo ">>> zap baseline..."
    zap-baseline.py -t "$ALVO" -J /out/zap.json || true
fi

echo ">>> atacar.sh concluido"
