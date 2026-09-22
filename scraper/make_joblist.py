
"""joblist generator with --after-now"""
from __future__ import annotations
import pathlib
import argparse, re, sys, time, pathlib
from datetime import datetime
from zoneinfo import ZoneInfo
import requests

JST = ZoneInfo('Asia/Tokyo')
HEADERS = {'User-Agent': 'Mozilla/5.0'}

def fetch_jos(date):
    r = requests.get('https://www.oddspark.com/keirin/RaceListInfo.do',
                     params={'kaisaiBi': date}, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return sorted(set(re.findall(r'joCode=(\d+)', r.text)))

def fetch_race_post_times(jo, date):
    try:
        r = requests.get('https://www.oddspark.com/keirin/AllRaceList.do',
                         params={'joCode': jo, 'kaisaiBi': date}, headers=HEADERS, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f'  WARN jo={jo} AllRaceList fail: {e}', file=sys.stderr)
        return {}
    text = r.text.replace('&nbsp;', ' ').replace('&#160;', ' ').replace('&amp;', '&')
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    pairs = re.findall(r'第(\d+)R[^第]*?発走時間\s*(\d{1,2}:\d{2})', text)
    out = {}
    for rn_s, hhmm in pairs:
        try:
            rn = int(rn_s)
        except ValueError:
            continue
        if 1 <= rn <= 12:
            out[rn] = hhmm
    return out

def hhmm_to_minutes(hhmm):
    hh, mm = map(int, hhmm.split(':'))
    return hh * 60 + mm

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dates', required=True)
    ap.add_argument('--out', default='joblist.txt')
    ap.add_argument('--after-now', action='store_true')
    args = ap.parse_args()
    now = datetime.now(JST)
    now_min = now.hour * 60 + now.minute
    today_str = now.strftime('%Y%m%d')
    jobs = []
    for d in [x.strip() for x in args.dates.split(',') if x.strip()]:
        jos = fetch_jos(d)
        print(d, jos, f'now={now.isoformat()}' if args.after_now else '')
        for jo in jos:
            if args.after_now:
                if d < today_str:
                    print(f'  SKIP jo={jo}: past date')
                    continue
                times = fetch_race_post_times(jo, d)
                time.sleep(0.4)
                if not times:
                    print(f'  WARN jo={jo}: no times -> add 1-12')
                    for rn in range(1, 13):
                        jobs.append(f'{d} {jo} {rn}')
                    continue
                kept = 0
                for rn in sorted(times):
                    if d > today_str or hhmm_to_minutes(times[rn]) >= now_min:
                        jobs.append(f'{d} {jo} {rn}')
                        kept += 1
                    else:
                        print(f'  skip jo={jo} R{rn} {times[rn]}')
                print(f'  jo={jo} kept={kept}/{len(times)} after {now.strftime("%H:%M")}')
            else:
                for rn in range(1, 13):
                    jobs.append(f'{d} {jo} {rn}')
    pathlib.Path(args.out).write_text('\n'.join(jobs) + ('\n' if jobs else ''), encoding='utf-8')
    print(f'total_jobs={len(jobs)} -> {args.out}')

if __name__ == '__main__':
    main()
