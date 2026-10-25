import json,urllib.request,time,base64,io
from PIL import Image
im=Image.new("RGB",(256,256),(220,20,20)); b=io.BytesIO(); im.save(b,"PNG")
u="data:image/png;base64,"+base64.b64encode(b.getvalue()).decode()
filler="The quick brown fox jumps over the lazy dog near the river bank. "*34000
body={"model":"w6","max_tokens":300,"temperature":0,"messages":[{"role":"user","content":[
 {"type":"text","text":filler+"\nThe secret word is MANGO-9921.\n"},
 {"type":"image_url","image_url":{"url":u}},
 {"type":"text","text":"What is the secret word, and what colour is the image? Answer briefly."}]}]}
t=time.time()
r=json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8080/v1/chat/completions",json.dumps(body).encode(),{"Content-Type":"application/json"}),timeout=1500))
print(round(time.time()-t),r["usage"],r["choices"][0]["message"]["content"][-200:])
