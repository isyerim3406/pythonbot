import asyncio
import os
import json
from binance import AsyncClient, BinanceSocketManager
from dotenv import load_dotenv
import telegram
from threading import Thread
import time

# .env dosyasını yükle
load_dotenv()

# =========================================================================================
# UT BOT STRATEGY SINIFI
# =========================================================================================
class UTBotStrategy:
    def __init__(self, options=None):
        options = options or {}
        self.a = options.get('a', 1)  # Key Value
        self.c = options.get('c', 10)  # ATR Period
        self.h = options.get('h', False)  # Heikin Ashi
        self.use_filter = options.get('use_filter', True)
        self.atr_ma_period = options.get('atr_ma_period', 100)
        self.atr_threshold = options.get('atr_threshold', 0.7)
        self.initial_capital = options.get('initial_capital', 100)
        self.qty_percent = options.get('qty_percent', 100)

        # Dahili durumlar
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
    'a': 7, 
    'c': 17,
    'h': False, 
    'use_filter': True,
    'atr_ma_period': 123,
    'atr_threshold': 0.7,
    'TRADE_SIZE_PERCENT': 100,
    'SYMBOL': os.getenv('SYMBOL', 'ETHUSDT'),
    'INTERVAL': os.getenv('INTERVAL', '1m'),
    'IS_TESTNET': os.getenv('IS_TESTNET', 'False').lower() == 'true',
    'INITIAL_CAPITAL': 100,
    'COOLDOWN_SECONDS': 60 * 60 # 1 saat soğuma süresi
}

# =========================================================================================
# GLOBAL DURUMLAR
# =========================================================================================
bot_current_position = 'none'
total_net_profit = 0
is_bot_initialized = False
last_signal_time = 0

# Simülasyon modu kontrolü
is_simulation_mode = not os.getenv('BINANCE_API_KEY') or not os.getenv('BINANCE_SECRET_KEY')

# Telegram Bot
telegram_bot = None
if os.getenv('TG_TOKEN') and os.getenv('TG_CHAT_ID'):
    telegram_bot = telegram.Bot(token=os.getenv('TG_TOKEN'))

# Strateji nesnesi
ut_bot_strategy = UTBotStrategy(options=CFG)

# =========================================================================================
# YARDIMCI FONKSİYONLAR
# =========================================================================================
async def send_telegram_message(text):
    if not telegram_bot or not os.getenv('TG_CHAT_ID'):
        print("Telegram API token veya chat ID ayarlanmadı. Mesaj atlanıyor.")
        return
    try:
        await telegram_bot.send_message(chat_id=os.getenv('TG_CHAT_ID'), text=text, parse_mode=telegram.ParseMode.MARKDOWN)
    except Exception as e:
        print(f"Telegram mesajı gönderilirken hata oluştu: {e}")

async def fetch_initial_data(client, symbol, interval):
    try:
        klines = await client.futures_klines(symbol=symbol, interval=interval, limit=500)
        for k in klines:
            ut_bot_strategy.process_candle(k[0], float(k[1]), float(k[2]), float(k[3]), float(k[4]))
        print(f"✅ İlk {len(ut_bot_strategy.klines)} mum verisi yüklendi.")
        global is_bot_initialized
        if not is_bot_initialized:
            await send_telegram_message(f"✅ Bot başlatıldı!\n\n**Mod:** {'Simülasyon' if is_simulation_mode else 'Canlı İşlem'}\n**Sembol:** {CFG['SYMBOL']}\n**Zaman Aralığı:** {CFG['INTERVAL']}\n**Başlangıç Sermayesi:** {CFG['INITIAL_CAPITAL']} USDT")
            is_bot_initialized = True
    except Exception as e:
        print(f"İlk verileri çekerken hata: {e}")

