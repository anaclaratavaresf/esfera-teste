#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Esfera Monitor — vigia promoções de compra de pontos e do Clube Esfera
e avisa no seu Telegram quando aparece bônus/desconto acima de um limite.

Como funciona (resumo):
1. Abre o Chrome via Selenium (a página da Esfera é Next.js / renderizada por JS,
   então requests simples NÃO funciona — precisa de navegador real).
2. (Opcional) Faz login com CPF/senha lidos do .env.
3. Visita a página de compra de pontos e a página do Clube.
4. Varre o texto visível procurando percentuais (>= LIMITE) perto de gatilhos
   como "bônus", "desconto", "em dobro", "em triplo".
5. Se achar algo novo, manda mensagem no Telegram. Guarda o que já avisou pra
   não te encher de mensagem repetida.

Toda a configuração fica no arquivo .env (veja .env.example).

Autor: gerado para Ana Clara.
"""

import os
import re
import sys
import json
import time
import html
import logging
import hashlib
import unicodedata
from pathlib import Path
from datetime import datetime

import requests

# python-dotenv é opcional; se não tiver, lemos variáveis de ambiente normais.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ----------------------------------------------------------------------------
# Configuração (lida do .env / ambiente)
# ----------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

ESFERA_CPF = os.getenv("ESFERA_CPF", "").strip()
ESFERA_SENHA = os.getenv("ESFERA_SENHA", "").strip()

# Limite mínimo de bônus/desconto (em %) para considerar "vale a pena avisar".
THRESHOLD = int(os.getenv("THRESHOLD_PERCENT", "50"))

# Rodar sem abrir janela do Chrome? (True = invisível). Para login com 2FA,
# deixe False na primeira vez.
HEADLESS = os.getenv("HEADLESS", "true").strip().lower() in ("1", "true", "yes", "sim")

# Reusar um perfil do Chrome já logado (plano B se o login automático for
# bloqueado). Aponte para uma pasta sua; faça login manual uma vez.
CHROME_PROFILE_DIR = os.getenv("CHROME_PROFILE_DIR", "").strip()

# Usar undetected-chromedriver pra disfarçar o Selenium (recomendado no site
# da Esfera, que é do grupo Santander e costuma detectar automação).
USE_UNDETECTED = os.getenv("USE_UNDETECTED", "true").strip().lower() in ("1", "true", "yes", "sim")

# Avisar mesmo se a promoção já tiver sido avisada antes? (padrão: não)
ALWAYS_NOTIFY = os.getenv("ALWAYS_NOTIFY", "false").strip().lower() in ("1", "true", "yes", "sim")

# --- Monitor visual de banner (print da faixa de topo + dHash) ---
# Detecta campanha que vem só como IMAGEM (sem texto). Tira print do topo da
# página, calcula um hash perceptual e avisa com a foto quando o banner muda.
MONITOR_BANNER = os.getenv("MONITOR_BANNER", "true").strip().lower() in ("1", "true", "yes", "sim")
# Em quais páginas capturar o banner (nomes iguais aos de URLS, separados por vírgula).
BANNER_PAGES = [p.strip() for p in os.getenv("BANNER_PAGES", "Compra de Pontos").split(",") if p.strip()]
# Altura (px) da faixa de topo capturada. Ajuste depois de ver a imagem salva.
BANNER_CROP_HEIGHT = int(os.getenv("BANNER_CROP_HEIGHT", "700"))
# Distância de Hamming do dHash acima da qual consideramos "mudou de verdade".
DHASH_THRESHOLD = int(os.getenv("DHASH_THRESHOLD", "8"))

# Páginas monitoradas
URLS = {
    "Compra de Pontos": "https://www.esfera.com.vc/p/compra-de-pontos/e000100033",
    "Clube Esfera": "https://www.esfera.com.vc/clube",
}
LOGIN_URL = "https://www.esfera.com.vc/login"

STATE_FILE = BASE_DIR / "esfera_state.json"
LOG_FILE = BASE_DIR / "esfera_monitor.log"

# ----------------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("esfera")


# ----------------------------------------------------------------------------
# Detecção de promoções
# ----------------------------------------------------------------------------
# Gatilhos que indicam promoção/campanha real de pontos.
PROMO_KEYWORDS = [
    "bonus", "bônus", "desconto", "off", "promocao", "promoção",
    "em dobro", "dobro de pontos", "pontos em dobro",
    "em triplo", "triplo de pontos", "pontos em triplo",
    "cashback", "compre e ganhe", "pontos extras", "ganhe ate", "ganhe até",
]

# Frases que valem como percentual fixo mesmo sem número.
SPECIAL_PHRASES = {
    "pontos em dobro": 100,
    "dobro de pontos": 100,
    "em dobro": 100,
    "pontos em triplo": 200,
    "triplo de pontos": 200,
    "em triplo": 200,
}

# Ruído fixo (benefícios permanentes do Clube, não são campanha).
NOISE_SNIPPETS = [
    "10% off em produtos e 5% off em viagens",
    "10% off em produtos",
    "5% off em viagens",
]

# Benefício PERMANENTE de plano do Clube: "X% OFF/desconto na compra de pontos".
# Aparece nos cards de plano (Pro 20%, VIP/Master 40%, Exclusive 50%). NÃO é
# campanha — só ignoramos isso na página do Clube (ver ignore_plan_discounts).
PLAN_DISCOUNT_RE = re.compile(r"\d{1,3}\s*%\s*(off|de\s+desconto)\s+na\s+compra\s+de\s+pontos")


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )


def _normalize(text: str) -> str:
    """Minúsculas, sem acento, espaços colapsados — pra busca robusta."""
    text = _strip_accents(text.lower())
    text = re.sub(r"\s+", " ", text)
    return text


def _matched_keyword(norm_line: str):
    return next((k for k in PROMO_KEYWORDS if _strip_accents(k) in norm_line), None)


def find_promotions(raw_text: str, threshold: int = THRESHOLD,
                    ignore_plan_discounts: bool = False):
    """
    Procura promoções relevantes, linha a linha (o texto vem com quebras de
    linha, então cada benefício/banner fica numa linha — snippet limpo).

    - Percentual >= threshold + gatilho de promoção na MESMA linha => candidato.
    - Frases especiais ("pontos em dobro" = 100%) também contam.
    - ignore_plan_discounts=True (página do Clube): descarta o benefício fixo
      de plano "X% OFF na compra de pontos". Campanhas de bônus na assinatura
      continuam passando (usam "bônus"/"em dobro"/"ganhe").

    Retorna lista de dicts: {percent, keyword, snippet}.
    """
    found = []
    seen = set()

    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        norm = _normalize(line)

        # Ruído fixo conhecido.
        if any(ns in norm for ns in NOISE_SNIPPETS):
            continue
        # Benefício de plano do Clube (não é campanha).
        if ignore_plan_discounts and PLAN_DISCOUNT_RE.search(norm):
            continue

        # 1) Percentuais explícitos na linha.
        for m in re.finditer(r"(\d{1,3})\s*%", norm):
            pct = int(m.group(1))
            if pct < threshold or pct > 500:
                continue
            kw = _matched_keyword(norm)
            if not kw:
                continue
            key = (pct, norm[:90])
            if key in seen:
                continue
            seen.add(key)
            found.append({"percent": pct, "keyword": kw, "snippet": line})

        # 2) Frases especiais sem número (em dobro/triplo).
        for phrase, pct in SPECIAL_PHRASES.items():
            if pct < threshold:
                continue
            if _strip_accents(phrase) in norm:
                key = (pct, norm[:90])
                if key in seen:
                    continue
                seen.add(key)
                found.append({"percent": pct, "keyword": phrase, "snippet": line})
                break  # uma frase especial por linha basta

    found.sort(key=lambda x: x["percent"], reverse=True)
    return found


# ----------------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------------
def send_telegram(message: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID não configurados no .env. "
                  "Mensagem NÃO enviada.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(url, data=payload, timeout=30)
        if r.status_code == 200:
            log.info("Mensagem enviada no Telegram com sucesso.")
            return True
        log.error("Falha ao enviar Telegram (%s): %s", r.status_code, r.text[:300])
        return False
    except requests.RequestException as e:
        log.error("Erro de rede ao enviar Telegram: %s", e)
        return False


def send_telegram_photo(image_bytes: bytes, caption: str) -> bool:
    """Envia uma foto (bytes PNG) no Telegram com legenda."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("Telegram não configurado — foto NÃO enviada.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    try:
        r = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"},
            files={"photo": ("banner.png", image_bytes, "image/png")},
            timeout=60,
        )
        if r.status_code == 200:
            log.info("Foto enviada no Telegram com sucesso.")
            return True
        log.error("Falha ao enviar foto (%s): %s", r.status_code, r.text[:300])
        return False
    except requests.RequestException as e:
        log.error("Erro de rede ao enviar foto: %s", e)
        return False


