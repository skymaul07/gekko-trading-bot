# -*- coding: utf-8 -*-
# ============================================================
# GEKKO BOT | Automated BTC trading bot for MetaTrader 5
# PUBLIC SHOWCASE VERSION
#
# This repository shows the bot's architecture: market data, regime
# detection, order-flow (CVD) monitoring, news filter, risk management
# and order execution.
#
# The proprietary entry logic and tuned parameters were REMOVED.
# The strategies below are simple placeholder examples.
#
# Copyright (c) 2026 Manuel Rosales Naveda. All rights reserved.
# ============================================================
import os, sys, time, json, threading, collections, datetime as dt
import requests
import pandas as pd
import MetaTrader5 as mt5

# ------------------------- CONFIG -------------------------
# Credentials come ONLY from environment variables (see .env.example).
TELEGRAM_TOKEN = os.environ.get("GEKKO_TG_TOKEN", "")
CHAT_ID = os.environ.get("GEKKO_TG_CHAT", "")

MODO = "DEMO_VALIDACION"
LOGIN_DEMO = int(os.environ.get("GEKKO_MT5_LOGIN", "0"))  # demo account number

NOMBRE_BOT = "gekko-showcase"
SYMBOL = "BTCUSDTm"
SYMBOL_CVD = "XXBTZUSD"
MAGIC_NUMBER = 100001              # identifies this bot's orders

# VOLUMENES AJUSTADOS PARA $100 DE CAPITAL
VOLUME_BASE = 0.01
VOLUME_TENDENCIA = 0.01
VOLUME_POST_NOTICIA = 0.01

CHECK_INTERVAL_SECONDS = 1
HEARTBEAT_CADA_SEG = 60 * 60    # 1 HORA
SUPERVISOR_CADA_SEG = 6 * 3600
COOLDOWN_SIGNAL_SEG = 60

# STOP LOSS / TAKE PROFIT (example values)
SL_DIST_USD = 200.0
TP_DIST_USD = 300.0

# ANTI-REENTRY & STARTUP
COOLDOWN_POST_CIERRE_SEG = 30 * 60      # 30 minutos después de cerrar
DISTANCIA_MINIMA_ENTRADA_PCT = 0.15     # 0.15% de distancia mínima
VENTANA_HISTORIAL_SEG = 2 * 3600        # Historial de 2 horas
COOLDOWN_ARRANQUE_SEG = 5 * 60          # 5 minutos de calentamiento al iniciar

# DEFENSAS DE CAPITAL (AJUSTADAS PARA $100)
MAX_PNL_DIA_USD = -10.0                 # 10% del capital
PAUSA_AUDITORIA_EQUITY = 85.0           # 15% de drawdown
KILL_SWITCH_EQUITY = 70.0               # 30% de drawdown máximo

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")  # optional: AI regime supervisor
APLICAR_AUTO = True

ESTADO_ARCHIVO = "estado_bot.json"
SOLO_LATERAL = True
REGIMEN_ACTUAL = "LATERAL"

# ------------------------- TELEGRAM -------------------------
def send_telegram(texto):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": f"[{NOMBRE_BOT}] {texto}", "parse_mode": "HTML"}, timeout=15)
    except: pass

def tiempo_servidor():
    tk = mt5.symbol_info_tick(SYMBOL)
    return int(tk.time) if tk and tk.time else int(time.time())

