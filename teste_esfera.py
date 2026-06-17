"""
Teste de viabilidade: roda no GitHub Actions (IP de datacenter) pra ver se a
Esfera carrega de verdade ou se devolve bloqueio/captcha (anti-bot).
Nao envia Telegram, nao guarda estado. So abre as paginas e relata.
"""
import sys
import time

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

URLS = {
    "compra": "https://www.esfera.com.vc/p/compra-de-pontos/e000100033",
    "clube": "https://www.esfera.com.vc/clube",
}

opts = Options()
opts.add_argument("--headless=new")
opts.add_argument("--no-sandbox")
opts.add_argument("--disable-dev-shm-usage")
opts.add_argument("--window-size=1366,2200")
opts.add_argument("--lang=pt-BR")
opts.add_argument(
    "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

driver = webdriver.Chrome(options=opts)
driver.set_page_load_timeout(60)

SINAIS_BLOQUEIO = [
    "just a moment", "verifying you are human", "attention required",
    "cloudflare", "acesso negado", "access denied", "blocked", "captcha",
]

suspeito = False
for nome, url in URLS.items():
    print(f"\n===== {nome}: {url} =====", flush=True)
    try:
        driver.get(url)
    except Exception as e:
        print(f"[timeout/erro no get] {e}", flush=True)
        try:
            driver.execute_script("window.stop();")
        except Exception:
            pass
    time.sleep(6)
    try:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    except Exception:
        pass
    time.sleep(4)

    try:
        body = driver.find_element(By.TAG_NAME, "body").text
    except Exception:
        body = ""

    print(f"Titulo da pagina: {driver.title!r}", flush=True)
    print(f"Tamanho do texto renderizado: {len(body)} chars", flush=True)
    print("----- primeiros 600 chars -----", flush=True)
    print(body[:600], flush=True)

    low = (driver.title + " " + body[:1500]).lower()
    if any(s in low for s in SINAIS_BLOQUEIO):
        print(">>> SINAL DE ANTI-BOT / BLOQUEIO DETECTADO <<<", flush=True)
        suspeito = True
    if len(body) < 300:
        print(">>> POUCO CONTEUDO — pode ser bloqueio ou nao renderizou <<<", flush=True)
        suspeito = True

    try:
        driver.save_screenshot(f"print_{nome}.png")
    except Exception:
        pass

driver.quit()

print("\n===== RESULTADO =====", flush=True)
if suspeito:
    print("BLOQUEADO/SUSPEITO — a nuvem provavelmente NAO vai funcionar sem ajustes.", flush=True)
else:
    print("OK — as paginas renderizaram conteudo real do IP do GitHub.", flush=True)