# ----------------------------------------------------------------------------
# Monitor visual de banner (dHash perceptual)
# ----------------------------------------------------------------------------
def _dhash(image, hash_size: int = 8) -> int:
    """
    Difference hash: redimensiona pra (hash_size+1 x hash_size) em cinza e
    compara pixels adjacentes. Retorna inteiro de hash_size*hash_size bits.
    Robusto a pequenas variações de renderização (antialias, compressão).
    """
    from PIL import Image
    img = image.convert("L").resize((hash_size + 1, hash_size), Image.LANCZOS)
    px = img.tobytes()  # 1 byte por pixel (modo "L"); evita getdata() deprecado
    bits = 0
    bit = 0
    for row in range(hash_size):
        for col in range(hash_size):
            left = px[row * (hash_size + 1) + col]
            right = px[row * (hash_size + 1) + col + 1]
            if left > right:
                bits |= (1 << bit)
            bit += 1
    return bits


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def capture_top_banner(driver):
    """Tira print da viewport, recorta a faixa de topo e devolve (PIL.Image, png_bytes)."""
    from io import BytesIO
    from PIL import Image
    # Garante que estamos no topo da página.
    try:
        driver.execute_script("window.scrollTo(0, 0);")
    except Exception:
        pass
    time.sleep(2)
    png = driver.get_screenshot_as_png()
    img = Image.open(BytesIO(png))
    w, h = img.size
    crop = img.crop((0, 0, w, min(BANNER_CROP_HEIGHT, h)))
    out = BytesIO()
    crop.save(out, format="PNG")
    return crop, out.getvalue()