# ------------------------- CVD MONITOR (KRAKEN) -------------------------
class CVDMonitor:
    def __init__(self, symbol=SYMBOL_CVD, poll_seg=10, ventana_min=60):
        self.symbol = symbol
        self.poll_seg = poll_seg
        self.ventana_min = ventana_min
        self.trades = collections.deque()
        self.cursor_ms = 0
        self.ultimo_ok = 0.0
        self._lock = threading.Lock()

    def _fetch(self, desde_ms, hasta_ms):
        url = "https://api.kraken.com/0/public/Trades"
        params = {"pair": self.symbol}
        headers = {"User-Agent": "Mozilla/5.0"}
        try:
            r = requests.get(url, params=params, headers=headers, timeout=10)
            r.raise_for_status()
            data = r.json()
            if "error" in data and data["error"]: return []
            trades = data["result"].get(self.symbol, [])
            out = []
            for t in trades:
                qty = float(t[1])
                ts = int(float(t[2]) * 1000)
                side = t[3]
                if ts < desde_ms: continue
                delta = qty if side == 'b' else -qty
                out.append((ts, delta))
            if out: self.cursor_ms = max(ts for ts, _ in out) + 1
            return out
        except Exception as e:
            print(f"[CVD] Error: {e}")
            return []

    def update(self):
        ahora_ms = int(time.time() * 1000)
        if self.cursor_ms == 0: self.cursor_ms = ahora_ms - self.ventana_min * 60000
        nuevos = self._fetch(self.cursor_ms, ahora_ms)
        with self._lock:
            for ts, d in nuevos:
                self.trades.append((ts, d))
            if nuevos: self.cursor_ms = max(ts for ts, _ in nuevos) + 1
            corte = ahora_ms - self.ventana_min * 60000
            while self.trades and self.trades[0][0] < corte: self.trades.popleft()
        if nuevos: self.ultimo_ok = time.time()

    def start(self):
        def loop():
            while True:
                self.update()
                time.sleep(self.poll_seg)
        threading.Thread(target=loop, daemon=True).start()

    def delta_min(self, n):
        ahora_ms = int(time.time() * 1000)
        desde = ahora_ms - n * 60000
        with self._lock: return sum(d for ts, d in self.trades if desde <= ts < ahora_ms + 60000)

CVM = CVDMonitor()

# ------------------------- ESCUDO NOTICIAS + ACELERADOR -------------------------
class NewsMonitor:
    def __init__(self):
        self.url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        self.modo = "NORMAL"
        self.ultima_noticia_time = 0
        self.ultima_revision = 0
        self.proxima_noticia_info = ""

    def actualizar(self):
        if time.time() - self.ultima_revision < 1800: return
        self.ultima_revision = time.time()
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            r = requests.get(self.url, headers=headers, timeout=10)
            r.raise_for_status()
            data = r.json()
            
            ahora_utc = dt.datetime.now(dt.timezone.utc)
            
            for ev in data:
                if ev.get("country") == "USD" and ev.get("impact") == "High":
                    fecha_str = ev.get("datetime", "")
                    try:
                        fecha_et = dt.datetime.strptime(fecha_str.strip(), "%b %d, %Y, %I:%M%p")
                        fecha_utc = fecha_et + dt.timedelta(hours=5)
                        fecha_utc = fecha_utc.replace(tzinfo=dt.timezone.utc)
                        diff_seg = (fecha_utc - ahora_utc).total_seconds()
                        diff_min = diff_seg / 60
                        
                        if -5 < diff_min < 30:
                            self.modo = "TRINCHERA"
                            self.proxima_noticia_info = f"{ev.get('title', '')} en {int(diff_min)} min"
                            return
                        
                        if -45 < diff_min < -15:
                            cvd_5min = CVM.delta_min(5)
                            if abs(cvd_5min) >= 5.0:
                                self.modo = "ACELERADOR"
                                self.ultima_noticia_time = time.time()
                                self.proxima_noticia_info = f"Post-{ev.get('title', '')} | CVD={cvd_5min:+.1f}"
                                return
                    except: continue
            
            if self.modo != "NORMAL":
                print(f"[NEWS] Volviendo a modo NORMAL")
            self.modo = "NORMAL"
            self.proxima_noticia_info = "Sin noticias High cerca"
            
        except Exception as e:
            print(f"[NEWS] Error: {e}")

    def es_trinchera(self):
        return self.modo == "TRINCHERA"
    
    def es_acelerador(self):
        if self.modo == "ACELERADOR":
            if time.time() - self.ultima_noticia_time > 45 * 60:
                self.modo = "NORMAL"
                return False
            return True
        return False
    
    def descripcion(self):
        if self.modo == "TRINCHERA":
            return f"🛡️ TRINCHERA: {self.proxima_noticia_info}"
        elif self.modo == "ACELERADOR":
            return f"⚡ ACELERADOR: {self.proxima_noticia_info}"
        else:
            return f"✅ NORMAL: {self.proxima_noticia_info}"

