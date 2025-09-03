import asyncio
import os
import json
import time
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
        self.heikin_ashi_candles = []
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

    def calculate_heikin_ashi(self, open_price, high, low, close_price):
        if not self.heikin_ashi_candles:
            ha_open = (open_price + close_price) / 2
        else:
            prev_ha = self.heikin_ashi_candles[-1]
            ha_open = (prev_ha['open'] + prev_ha['close']) / 2
        ha_close = (open_price + high + low + close_price) / 4
        ha_high = max(high, ha_open, ha_close)
        ha_low = min(low, ha_open, ha_close)
        return {'open': ha_open, 'high': ha_high, 'low': ha_low, 'close': ha_close}

    def process_candle(self, timestamp, open_price, high, low, close_price):
        self.klines.append({'timestamp': timestamp, 'open': open_price, 'high': high, 'low': low, 'close': close_price})
        if len(self.klines) > 500:
            self.klines.pop(0)
        prev_close = self.klines[-2]['close'] if len(self.klines) > 1 else close_price
        src_candle = self.calculate_heikin_ashi(open_price, high, low, close_price) if self.h else {'close': close_price, 'high': high, 'low': low}
        if self.h:
            self.heikin_ashi_candles.append(src_candle)
        src = src_candle['close']
        src_high = src_candle['high']
        src_low = src_candle['low']
        true_range = max(src_high - src_low, abs(src_high - prev_close), abs(src_low - prev_close))
        self.true_ranges.append(true_range)
        xATR = self.calculate_atr(self.c)
        if xATR is None:
            return {'signal': None}
        nLoss = self.a * xATR
        prev_xATRTrailingStop = self.xATRTrailingStop if self.xATRTrailingStop is not None else src - nLoss
        prev_pos = self.pos
        if src > prev_xATRTrailingStop and prev_close > prev_xATRTrailingStop:
            self.xATRTrailingStop = max(prev_xATRTrailingStop, src - nLoss)
        elif src < prev_xATRTrailingStop and prev_close < prev_xATRTrailingStop:
            self.xATRTrailingStop = min(prev_xATRTrailingStop, src + nLoss)
        elif src > prev_xATRTrailingStop:
            self.xATRTrailingStop = src - nLoss
        else:
            self.xATRTrailingStop = src + nLoss
        if prev_close < prev_xATRTrailingStop and src > prev_xATRTrailingStop:
            self.pos = 1
        elif prev_close > prev_xATRTrailingStop and src < prev_xATRTrailingStop:
            self.pos = -1
        else:
            self.pos = prev_pos
        current_atr = self.calculate_atr(self.c)
        if current_atr is not None:
            self.atr_values.append(current_atr)
        long_term_atr_ma = self.calculate_sma(self.atr_values, self.atr_ma_period)
        is_sideways = self.use_filter and long_term_atr_ma is not None and (current_atr < long_term_atr_ma * self.atr_threshold)
        signal = None
        if self.pos != prev_pos:
            if self.pos == 1 and not is_sideways:
                signal = {'type': 'BUY', 'message': 'UT Bot: AL sinyali'}
            elif self.pos == -1 and not is_sideways:
                signal = {'type': 'SELL', 'message': 'UT Bot: SAT sinyali'}
        return {'signal': signal}

    def close_position(self, price):
        if self.position_size == 0:
            return
        pnl = self.position_size * (price - self.get_avg_entry_price())
        self.capital += pnl
        self.trades.append({
            'type': 'SELL' if self.position_size > 0 else 'BUY',
            'price': price,
            'quantity': abs(self.position_size),
            'action': 'exit',
            'pnl': pnl
        })
        self.position_size = 0

    def open_position(self, side, price):
        qty = (self.capital * (self.qty_percent / 100)) / price
        self.position_size = qty if side == 'BUY' else -qty
        self.trades.append({
            'type': side,
            'price': price,
            'quantity': qty,
            'action': 'entry'
        })
    
    def get_avg_entry_price(self):
        entry_trades = [t for t in self.trades if t['action'] == 'entry']
        if not entry_trades:
            return 0
        return entry_trades[-1]['price']

# =========================================================================================
# BOT AYARLARI
# =========================================================================================
CFG = {
    'a': 7, 'c': 17, 'h': False, 'use_filter': True,
    'atr_ma_period': 123, 'atr_threshold': 0.7,
    'TRADE_SIZE_PERCENT': 100,
    'SYMBOL': os.getenv('SYMBOL', 'ETHUSDT'),
    'INTERVAL': os.getenv('INTERVAL', '1m'),
    'IS_TESTNET': os.getenv('IS_TESTNET', 'False').lower() == 'true',
    'INITIAL_CAPITAL': 100,
    'COOLDOWN_SECONDS': 60*60
}

bot_current_position = 'none'
total_net_profit = 0
is_bot_initialized = False
last_signal_time = 0
is_simulation_mode = not os.getenv('BINANCE_API_KEY') or not os.getenv('BINANCE_SECRET_KEY')

telegram_bot = None
if os.getenv('TG_TOKEN') and os.getenv('TG_CHAT_ID'):
    telegram_bot = telegram.Bot(token=os.getenv('TG_TOKEN'))

ut_bot_strategy = UTBotStrategy(options=CFG)

async def send_telegram_message(text):
    if not telegram_bot or not os.getenv('TG_CHAT_ID'):
        print("Telegram API token veya chat ID ayarlanmadı. Mesaj atlanıyor.")
        return
    try:
        await telegram_bot.send_message(chat_id=os.getenv('TG_CHAT_ID'), text=text, parse_mode=constants.ParseMode.MARKDOWN)
    except Exception as e:
        print(f"Telegram mesajı gönderilirken hata oluştu: {e}")

# =========================================================================================
# BOT ANA DÖNGÜSÜ (WebSocket ile Binance'ten veri)
# =========================================================================================
async def run_bot():
    print("🤖 Bot başlatılıyor...")
    await send_telegram_message("🤖 Bot Render üzerinde başlatıldı!")

    client = await AsyncClient.create()
    bm = BinanceSocketManager(client)

    # Kline stream (mum verisi)
    ts = bm.kline_socket(symbol=CFG['SYMBOL'], interval=CFG['INTERVAL'])

    async with ts as tscm:
        async for msg in tscm:
            if msg['e'] != 'kline':
                continue

            k = msg['k']
            is_candle_closed = k['x']
            if is_candle_closed:
                timestamp = k['t']
                open_price = float(k['o'])
                high = float(k['h'])
                low = float(k['l'])
                close_price = float(k['c'])

                print(f"🕒 Yeni mum kapandı: {CFG['SYMBOL']} {CFG['INTERVAL']} close={close_price}")
                result = ut_bot_strategy.process_candle(timestamp, open_price, high, low, close_price)

                if result['signal']:
                    signal = result['signal']
                    log_msg = f"{signal['message']} | Fiyat: {close_price}"
                    print(f"📢 {log_msg}")
                    await send_telegram_message(log_msg)

    await client.close_connection()

# =========================================================================================
# HTTP SERVER (Render için)
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
    print(f"🌐 HTTP server ayakta: 0.0.0.0:{port}")

# =========================================================================================
# MAIN
# =========================================================================================
async def main():
    await asyncio.gather(
        start_http_server(),
        run_bot()
    )

if __name__ == "__main__":
    asyncio.run(main())