async def place_order(client, side, signal_message):
    global bot_current_position, total_net_profit, last_signal_time
    now = time.time()
    if last_signal_time != 0 and (now - last_signal_time) < CFG['COOLDOWN_SECONDS']:
        print("⏱️ Cooldown süresi dolmadan yeni sinyal geldi. İşlem atlandı.")
        return

    last_close_price = ut_bot_strategy.klines[-1]['close'] if ut_bot_strategy.klines else 0
    
    # Mevcut pozisyonu kapatma
    if bot_current_position != 'none':
        try:
            ut_bot_strategy.close_position(last_close_price)
            total_net_profit = sum(t['pnl'] for t in ut_bot_strategy.trades if t['action'] == 'exit')
            
            if not is_simulation_mode:
                # Gerçek hesapta pozisyon kapatma
                account_info = await client.futures_account_balance()
                position_info = await client.futures_position_information()
                symbol_pos = next((p for p in position_info if p['symbol'] == CFG['SYMBOL']), None)
                if symbol_pos and float(symbol_pos['positionAmt']) != 0:
                    quantity = abs(float(symbol_pos['positionAmt']))
                    closing_side = 'SELL' if float(symbol_pos['positionAmt']) > 0 else 'BUY'
                    await client.futures_create_order(symbol=CFG['SYMBOL'], side=closing_side, type='MARKET', quantity=quantity)
                    print(f"✅ Gerçek pozisyon ({bot_current_position}) kapatıldı.")

            profit = ut_bot_strategy.trades[-1]['pnl']
            profit_message = f"+{profit:.2f} USDT" if profit >= 0 else f"{profit:.2f} USDT"
            position_close_message = f"📉 Pozisyon kapatıldı! {bot_current_position.upper()}\n\nSon Kapanış Fiyatı: {last_close_price}\nBu İşlemden Kâr/Zarar: {profit_message}\n**Toplam Net Kâr: {total_net_profit:.2f} USDT**"
            await send_telegram_message(position_close_message)
            bot_current_position = 'none'
        except Exception as e:
            print(f"Mevcut pozisyonu kapatırken hata oluştu: {e}")
            return
    
    # Yeni pozisyon açma
    if bot_current_position == 'none':
        try:
            current_price = last_close_price
            
            if not is_simulation_mode:
                account_info = await client.futures_account_balance()
                usdt_balance = float(next(a['availableBalance'] for a in account_info if a['asset'] == 'USDT'))
                quantity = (usdt_balance * (CFG['TRADE_SIZE_PERCENT'] / 100)) / current_price
                await client.futures_create_order(symbol=CFG['SYMBOL'], side=side, type='MARKET', quantity=round(quantity, 4))
                print(f"🟢 {side} emri başarıyla verildi. Fiyat: {current_price}")
            
            ut_bot_strategy.open_position(side, current_price)
            bot_current_position = side.lower()
            last_signal_time = now

            await send_telegram_message(f"🚀 **{side} Emri Gerçekleşti!**\n\n**Sinyal:** {signal_message}\n**Fiyat:** {current_price}\n**Toplam Net Kâr: {total_net_profit:.2f} USDT**")
        except Exception as e:
            print(f"Emir verirken hata oluştu: {e}")

async def process_message(msg):
    global bot_current_position
    
    if msg['e'] == 'kline' and msg['k']['x']:
        kline = msg['k']
        new_bar = {
            'open': float(kline['o']),
            'high': float(kline['h']),
            'low': float(kline['l']),
            'close': float(kline['c']),
            'timestamp': kline['T']
        }
        
        result = ut_bot_strategy.process_candle(new_bar['timestamp'], new_bar['open'], new_bar['high'], new_bar['low'], new_bar['close'])
        signal = result['signal']
        
        print(f"Yeni mum verisi geldi. Fiyat: {new_bar['close']}. Sinyal: {signal['type'] if signal else 'none'}.")

        if signal:
            client = await AsyncClient.create(os.getenv('BINANCE_API_KEY'), os.getenv('BINANCE_SECRET_KEY'), testnet=CFG['IS_TESTNET'])
            if signal['type'] == 'BUY' and bot_current_position != 'long':
                await place_order(client, 'BUY', signal['message'])
            elif signal['type'] == 'SELL' and bot_current_position != 'short':
                await place_order(client, 'SELL', signal['message'])

async def main():
    client = await AsyncClient.create(os.getenv('BINANCE_API_KEY'), os.getenv('BINANCE_SECRET_KEY'), testnet=CFG['IS_TESTNET'])
    await fetch_initial_data(client, CFG['SYMBOL'], CFG['INTERVAL'])
    
    bm = BinanceSocketManager(client)
    ts = bm.futures_kline_socket(symbol=CFG['SYMBOL'], interval=CFG['INTERVAL'])

    async with ts as kline_socket:
        while True:
            res = await kline_socket.recv()
            if isinstance(res, dict):
                await process_message(res)

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