NEWS = NewsMonitor()

# ------------------------- INDICADORES -------------------------
def obtener_df(tf, n=200):
    rates = mt5.copy_rates_from_pos(SYMBOL, tf, 0, n)
    if rates is None or len(rates) == 0: return None
    return pd.DataFrame(rates)

def ema(closes, period):
    if len(closes) < period: return closes[-1] if closes else 0
    k = 2.0 / (period + 1)
    out = [closes[0]]
    for c in closes[1:]:
        out.append(c * k + out[-1] * (1 - k))
    return out[-1]

def adx(df, period=14):
    if len(df) < period * 2: return 0
    highs = df["high"].tolist()
    lows = df["low"].tolist()
    closes = df["close"].tolist()
    plus_dm, minus_dm, tr = [], [], []
    for i in range(1, len(closes)):
        up_move = highs[i] - highs[i-1]
        down_move = lows[i-1] - lows[i]
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])))
    if len(tr) < period: return 0
    atr = sum(tr[-period:]) / period
    plus_di = 100 * sum(plus_dm[-period:]) / (atr * period) if atr > 0 else 0
    minus_di = 100 * sum(minus_dm[-period:]) / (atr * period) if atr > 0 else 0
    dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di) if (plus_di + minus_di) > 0 else 0
    return dx

def rsi(closes, period=14):
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0)); losses.append(max(-d, 0.0))
    if len(gains) < period: return 50.0
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    return 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)

# ------------------------- SUPERVISOR IA -------------------------
def estadisticas_recientes():
    fin = dt.datetime.now()
    ini = fin - dt.timedelta(days=7)
    deals = mt5.history_deals_get(ini, fin)
    if not deals: return None
    dir_map = {}
    pnls, pnls_l, pnls_s = [], [], []
    for d in deals:
        if d.magic != MAGIC_NUMBER: continue
        if d.entry == mt5.DEAL_TYPE_BUY: dir_map[d.position_id] = "LONG"
        elif d.entry == mt5.DEAL_TYPE_SELL: dir_map[d.position_id] = "SHORT"
        elif d.entry == mt5.DEAL_ENTRY_OUT:
            pnl = d.profit + d.swap + d.commission
            dd = dir_map.get(d.position_id, "LONG")
            pnls.append(pnl)
            (pnls_l if dd == "LONG" else pnls_s).append(pnl)
    def stats(xs):
        if not xs: return None
        w = [p for p in xs if p > 0]; l = [p for p in xs if p <= 0]
        gw = sum(w); gl = abs(sum(l))
        pf = (gw / gl) if gl > 0 else 99.0
        return {"n": len(xs), "wr": round(100.0 * len(w) / len(xs), 1), "pf": round(pf, 2), "exp": round(sum(xs) / len(xs), 2)}
    if len(pnls) < 3: return None
    out = stats(pnls)
    out["long"] = stats(pnls_l)
    out["short"] = stats(pnls_s)
    return out