def check_banner(driver, page_name: str, url: str, state: dict) -> bool:
    """
    Captura a faixa de topo, compara o dHash com o último salvo e, se mudou
    além do limite, manda a foto no Telegram. Salva sempre a imagem capturada
    em banner_<pagina>.png pra inspeção. Retorna True se avisou.
    """
    try:
        from PIL import Image  # noqa: F401
    except Exception:
        log.warning("Pillow não instalado — monitor de banner desativado. "
                    "Rode: pip install pillow")
        return False

    try:
        crop, png_bytes = capture_top_banner(driver)
    except Exception as e:
        log.error("Falha ao capturar banner de '%s': %s", page_name, e)
        return False

    # Salva a imagem capturada (debug / calibração da altura de corte).
    safe = re.sub(r"\W+", "_", page_name)
    try:
        (BASE_DIR / f"banner_{safe}.png").write_bytes(png_bytes)
    except Exception:
        pass

    new_hash = _dhash(crop)
    banner_hashes = state.setdefault("banner_hashes", {})
    old_hash = banner_hashes.get(page_name)

    if old_hash is None:
        # Primeira vez: salva a baseline, não avisa (não sabemos se é promo).
        banner_hashes[page_name] = new_hash
        log.info("Banner de '%s': baseline registrada (sem aviso).", page_name)
        return False

    dist = _hamming(int(old_hash), new_hash)
    log.info("Banner de '%s': distância dHash = %d (limite %d).",
             page_name, dist, DHASH_THRESHOLD)

    if dist > DHASH_THRESHOLD:
        banner_hashes[page_name] = new_hash
        caption = (f"🖼️ <b>Banner mudou na Esfera</b> — {page_name}\n"
                   f"Pode ser campanha nova (mudança visual de {dist} pts).\n{url}")
        send_telegram_photo(png_bytes, caption)
        return True

    return False


# ----------------------------------------------------------------------------
# Estado (evita avisos repetidos)
# ----------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except Exception as e:
        log.error("Não consegui salvar estado: %s", e)


