#!/usr/bin/env bash
# backfill.yml'in bir işi: DAYS'teki günleri sırayla işler ve hedef deponun arşivine yükler.
# Girdi (ortam): DAYS (boşlukla ayrılmış günler), TARGET_REPO, GH_TOKEN.
# Bir günün hatası diğerlerini durdurmaz; sonunda hata varsa iş kırmızı biter (yeniden
# başlatılınca tamamlanan günler atlanır).
set -uo pipefail

: "${DAYS:?}" "${TARGET_REPO:?}"
BUDGET_S=$((300 * 60)) # iş sınırı 340 dk; bundan sonra yeni gün başlatma

done_days=() missing=() failed=() left=()
for day in $DAYS; do
  if [ "$SECONDS" -ge "$BUDGET_S" ]; then
    left+=("$day")
    continue
  fi
  echo "::group::$day"
  uv run mz-detect daily --date "$day" --out data/archive --cache data/cache
  rc=$?
  if [ "$rc" -eq 0 ]; then
    # Yayın kapısı: boş saatlik özet (0 satır) arşive yüklenmez; archive-publish bundan sonra.
    if ! uv run mz-detect check-contract --date "$day" --archive data/archive; then
      failed+=("$day (sözleşme)")
    elif uv run mz-ingest archive-publish --date "$day" --archive data/archive --repo "$TARGET_REPO"; then
      done_days+=("$day")
    else
      failed+=("$day (yükleme)")
    fi
  elif [ "$rc" -eq 3 ]; then
    missing+=("$day") # adsb.lol'da bu günün arşivi yok
  else
    failed+=("$day (çıkış $rc)")
  fi
  echo "::endgroup::"
  rm -rf data/archive data/cache
done

{
  echo "### ${DAYS%% *} …"
  echo "- İşlendi: ${done_days[*]:-yok}"
  echo "- Kaynakta yok: ${missing[*]:-yok}"
  echo "- Hata: ${failed[*]:-yok}"
  echo "- Süre yetmedi (yeniden başlatınca işlenir): ${left[*]:-yok}"
} >>"${GITHUB_STEP_SUMMARY:-/dev/stdout}"

for d in "${missing[@]}"; do echo "::notice::$d: adsb.lol arşivinde yok"; done
if [ "${#failed[@]}" -gt 0 ]; then
  echo "::error::başarısız günler: ${failed[*]}"
  exit 1
fi