def detectar_regimen_ia(df_h1):
    closes = df_h1["close"].tolist()
    ema50 = ema(closes, 50)
    ema200 = ema(closes, 200)
    diff_pct = (ema50 - ema200) / ema200 * 100
    adx_val = adx(df_h1, 14)
    stats = estadisticas_recientes()
    
    if not DEEPSEEK_API_KEY or DEEPSEEK_API_KEY == "sk-tu_clave_aqui" or SOLO_LATERAL:
        if adx_val > 25:
            if diff_pct > 0.3: return "ALCISTA", diff_pct, adx_val
            elif diff_pct < -0.3: return "BAJISTA", diff_pct, adx_val
        return "LATERAL", diff_pct, adx_val
    
    prompt = (f"Analiza BTC/USDT:\n- EMA50 vs EMA200: {diff_pct:.2f}%\n- ADX: {adx_val:.1f}\n- Stats: {stats}\nResponde: ALCISTA, BAJISTA, o LATERAL")
    try:
        r = requests.post("https://api.deepseek.com/chat/completions",
                          headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
                          json={"model": "deepseek-chat", "messages": [{"role": "user", "content": prompt}], "temperature": 0.1}, timeout=30)
        respuesta = r.json()["choices"][0]["message"]["content"].strip().upper()
        if "ALCISTA" in respuesta: return "ALCISTA", diff_pct, adx_val
        elif "BAJISTA" in respuesta: return "BAJISTA", diff_pct, adx_val
        return "LATERAL", diff_pct, adx_val
    except:
        if adx_val > 25:
            if diff_pct > 0.3: return "ALCISTA", diff_pct, adx_val
            elif diff_pct < -0.3: return "BAJISTA", diff_pct, adx_val
        return "LATERAL", diff_pct, adx_val

def revision_supervisor():
    global REGIMEN_ACTUAL
    df_h1 = obtener_df(mt5.TIMEFRAME_H1, 300)
    if df_h1 is None: return
    regimen, diff_pct, adx_val = detectar_regimen_ia(df_h1)
    if regimen != REGIMEN_ACTUAL:
        REGIMEN_ACTUAL = regimen
        send_telegram(f"🔄 <b>RÉGIMEN CAMBIADO A: {regimen}</b>\nEMA: {diff_pct:.2f}% | ADX: {adx_val:.1f}")
        print(f"[SUPERVISOR] Régimen: {regimen} | EMA: {diff_pct:.2f}% | ADX: {adx_val:.1f}")

# ------------------------- ESTRATEGIAS -------------------------
# NOTE: the original entry rules and tuned thresholds are proprietary and are
# not included in this public version. These are simple placeholder examples
# (EMA crossover / RSI) so the rest of the bot can run end to end.
# Each function returns: (direction or None, reason text, cvd_confirms).

def estrategia_alcista(tick, df_m15):
    closes = df_m15["close"].tolist()
    if ema(closes, 9) > ema(closes, 21):
        return "BUY", "Placeholder: EMA9 > EMA21", CVM.delta_min(5) > 0
    return None, "Placeholder: waiting for EMA crossover", False

def estrategia_bajista(tick, df_m15):
    closes = df_m15["close"].tolist()
    if ema(closes, 9) < ema(closes, 21):
        return "SELL", "Placeholder: EMA9 < EMA21", CVM.delta_min(5) < 0
    return None, "Placeholder: waiting for EMA crossover", False

def estrategia_lateral(tick, df_h1, df_m15):
    rsi_val = rsi(df_m15["close"].tolist(), 14)
    if rsi_val < 30:
        return "BUY", f"Placeholder: RSI oversold ({rsi_val:.0f})", CVM.delta_min(5) > 0
    if rsi_val > 70:
        return "SELL", f"Placeholder: RSI overbought ({rsi_val:.0f})", CVM.delta_min(5) < 0
    return None, f"Placeholder: RSI {rsi_val:.0f}, no signal", False

# ------------------------- HELPERS -------------------------
def posicion_abierta_mia():
    pos = mt5.positions_get(symbol=SYMBOL)
    if not pos: return None
    for p in pos:
        if p.magic == MAGIC_NUMBER: return p
    return None

def cierre_real_desde_historial(ticket):
    deals = mt5.history_deals_get(position=ticket)
    px, pnl = None, 0.0
    if deals:
        for d in deals:
            if d.entry == mt5.DEAL_ENTRY_OUT:
                px = d.price; pnl += d.profit + d.swap + d.commission
    return px, pnl

