import http.server, functools, threading, urllib.request, time
h = functools.partial(http.server.SimpleHTTPRequestHandler, directory="/home/user/raspoznavatel/ui")
s = http.server.ThreadingHTTPServer(("127.0.0.1", 8799), h)
threading.Thread(target=s.serve_forever, daemon=True).start()
time.sleep(0.5)
try:
    r = urllib.request.urlopen("http://127.0.0.1:8799/index.html", timeout=5)
    print("index.html:", r.status, len(r.read()), "байт")
except Exception as e:
    print("ОШИБКА:", e)
s.shutdown()