def promo_signature(page_name: str, promo: dict) -> str:
    base = f"{page_name}|{promo['percent']}|{_normalize(promo['snippet'])[:80]}"
    return hashlib.md5(base.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------
# Selenium
# ----------------------------------------------------------------------------
def build_driver():
    """Cria o webdriver. Tenta undetected-chromedriver; cai pro Selenium padrão."""
    chrome_args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--window-size=1366,900",
        "--lang=pt-BR",
    ]
    if HEADLESS:
        chrome_args.append("--headless=new")

    if USE_UNDETECTED:
        try:
            import undetected_chromedriver as uc
            options = uc.ChromeOptions()
            for a in chrome_args:
                options.add_argument(a)
            if CHROME_PROFILE_DIR:
                options.add_argument(f"--user-data-dir={CHROME_PROFILE_DIR}")
            driver = uc.Chrome(options=options, headless=HEADLESS)
            log.info("Driver: undetected-chromedriver.")
            return driver
        except Exception as e:
            log.warning("undetected-chromedriver falhou (%s). Usando Selenium padrão.", e)

    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    options = Options()
    for a in chrome_args:
        options.add_argument(a)
    if CHROME_PROFILE_DIR:
        options.add_argument(f"--user-data-dir={CHROME_PROFILE_DIR}")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    driver = webdriver.Chrome(options=options)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"},
    )
    log.info("Driver: Selenium padrão.")
    return driver