def obtener_filling_mode():
    info = mt5.symbol_info(SYMBOL)
    fm = info.filling_mode if info else 0
    if fm & mt5.ORDER_FILLING_FOK: return mt5.ORDER_FILLING_FOK
    if fm & mt5.ORDER_FILLING_IOC: return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN

# ------------------------- ESTADO PERSISTENTE -------------------------
def cargar_estado():
    if os.path.exists(ESTADO_ARCHIVO):
        try:
            with open(ESTADO_ARCHIVO, "r") as f:
                return json.load(f)
        except:
            pass
    return {"fecha_pnl": "", "pnl_dia": 0.0, "fecha_breaker": "", "cerrojo": False, "historial_entradas": [], "ultimo_cierre_time": 0}

def guardar_estado(estado):
    with open(ESTADO_ARCHIVO, "w") as f:
        json.dump(estado, f, indent=2)

# ------------------------- DEFENSAS DE CAPITAL -------------------------
def revisar_defensas(estado):
    info = mt5.account_info()
    if info is None:
        return "OK"
    
    equity = info.equity
    
    if equity <= KILL_SWITCH_EQUITY:
        send_telegram(f"🚨 <b>KILL SWITCH ACTIVADO</b>\nEquity: ${equity:.2f}\nLímite: ${KILL_SWITCH_EQUITY:.2f}\n\nEl bot se detiene INMEDIATAMENTE.")
        print(f"[KILL SWITCH] Equity: ${equity:.2f}")
        return "KILL"
    
    if equity <= PAUSA_AUDITORIA_EQUITY:
        if not estado.get("cerrojo"):
            estado["cerrojo"] = True
            guardar_estado(estado)
            send_telegram(f"⚠️ <b>PAUSA DE AUDITORÍA</b>\nEquity: ${equity:.2f}\nLímite: ${PAUSA_AUDITORIA_EQUITY:.2f}\n\nEl bot se pausa hasta revisión manual.")
            print(f"[PAUSA AUDITORÍA] Equity: ${equity:.2f}")
    
    if estado.get("cerrojo"):
        return "BLOQUEADO"
    
    return "OK"

# ------------------------- BREAKER DIARIO -------------------------
def breaker_activo_hoy(estado):
    hoy = dt.datetime.now().strftime("%Y-%m-%d")
    
    if estado.get("fecha_pnl") != hoy:
        estado["fecha_pnl"] = hoy
        estado["pnl_dia"] = 0.0
        estado["fecha_breaker"] = ""
        guardar_estado(estado)
    
    return estado.get("fecha_breaker") == hoy

def registrar_pnl_dia(estado, pnl):
    hoy = dt.datetime.now().strftime("%Y-%m-%d")
    
    if estado.get("fecha_pnl") != hoy:
        estado["fecha_pnl"] = hoy
        estado["pnl_dia"] = 0.0
    
    estado["pnl_dia"] = round(estado.get("pnl_dia", 0.0) + pnl, 2)
    guardar_estado(estado)
    
    if estado["pnl_dia"] <= MAX_PNL_DIA_USD and estado.get("fecha_breaker") != hoy:
        estado["fecha_breaker"] = hoy
        guardar_estado(estado)
        send_telegram(f"🛑 <b>BREAKER DIARIO ACTIVADO</b>\nPnL del día: ${estado['pnl_dia']:.2f}\nLímite: ${MAX_PNL_DIA_USD:.2f}\n\nEl bot se detiene hasta mañana.")
        print(f"[BREAKER] Activado | PnL día: ${estado['pnl_dia']:.2f}")

