import json,statistics as s,collections,sys
d=collections.defaultdict(list); meta={}
for l in open('/srv/ai/var/dflash-eval/results.jsonl'):
    r=json.loads(l); meta[r['cfg']]=(r['idle_mib'],r['peak_mib'])
    if 'tg_s' in r or 'agg_tg_s' in r or 'dec_tg_s' in r or 'error' in r: d[(r['cfg'],r['test'])].append(r)
for k,v in d.items():
    if 'tg_s' in v[0]:
        acc=[x['draft_ok']/x['draft_n'] for x in v if x.get('draft_n')]
        print(f"{k[0]:18} {k[1]:10} tg {s.median(x['tg_s'] for x in v):6} pp {s.median(x['pp_s'] for x in v):7} acc {round(s.median(acc),2) if acc else '-'}")
    else:
        print(k, [{a:b for a,b in x.items() if a not in('cfg','test','idle_mib','peak_mib','load_s')} for x in v])
print(meta)
