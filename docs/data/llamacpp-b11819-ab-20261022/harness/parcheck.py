import json, re, sys, time, urllib.request, importlib.util, concurrent.futures as cf, collections
spec=importlib.util.spec_from_file_location('tc','/srv/ai/tmp/lcpp-test/toolcall-suite.py'); tc=importlib.util.module_from_spec(spec); spec.loader.exec_module(tc)
def call(path, body, timeout=300):
    req=urllib.request.Request('http://127.0.0.1:9090'+path, json.dumps(body).encode(), {'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r: return json.loads(r.read())
PROMPTS={"weather":"Check the weather in Tokyo (celsius) and in Denver (fahrenheit).",
         "time":"What time is it right now in America/New_York and in Europe/Berlin?"}
MARK={"chat":r"<tool_call>", "coding":r"<tool_call>", "small":None}
def run(model, n):
    out=collections.defaultdict(lambda: collections.defaultdict(list))
    for name, text in PROMPTS.items():
        msgs=[{"role":"user","content":text}]
        prompt=call(f'/upstream/{model}/apply-template', {"messages":msgs,"tools":tc.TOOLS})["prompt"]
        for i in range(n):
            r=call(f'/upstream/{model}/completion', {"prompt":prompt,"n_predict":1500,"temperature":0.6,"top_p":0.95,"top_k":20,"cache_prompt":True})
            raw=r["content"]
            if i==0 and name=="weather": print(model, "RAW SAMPLE:", repr(raw[-420:]), flush=True)
            m=MARK[model]
            out["raw"][name].append(len(re.findall(m, raw)) if m else None)
            r=call('/v1/chat/completions', {"model":model,"messages":msgs,"tools":tc.TOOLS,"max_tokens":1500,"temperature":0.6,"top_p":0.95,"top_k":20})
            out["parsed"][name].append(len(r["choices"][0]["message"].get("tool_calls") or []))
    return model, out
n=int(sys.argv[1]); models=sys.argv[2:]
with cf.ThreadPoolExecutor(len(models)) as ex:
    for fut in [ex.submit(run,m,n) for m in models]:
        m,o=fut.result()
        for path in ("raw","parsed"):
            for name in PROMPTS:
                v=o[path][name]; print(m, path, name, dict(collections.Counter(v)))