# ------------------------- ANTI-REENTRY CHECK -------------------------
def verificar_anti_reentry(precio_actual, historial_entradas):
    ahora = time.time()
    
    historial_entradas[:] = [(p, t) for p, t in historial_entradas if ahora - t < VENTANA_HISTORIAL_SEG]
    
    for precio_entrada, timestamp in historial_entradas:
        distancia_pct = abs(precio_actual - precio_entrada) / precio_entrada * 100
        if distancia_pct < DISTANCIA_MINIMA_ENTRADA_PCT:
            return False, f"Precio muy cercano a entrada reciente ({distancia_pct:.3f}% < {DISTANCIA_MINIMA_ENTRADA_PCT}%)"
    
    return True, "OK"

# ------------------------- EJECUCION -------------------------
def lanzar_operacion(tick, direccion, regimen, cvd_ok, es_post_noticia=False):
    if es_post_noticia:
        vol_base = VOLUME_POST_NOTICIA
        tag_vol = "POST-NOTICIA"
    elif regimen != "LATERAL" and cvd_ok:
        vol_base = VOLUME_TENDENCIA
        tag_vol = "CVD_OK"
    else:
        vol_base = VOLUME_BASE
        tag_vol = "BASICO"
    
    info = mt5.symbol_info(SYMBOL)
    vol = max(info.volume_min, vol_base)
    vol = round(round(vol / info.volume_step) * info.volume_step, 2)
    digits = info.digits
    
    sl_dist = SL_DIST_USD   # example values, not the tuned ones
    tp_dist = TP_DIST_USD
    
    if direccion == "BUY":
        sl = round(tick.ask - sl_dist, digits)
        tp = round(tick.ask + tp_dist, digits)
        order_type = mt5.ORDER_TYPE_BUY
        price = tick.ask
    else:
        sl = round(tick.bid + sl_dist, digits)
        tp = round(tick.bid - tp_dist, digits)
        order_type = mt5.ORDER_TYPE_SELL
        price = tick.bid
    
    req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": SYMBOL, "volume": vol,
           "type": order_type, "price": price, "sl": sl, "tp": tp,
           "deviation": 20, "magic": MAGIC_NUMBER, "comment": f"Gekko {regimen}",
           "type_filling": obtener_filling_mode(), "type_time": mt5.ORDER_TIME_GTC}
    
    res = mt5.order_send(req)
    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
        msg = (f"🚀 <b>APERTURA {direccion}</b>\n"
               f"Vol: {vol} lotes | Precio: {price:.2f}\n"
               f"SL: {sl:.2f} | TP: {tp:.2f}\n"
               f"Régimen: {regimen} | Modo: {tag_vol}\n"
               f"CVD5: {CVM.delta_min(5):+.1f}")
        send_telegram(msg)
        print(f"[TRADE] {direccion} {vol} @ {price:.2f} | {tag_vol}")
        return res.order, price, vol
    else:
        print(f"[ERROR] retcode={res.retcode if res else 'None'}")
        return None, None, None

