# SP版 選手詳細 + 並び予想 パッチ

## 取得元
https://sp.oddspark.com/keirin/SpRaceInfo.do

## 追加される rider フィールド
車番, 枠番, 選手名, 地区, 年齢, 期, 級班, 競走得点, 脚質,
逃, 捲, 差, マ, S, H, B, ギア倍数,
直近着順{1着,2着,3着,着外}, 勝率, 2連対率, 3連対率, コメント

## entry 追加
lines, lines_detail, narabi_comment, sp_url

## import
- Race.lines_data
- Entry の s_count/h_count/b_count/kimarite_*/finish_*/app_*_rate/gear_ratio 等

## 検証
```bash
cd scraper
python -c "
from keirin_oddspark_scraper import parse_sp_race_info
print(parse_sp_race_info('21','20231022',12)['riders_sp'][1])
"
```

## 注意: 既存(過去)データについて
このパッチ適用前にスクレイプ済みのJSONファイルには、lines(並び予想)・S/H/B・
決まり手回数・着順履歴・勝率等の新フィールドは含まれていません。過去分で
これらを使いたい場合は、該当レースをこのパッチ適用後のスクレイパーで
再取得してください(出走表・オッズ・結果のうち、出走表だけ取り直しても
`merge_sp_into_entry`で反映されます)。
