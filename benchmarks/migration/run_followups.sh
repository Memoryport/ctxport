#!/bin/bash
# Everything after the main Haiku run (contexts for S and L exist): more readers, the deletion test, a
# cross-family extractor. Each step is cached and resumable; a failed step does not stop the next one.
set -u
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
R=${R:-$HOME/.ctxport/research/synth}
KEY="none,recent@1400,bm25@1400,summary@1400,rolling@1400,chmd-det@1400,chmd-detT@1400,chmd-llm@1400,chmd-write@1400"
QWEN=ollama:qwen3.5-9b-uncensored-aggressive:latest
GPT=${GPT:-codex:gpt-6-luna}

LSUB=p00,p01,p02,p03,p04,p05,p06,p07
for D in ${SCALES:-S}; do
  P=""; [ $D = L ] && P="--personas $LSUB"
  echo "== $D: deletion"; $PY benchmarks/migration/deletion.py --data $R/$D --reader claude:haiku
done
for D in ${SCALES:-S}; do
  P=""; [ $D = L ] && P="--personas $LSUB"
  echo "== $D: GPT reader"; $PY benchmarks/migration/run_synth.py --data $R/$D $P --stage read --readers $GPT --methods $KEY --workers 2
done
for D in ${SCALES:-S}; do
  P=""; [ $D = L ] && P="--personas $LSUB"
  echo "== $D: Qwen reader"; $PY benchmarks/migration/run_synth.py --data $R/$D $P --stage read --readers $QWEN --methods $KEY --workers 1
done
# cross-family extractor on ten small personas (local model, slow)
SUB=p00,p01,p02,p03,p04,p05,p06,p07,p08,p09
echo "== S: Qwen extractor"; $PY benchmarks/migration/run_synth.py --data $R/S --extractor $QWEN --readers claude:haiku --personas $SUB --workers 1
echo "== done"