def try_login(driver) -> bool:
    """
    Tenta logar com CPF/senha. Best-effort: o fluxo da Esfera pode mudar e/ou
    pedir 2FA/captcha. Se HEADLESS=False, dá tempo pra você resolver 2FA na mão.
    Retorna True se aparentemente logou.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    if not ESFERA_CPF or not ESFERA_SENHA:
        log.info("Sem CPF/senha no .env — seguindo sem login (página pública).")
        return False

    log.info("Tentando login na Esfera...")
    driver.get(LOGIN_URL)
    wait = WebDriverWait(driver, 25)

    # Campo de CPF/usuário — tentamos vários seletores comuns.
    cpf_selectors = [
        (By.CSS_SELECTOR, "input[name='cpf']"),
        (By.CSS_SELECTOR, "input[name='username']"),
        (By.CSS_SELECTOR, "input[type='tel']"),
        (By.CSS_SELECTOR, "input[autocomplete='username']"),
        (By.XPATH, "//input[contains(@placeholder,'CPF') or contains(@placeholder,'cpf')]"),
    ]
    cpf_field = None
    for by, sel in cpf_selectors:
        try:
            cpf_field = wait.until(EC.presence_of_element_located((by, sel)))
            if cpf_field:
                break
        except Exception:
            continue
    if not cpf_field:
        log.warning("Não achei o campo de CPF. Layout de login pode ter mudado. "
                    "Tente HEADLESS=false e login manual com CHROME_PROFILE_DIR.")
        return False

    cpf_field.clear()
    cpf_field.send_keys(ESFERA_CPF)

    # Senha
    pwd_selectors = [
        (By.CSS_SELECTOR, "input[type='password']"),
        (By.CSS_SELECTOR, "input[name='password']"),
        (By.CSS_SELECTOR, "input[autocomplete='current-password']"),
    ]
    pwd_field = None
    for by, sel in pwd_selectors:
        try:
            pwd_field = driver.find_element(by, sel)
            if pwd_field:
                break
        except Exception:
            continue
    if pwd_field is None:
        # Alguns fluxos pedem senha em etapa seguinte; tenta avançar com Enter.
        from selenium.webdriver.common.keys import Keys
        cpf_field.send_keys(Keys.ENTER)
        time.sleep(3)
        for by, sel in pwd_selectors:
            try:
                pwd_field = wait.until(EC.presence_of_element_located((by, sel)))
                if pwd_field:
                    break
            except Exception:
                continue
    if pwd_field is None:
        log.warning("Não achei o campo de senha. Veja nota sobre login manual.")
        return False

    pwd_field.clear()
    pwd_field.send_keys(ESFERA_SENHA)

    # Botão de entrar
    btn_selectors = [
        (By.XPATH, "//button[contains(translate(., 'ENTRAR', 'entrar'),'entrar')]"),
        (By.CSS_SELECTOR, "button[type='submit']"),
    ]
    clicked = False
    for by, sel in btn_selectors:
        try:
            btn = driver.find_element(by, sel)
            btn.click()
            clicked = True
            break
        except Exception:
            continue
    if not clicked:
        from selenium.webdriver.common.keys import Keys
        pwd_field.send_keys(Keys.ENTER)

    # Espaço para 2FA manual quando não-headless.
    if not HEADLESS:
        log.info("Se aparecer 2FA/captcha, resolva na janela agora. Aguardando 40s...")
        time.sleep(40)
    else:
        time.sleep(8)

    # Heurística simples de sucesso: sair da URL de login.
    ok = "login" not in driver.current_url.lower()
    log.info("Login %s (url atual: %s)", "OK" if ok else "incerto", driver.current_url)
    return ok


def get_page_text(driver, url: str, wait_seconds: int = 12) -> str:
    """Abre a URL, espera o JS renderar e devolve o texto visível."""
    from selenium.webdriver.common.by import By
    log.info("Abrindo: %s", url)
    try:
        driver.set_page_load_timeout(45)
        driver.get(url)
    except Exception as e:
        # Sem isso, driver.get() podia travar pra SEMPRE num Next.js que nunca
        # dispara o evento 'load' (foi o que aconteceu na rodada das 9h de 17/06).
        log.warning("Timeout/erro carregando %s (%s) — sigo com o que renderizou.", url, e)
        try:
            driver.execute_script("window.stop();")
        except Exception:
            pass
    # Espera ativa: rola a página pra disparar lazy-load dos banners.
    end = time.time() + wait_seconds
    last_len = 0
    while time.time() < end:
        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        except Exception:
            pass
        time.sleep(1.5)
        try:
            body = driver.find_element(By.TAG_NAME, "body").text
        except Exception:
            body = ""
        if len(body) == last_len and len(body) > 500:
            break
        last_len = len(body)
    try:
        return driver.find_element(By.TAG_NAME, "body").text
    except Exception:
        return ""


# ----------------------------------------------------------------------------
# Fluxo principal
# ----------------------------------------------------------------------------
def format_alert(page_name: str, url: str, promos: list) -> str:
    lines = [f"🎯 <b>Promoção na Esfera!</b> — {html.escape(page_name)}", ""]
    for p in promos:
        snippet = html.escape(p["snippet"][:200])
        lines.append(f"• <b>{p['percent']}%</b> ({html.escape(p['keyword'])}): {snippet}")
    lines.append("")
    lines.append(f'<a href="{html.escape(url)}">Abrir página</a>')
    lines.append(f"<i>{datetime.now().strftime('%d/%m/%Y %H:%M')}</i>")
    return "\n".join(lines)


def main():
    log.info("=== Esfera Monitor iniciado (limite: %s%%) ===", THRESHOLD)

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram não configurado — vou detectar mas não consigo te avisar. "
                    "Preencha TELEGRAM_TOKEN e TELEGRAM_CHAT_ID no .env.")

    state = load_state()
    notified = set(state.get("notified", []))
    any_new = False

    driver = None
    try:
        driver = build_driver()
        if ESFERA_CPF and ESFERA_SENHA:
            try_login(driver)

        for page_name, url in URLS.items():
            try:
                text = get_page_text(driver, url)
            except Exception as e:
                log.error("Erro ao abrir %s: %s", page_name, e)
                continue

            # Debug: salva o texto cru da página pra inspeção.
            try:
                dump_name = "debug_" + re.sub(r"\W+", "_", page_name) + ".txt"
                (BASE_DIR / dump_name).write_text(text, encoding="utf-8")
            except Exception:
                pass

            if len(text) < 200:
                log.warning("Pouco texto em '%s' (%s chars). Pode ser bloqueio/anti-bot "
                            "ou exigir login.", page_name, len(text))

            # Monitor visual do banner (pega campanha que vem só como imagem).
            if MONITOR_BANNER and page_name in BANNER_PAGES:
                if check_banner(driver, page_name, url, state):
                    any_new = True

            # Na página do Clube, ignoramos os descontos fixos de plano
            # (20/40/50% "na compra de pontos") — são benefício, não campanha.
            ignore_plan = page_name == "Clube Esfera"
            promos = find_promotions(text, THRESHOLD, ignore_plan_discounts=ignore_plan)
            log.info("'%s': %d promoção(ões) >= %s%% encontradas.",
                     page_name, len(promos), THRESHOLD)

            if not promos:
                continue

            new_promos = []
            for p in promos:
                sig = promo_signature(page_name, p)
                if ALWAYS_NOTIFY or sig not in notified:
                    new_promos.append(p)
                    notified.add(sig)

            if new_promos:
                any_new = True
                msg = format_alert(page_name, url, new_promos)
                send_telegram(msg)
            else:
                log.info("Promoções de '%s' já avisadas antes — sem repetir.", page_name)

    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    state["notified"] = list(notified)[-200:]  # mantém histórico enxuto
    state["last_run"] = datetime.now().isoformat()
    save_state(state)

    if not any_new:
        log.info("Nenhuma promoção nova desta vez.")
    log.info("=== Fim ===")


if __name__ == "__main__":
    main()
