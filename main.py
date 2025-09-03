import asyncio
import os
import time
from datetime import datetime
from binance import AsyncClient, BinanceSocketManager
from dotenv import load_dotenv
import telegram
from telegram import constants
from aiohttp import web

# .env dosyasını yükle
load_dotenv()

# =========================================================================================
# UT BOT STRATEGY SINIFI
# =========================================================================================
class UTBotStrategy:
    def __init__(self, options=None):
        options = options or {}
        self.a = options.get('a', 1)
        self.c = options.get('c', 10)
        self.h = options.get('h', False)
        self.use_filter = options.get('use_filter', True)
        self.atr_ma_period = options.get('atr_ma_period', 100)
        self.atr_threshold = options.get('atr_threshold', 0.7)
        self.initial_capital = options.get('initial_capital', 100)
        self.qty_percent = options.get('qty_percent', 100)
        self.klines = []
        self.true_ranges = []
        self.atr_values = []
        self.xATRTrailingStop = None
        self.pos = 0
        self.capital = self.initial_capital
        self.trades = []
        self.position_size = 0

    def calculate_atr(self, period):
        if len(self.true_ranges) < period:
            return None
        return sum(self.true_ranges[-period:]) / period

    def calculate_sma(self, values, period):
        if len(values) < period:
            return None
        return sum(values[-period:]) / period

    def process_candle(self, timestamp, open_price, high, low, close_price):
        self.klines.append({'timestamp': timestamp, 'close': close_price})
        if len(self.klines) > 500:
            self.klines.pop(0)
        prev_close = self.klines[-2]['close'] if len(self.klines) > 1 else close_price
        true_range = max(high - low, abs(high - prev_close), abs(low - prev_close))
        self.true_ranges.append(true_range)
        xATR = self.calculate_atr(self.c)
        if xATR is None:
            return {'signal': None}
        nLoss = self.a * xATR
        prev_xATRTrailingStop = self.xATRTrailingStop if self.xATRTrailingStop else close_price - nLoss
        prev_pos = self.pos
        if close_price > prev_xATRTrailingStop and prev_close > prev_xATRTrailingStop:
            self.xATRTrailingStop = max(prev_xATRTrailingStop, close_price - nLoss)
        elif close_price < prev_xATRTrailingStop and prev_close < prev_xATRTrailingStop:
            self.xATRTrailingStop = min(prev_xATRTrailingStop, close_price + nLoss)
        elif close_price > prev_xATRTrailingStop:
            self.xATRTrailingStop = close_price - nLoss
        else:
            self.xATRTrailingStop = close_price + nLoss
        if prev_close < prev_xATRTrailingStop and close_price > prev_xATRTrailingStop:
            self.pos = 1
        elif prev_close > prev_xATRTrailingStop and close_price < prev_xATRTrailingStop:
            self.pos = -1
        else:
            self.pos = prev_pos
        current_atr = self.calculate_atr(self.c)
        if current_atr:
            self.atr_values.append(current_atr)
        long_term_atr_ma = self.calculate_sma(self.atr_values, self.atr_ma_period)
        is_sideways = self.use_filter and long_term_atr_ma and (current_atr < long_term_atr_ma * self.atr_threshold)
        signal = None
        if self.pos != prev_pos:
            if self.pos == 1 and not is_sideways:
                signal = {'type': 'BUY', 'message': 'AL Sinyali'}
            elif self.pos == -1 and not is_sideways:
                signal = {'type': 'SELL', 'message': 'SAT Sinyali'}
        return {'signal': signal}

    def close_position(self, price):
        if self.position_size == 0:
            return 0
        pnl = self.position_size * (price - self.get_avg_entry_price())
        self.capital += pnl
        self.trades.append({'action': 'exit', 'pnl': pnl, 'price': price})
        self.position_size = 0
        return pnl

    def open_position(self, side, price):
        qty = (self.capital * (self.qty_percent / 100)) / price
        self.position_size = qty if side == 'BUY' else -qty
        self.trades.append({'action': 'entry', 'price': price, 'type': side, 'quantity': qty})
    
    def get_avg_entry_price(self):
        entries = [t for t in self.trades if t['action'] == 'entry']
        return entries[-1]['price'] if entries else 0

# =========================================================================================
# BOT AYARLARI
# =========================================================================================
CFG = {
    'a': 7, 'c': 17, 'h': False, 'use_filter': True,
    'atr_ma_period': 123, 'atr_threshold': 0.7,
    'TRADE_SIZE_PERCENT': 100,
    'SYMBOL': os.getenv('SYMBOL', 'ETHUSDT'),
    'INTERVAL': os.getenv('INTERVAL', '1h'),
    'INITIAL_CAPITAL': 100,
    'COOLDOWN_SECONDS': 60*60,
    'BOT_NAME': "UTBOTS Python",
    'MODE': "Simülasyon"
}

