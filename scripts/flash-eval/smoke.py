import json,urllib.request,sys
U="http://127.0.0.1:%s/v1/chat/completions"%(sys.argv[1] if len(sys.argv)>1 else "8080")
def c(body):
    r=urllib.request.Request(U,json.dumps({"model":__import__("os").environ.get("BENCH_MODEL","x"),"temperature":0,**body}).encode(),{"Content-Type":"application/json"})
    return json.load(urllib.request.urlopen(r,timeout=600))
def m(d): return d["choices"][0]["message"]; 
def f(d): return d["choices"][0]["finish_reason"]
q=[{"role":"user","content":"What is 12*13? Answer briefly."}]
d=c({"messages":q,"max_tokens":600,"chat_template_kwargs":{"enable_thinking":False}}); print("think-off:",repr(m(d).get("content"))[:80],"reasoning:",bool(m(d).get("reasoning_content")),f(d))
d=c({"messages":q,"max_tokens":1500,"chat_template_kwargs":{"enable_thinking":True}}); print("think-on:",repr(m(d).get("content"))[:80],"reasoning_len:",len(m(d).get("reasoning_content") or ""),f(d))
d=c({"messages":q,"max_tokens":600,"reasoning_effort":"medium"}); print("effort-medium:",repr(m(d).get("content"))[:60],len(m(d).get("reasoning_content") or ""),f(d))
tools=[{"type":"function","function":{"name":"get_weather","description":"Weather for a city","parameters":{"type":"object","properties":{"city":{"type":"string"},"unit":{"type":"string","enum":["c","f"]}},"required":["city"]}}}]
d=c({"messages":[{"role":"user","content":"What's the weather in Paris in celsius?"}],"tools":tools,"max_tokens":800,"chat_template_kwargs":{"enable_thinking":False}}); print("tools:",m(d).get("tool_calls"),f(d))
d=c({"messages":[{"role":"user","content":"Return a JSON object with keys name and age for a fictional person."}],"response_format":{"type":"json_object"},"max_tokens":200,"chat_template_kwargs":{"enable_thinking":False}}); 
try: json.loads(m(d)["content"]); ok=True
except Exception as e: ok=False
print("json_mode valid:",ok,repr(m(d)["content"])[:80])
d=c({"messages":[{"role":"user","content":"Count from 1 to 20 separated by commas."}],"stop":["7"],"max_tokens":100,"chat_template_kwargs":{"enable_thinking":False}}); print("stop:",repr(m(d)["content"]),f(d))
d=c({"messages":[{"role":"user","content":"Write a long essay about the sea."}],"max_tokens":20,"chat_template_kwargs":{"enable_thinking":False}}); print("length:",f(d))
d=c({"messages":[{"role":"user","content":"Repeat exactly this text and nothing else: abc </think> def"}],"max_tokens":200,"chat_template_kwargs":{"enable_thinking":False}}); print("</think> literal:",repr(m(d).get("content")))
