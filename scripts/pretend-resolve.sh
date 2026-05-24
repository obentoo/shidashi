#!/usr/bin/env bash
# pretend-resolve.sh — esqueleto do fluxo seed → apply_portage → run --pretend (OVERVIEW §18.7).
#
# Fase 0: este script é um ESQUELETO documentado. As etapas que exigem um host
# Gentoo real (seed de stage3, bind dos portage_layers, systemd-nspawn, emerge)
# estão marcadas com TODO(phase-2) e ainda não executam de verdade. O objetivo
# da Fase 0 é fixar a FORMA do fluxo e passar `bash -n` na CI.
#
# Uso (forma alvo):
#   scripts/pretend-resolve.sh <arch> <flavor> <init>
#
# Saída alvo (fase 2): roda `emerge --pretend --emptytree @world` dentro de um
# systemd-nspawn com os portage_layers da receita sobrepostos, e extrai do output
# as sugestões de "circular"/"change USE" para alimentar a curadoria de use_break.

set -euo pipefail

readonly ARCH="${1:?uso: pretend-resolve.sh <arch> <flavor> <init>}"
readonly FLAVOR="${2:?uso: pretend-resolve.sh <arch> <flavor> <init>}"
readonly INIT="${3:?uso: pretend-resolve.sh <arch> <flavor> <init>}"

readonly SCRATCH="${KAJI_SCRATCH:-/var/tmp/kaji-pretend}"

log() { printf '[pretend-resolve] %s\n' "$*" >&2; }

# 1) seed — extrai um stage3 num diretório de scratch isolado.
seed_stage3() {
  log "seed: ${ARCH}×${FLAVOR}×${INIT} → ${SCRATCH}"
  # TODO(phase-2): detectar/baixar o stage3 mais recente e extraí-lo em ${SCRATCH}.
  #   - usar kaji.image/kaji.container quando implementados (OVERVIEW §7/§12)
  #   - validar checksum/assinatura do stage3
  :
}

# 2) apply_portage — sobrepõe os portage_layers da receita resolvida no scratch.
apply_portage() {
  log "apply_portage: sobrepondo portage_layers da receita resolvida"
  # A ordem das camadas vem de `kaji recipe show`:
  #   kaji recipe show "${ARCH}" "${FLAVOR}" "${INIT}" --format json | jq -r '.portage_layers[]'
  # TODO(phase-2): para cada layer (base→arch→flavor→init), bind-mount/copiar
  #   variants/<layer>/portage/ sobre ${SCRATCH}/etc/portage/.
  :
}

# 3) run_pretend — roda o emerge --pretend dentro do container e captura a saída.
run_pretend() {
  log "run --pretend: emerge --pretend --emptytree @world (em systemd-nspawn)"
  # TODO(phase-2): executar dentro de systemd-nspawn --directory="${SCRATCH}":
  #   emerge --pretend --emptytree @world
  # Capturar stdout+stderr para análise de ciclos abaixo.
  :
}

# 4) suggest_cycles — extrai sugestões de quebra de ciclo do output do emerge.
suggest_cycles() {
  local pretend_output="${1:-/dev/null}"
  log "analisando sugestões de ciclo / mudança de USE"
  # As linhas de interesse do emerge contêm "circular" ou "change USE";
  # elas alimentam a curadoria manual de Phase.use_break (OVERVIEW §18.3).
  grep -E 'circular|change USE' "${pretend_output}" || true
  # TODO(phase-2): mapear cada sugestão para o átomo+flag e emitir um patch de
  #   use_break para a phase correspondente.
}

main() {
  seed_stage3
  apply_portage
  run_pretend
  suggest_cycles "/dev/null"
  log "esqueleto concluído (Fase 0) — nenhuma resolução real executada"
}

main "$@"