# ------------------------- MAIN -------------------------
def main():
    global REGIMEN_ACTUAL
    
    print(f"Bot {NOMBRE_BOT} | Capital $100 | Breaker -$10 | Kill $70")
    if not mt5.initialize(): print("No MT5"); sys.exit(1)
    
    info = mt5.account_info()
    if info.login != LOGIN_DEMO:
        print("Login incorrecto"); sys.exit(1)
    
    mt5.symbol_select(SYMBOL, True)
    
    si = mt5.symbol_info(SYMBOL)
    print(f"[DIAG] contract_size={si.trade_contract_size} | vol_min={si.volume_min} | filling={obtener_filling_mode()}")
    print(f"[DEMO] login={info.login} | balance={info.balance:.2f}")
    
    CVM.update(); CVM.start()
    NEWS.actualizar()
    
    estado = cargar_estado()
    historial_entradas = [(p, t) for p, t in estado.get("historial_entradas", [])]
    ultimo_cierre_time = estado.get("ultimo_cierre_time", 0)
    
    def_res = revisar_defensas(estado)
    if def_res == "KILL":
        mt5.shutdown()
        sys.exit(0)
    
    if breaker_activo_hoy(estado):
        send_telegram(f"⚠️ <b>BREAKER DIARIO ACTIVO</b>\nEl bot está pausado hasta mañana.\nPnL del día: ${estado.get('pnl_dia', 0.0):.2f}")
        print(f"[BREAKER] Activo | PnL día: ${estado.get('pnl_dia', 0.0):.2f}")
    
    send_telegram(f"🦎 <b>BOT GEKKO {NOMBRE_BOT} INICIADO</b>\n"
                  f"Balance: ${info.balance:.2f}\n"
                  f"Régimen inicial: {REGIMEN_ACTUAL}\n"
                  f"News: {NEWS.descripcion()}\n"
                  f"Breaker: ${MAX_PNL_DIA_USD}\n"
                  f"Kill Switch: ${KILL_SWITCH_EQUITY}\n"
                  f"⏳ Cooldown de arranque: 5 minutos")
    print(f"[CVD] OK | [NEWS] {NEWS.descripcion()} | [BREAKER] ${MAX_PNL_DIA_USD} | [KILL] ${KILL_SWITCH_EQUITY}")

    ticket_activo = None
    precio_entrada = None
    volumen_entrada = None
    direccion_entrada = None
    ultimo_hb = 0
    ultima_deteccion = 0
    ultimo_signal_time = 0
    tiempo_arranque = time.time()  # REGISTRO DEL TIEMPO DE ARRANQUE

    revision_supervisor()

    while True:
        now = time.time()
        tick = mt5.symbol_info_tick(SYMBOL)
        if tick is None: time.sleep(CHECK_INTERVAL_SECONDS); continue

        NEWS.actualizar()

        if now - ultima_deteccion >= SUPERVISOR_CADA_SEG:
            revision_supervisor()
            ultima_deteccion = now

        def_res = revisar_defensas(estado)
        if def_res == "KILL":
            mt5.shutdown()
            sys.exit(0)

        if now - ultimo_hb >= HEARTBEAT_CADA_SEG:
            ts = dt.datetime.fromtimestamp(tiempo_servidor()).strftime('%H:%M:%S')
            cvd5 = CVM.delta_min(5)
            pos_txt = "0" if ticket_activo is None else f"1 ({direccion_entrada} {volumen_entrada} lotes)"
            mira = ""
            if REGIMEN_ACTUAL == "LATERAL" and ticket_activo is None:
                try:
                    _h1 = obtener_df(mt5.TIMEFRAME_H1, 300); _m15 = obtener_df(mt5.TIMEFRAME_M15, 100)
                    if _h1 is not None and _m15 is not None:
                        mira = "\n" + estrategia_lateral(tick, _h1, _m15)[1]
                except Exception as e:
                    mira = f"\n(no pude calcular el rango: {e})"
            elif REGIMEN_ACTUAL != "LATERAL":
                mira = "\nEsperando régimen LATERAL (no opera en tendencia)"
            breaker_txt = " 🛑" if breaker_activo_hoy(estado) else ""
            msg = (f"⏰ <b>HEARTBEAT {ts}</b>\n"
                   f"Precio: {tick.bid:.2f}\n"
                   f"Régimen: {REGIMEN_ACTUAL}\n"
                   f"CVD5: {cvd5:+.1f}\n"
                   f"Posición: {pos_txt}{mira}\n"
                   f"PnL Día: ${estado.get('pnl_dia', 0.0):.2f}{breaker_txt}\n"
                   f"News: {NEWS.descripcion()}")
            send_telegram(msg)
            ultimo_hb = now

        pos = posicion_abierta_mia()
        if ticket_activo is not None and pos is None:
            px_cierre, pnl = cierre_real_desde_historial(ticket_activo)
            if px_cierre is None: px_cierre = tick.bid
            
            tag = "✅ GANADORA" if pnl > 0 else "❌ PERDEDORA"
            emoji = "🟢" if pnl > 0 else "🔴"
            
            msg = (f"{emoji} <b>CIERRE {direccion_entrada}</b>\n"
                   f"Vol: {volumen_entrada} lotes\n"
                   f"Entrada: {precio_entrada:.2f}\n"
                   f"Salida: {px_cierre:.2f}\n"
                   f"PnL: {pnl:+.2f} USD {tag}\n"
                   f"Régimen: {REGIMEN_ACTUAL}\n"
                   f"PnL Día: ${estado.get('pnl_dia', 0.0):.2f}")
            send_telegram(msg)
            print(f"[CIERRE] {direccion_entrada} | Entrada: {precio_entrada:.2f} | Salida: {px_cierre:.2f} | PnL: {pnl:+.2f}")
            
            historial_entradas.append((precio_entrada, now))
            registrar_pnl_dia(estado, pnl)
            
            ticket_activo = None
            precio_entrada = None
            volumen_entrada = None
            direccion_entrada = None
            ultimo_cierre_time = now
            
            estado["historial_entradas"] = historial_entradas
            estado["ultimo_cierre_time"] = ultimo_cierre_time
            guardar_estado(estado)

        # COOLDOWN DE ARRANQUE (5 MINUTOS)
        tiempo_transcurrido = now - tiempo_arranque
        if tiempo_transcurrido < COOLDOWN_ARRANQUE_SEG:
            # Imprimir en consola cada minuto durante el calentamiento
            if int(tiempo_transcurrido) % 60 == 0 and int(tiempo_transcurrido) > 0:
                faltan = int((COOLDOWN_ARRANQUE_SEG - tiempo_transcurrido) / 60)
                print(f"[ARRANQUE] Calentando motores... Faltan {faltan} min para evaluar entradas.")
        else:
            # LÓGICA NORMAL DE EVALUACIÓN (SOLO DESPUÉS DE 5 MIN)
            if ticket_activo is None and not NEWS.es_trinchera() and not breaker_activo_hoy(estado) and def_res == "OK" \
                    and (REGIMEN_ACTUAL == "LATERAL" or not SOLO_LATERAL):
                if now - ultimo_cierre_time < COOLDOWN_POST_CIERRE_SEG:
                    pass
                else:
                    permitido, razon = verificar_anti_reentry(tick.bid, historial_entradas)
                    
                    if not permitido:
                        pass
                    else:
                        df_h1 = obtener_df(mt5.TIMEFRAME_H1, 300)
                        df_m15 = obtener_df(mt5.TIMEFRAME_M15, 100)
                        
                        if df_h1 is not None and df_m15 is not None:
                            direccion = None
                            razon = ""
                            cvd_ok = False
                            
                            if REGIMEN_ACTUAL == "ALCISTA":
                                direccion, razon, cvd_ok = estrategia_alcista(tick, df_m15)
                            elif REGIMEN_ACTUAL == "BAJISTA":
                                direccion, razon, cvd_ok = estrategia_bajista(tick, df_m15)
                            else:
                                direccion, razon, cvd_ok = estrategia_lateral(tick, df_h1, df_m15)
                            
                            if direccion:
                                if now - ultimo_signal_time >= COOLDOWN_SIGNAL_SEG:
                                    es_post_noticia = NEWS.es_acelerador()
                                    
                                    print(f"[SIGNAL] {direccion} | {razon} | Régimen: {REGIMEN_ACTUAL} | {'ACELERADOR' if es_post_noticia else 'NORMAL'}")
                                    
                                    ticket_activo, precio_entrada, volumen_entrada = lanzar_operacion(
                                        tick, direccion, REGIMEN_ACTUAL, cvd_ok, es_post_noticia
                                    )
                                    
                                    if ticket_activo:
                                        direccion_entrada = direccion
                                    
                                    ultimo_signal_time = now
        
        if int(now) % 300 == 0:
            estado["historial_entradas"] = historial_entradas
            estado["ultimo_cierre_time"] = ultimo_cierre_time
            guardar_estado(estado)
        
        time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()