total_net_profit = 0
last_signal_time = 0

telegram_bot = None
if os.getenv('TG_TOKEN') and os.getenv('TG_CHAT_ID'):
    telegram_bot = telegram.Bot(token=os.getenv('TG_TOKEN'))

ut_bot_strategy = UTBotStrategy(options=CFG)

async def send_telegram_message(text):
    if not telegram_bot or not os.getenv('TG_CHAT_ID'):
        print("Telegram ayarlı değil.")
        return
    await telegram_bot.send_message(chat_id=os.getenv('TG_CHAT_ID'), text=text, parse_mode=constants.ParseMode.MARKDOWN)

# =========================================================================================
# BOT ANA DÖNGÜSÜ
# =========================================================================================
async def run_bot():
    global total_net_profit, last_signal_time
    print("🤖 Bot başlatılıyor...")

    client = await AsyncClient.create()
    bm = BinanceSocketManager(client)

    # Başlangıçta geçmiş 500 mum
    candles = await client.get_klines(symbol=CFG['SYMBOL'], interval=CFG['INTERVAL'], limit=500)
    last_signal = None
    for c in candles:
        ts, o, h, l, cl = c[0], float(c[1]), float(c[2]), float(c[3]), float(c[4])
        result = ut_bot_strategy.process_candle(ts, o, h, l, cl)
        if result['signal']:
            last_signal = result['signal']

    if last_signal:
        msg = (
            f"Bot Başlatıldı!\n"
            f"Mod:{CFG['MODE']}\n"
            f"Sembol: {CFG['SYMBOL']}\n"
            f"Zaman Aralığı: {CFG['INTERVAL']}\n"
            f"Son Oluşan Sinyal: {last_signal['message']}"
        )
        await send_telegram_message(msg)

    ts = bm.kline_socket(symbol=CFG['SYMBOL'], interval=CFG['INTERVAL'])
    async with ts as stream:
        while True:
            kmsg = await stream.recv()
            if kmsg.get('e') != 'kline':
                continue
            k = kmsg['k']
            if k['x']:
                ts = k['t']
                close_price = float(k['c'])
                result = ut_bot_strategy.process_candle(ts, float(k['o']), float(k['h']), float(k['l']), close_price)
                if result['signal']:
                    now = time.time()
                    if last_signal_time and (now - last_signal_time) < CFG['COOLDOWN_SECONDS']:
                        continue
                    side = result['signal']['type']
                    pnl = ut_bot_strategy.close_position(close_price)
                    total_net_profit += pnl
                    ut_bot_strategy.open_position(side, close_price)
                    last_signal_time = now
                    ts_str = datetime.utcfromtimestamp(ts/1000).strftime("%d.%m.%Y - %H:%M")
                    percent_pnl = (pnl / CFG['INITIAL_CAPITAL'])*100 if CFG['INITIAL_CAPITAL'] else 0
                    total_percent = (total_net_profit / CFG['INITIAL_CAPITAL'])*100
                    msg = (
                        f"{side} Emri Gerçekleşti!\n\n"
                        f"Bot Adı: {CFG['BOT_NAME']}\n"
                        f"Sembol: {CFG['SYMBOL']}\n"
                        f"Zaman Aralığı: {CFG['INTERVAL']}\n"
                        f"Sinyal:{result['signal']['message']}\n"
                        f"Fiyat:{close_price}\n"
                        f"Zaman : {ts_str}\n"
                        f"Bu İşlemden Kar/Zarar : % {percent_pnl:.2f} ({pnl:.2f} USDT)\n"
                        f"Toplam Net Kar/Zarar : % {total_percent:.2f} ({total_net_profit:.2f} USDT)"
                    )
                    await send_telegram_message(msg)

    await client.close_connection()

# =========================================================================================
# HTTP SERVER
# =========================================================================================
async def start_http_server():
    async def handle_root(request):
        return web.Response(text="Bot çalışıyor 🚀")
    async def handle_health(request):
        return web.Response(text="ok")
    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_get("/healthz", handle_health)
    port = int(os.environ.get("PORT", 8000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

# =========================================================================================
# MAIN
# =========================================================================================
async def main():
    await asyncio.gather(start_http_server(), run_bot())

if __name__ == "__main__":
    asyncio.run(main())
