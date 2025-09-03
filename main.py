import asyncio
import os
import json
from binance import AsyncClient, BinanceSocketManager
from dotenv import load_dotenv
import telegram
from telegram import constants
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

is_simulation_mode = not os.getenv('BINANCE_API_KEY') or not os.getenv('BINANCE_SECRET_KEY')

# =========================================================================================
# SİMÜLASYON İÇİN SANAL BİNANCE İstemcisi
# =========================================================================================
class MockAsyncClient:
    async def futures_klines(self, **kwargs):
        # Statik veri döndürür
        try:
            with open('mock_klines.json', 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            # Varsayılan mock data
            return [
                [1678886400000, "42000", "42500", "41500", "42300"],
                [1678886460000, "42300", "42800", "42200", "42700"],
                [1678886520000, "42700", "42900", "42600", "42850"]
            ]

    async def futures_account_balance(self, **kwargs):
        return [{'asset': 'USDT', 'availableBalance': '1000'}]
    
    async def futures_position_information(self, **kwargs):
        return [{'symbol': CFG['SYMBOL'], 'positionAmt': '0'}]

    async def futures_create_order(self, **kwargs):
        print(f"✅ SİMÜLASYON: {kwargs['side']} emri verildi. Miktar: {kwargs['quantity']}")
        return {'status': 'FILLED'}

class MockBinanceSocketManager:
    def __init__(self, client):
        self.client = client
        self.mock_data = []
        try:
            with open('mock_kline_stream.json', 'r') as f:
                self.mock_data = json.load(f)
        except FileNotFoundError:
            # Varsayılan mock stream data
            self.mock_data = [
                {"e":"kline","k":{"t":1678886580000,"T":1678886639999,"s":"ETHUSDT","i":"1m","f":100,"L":200,"o":"42850","c":"43000","h":"43050","l":"42800","v":"1000","n":100,"x":True,"q":"100000","V":"500","Q":"50000","B":"0"}},
                {"e":"kline","k":{"t":1678886640000,"T":1678886699999,"s":"ETHUSDT","i":"1m","f":201,"L":301,"o":"43000","c":"42950","h":"43020","l":"42900","v":"1200","n":150,"x":True,"q":"120000","V":"600","Q":"60000","B":"0"}}
            ]
        self.current_index = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    def kline_socket(self, symbol, interval):
        print(f"SİMÜLASYON: {symbol.lower()}@kline_{interval} için WebSocket bağlantısı kuruluyor.")
        return self

    async def recv(self):
        if self.current_index >= len(self.mock_data):
            self.current_index = 0  # Döngüsel olarak veri sağlar
        data = self.mock_data[self.current_index]
        self.current_index += 1
        await asyncio.sleep(1)  # 1 saniye bekle
        return data

# =========================================================================================
# TELEGRAM BOT VE DİĞER YARDIMCI FONKSİYONLAR
# =========================================================================================
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
        # Hatanın tekrar yükseltilmesini sağlar
        raise e

async def place_order(client, side, signal_message):
    global bot_current_position, total_net_profit, last_signal_time
    now = time.time()
    if last_signal_time != 0 and (now - last_signal_time) < CFG['COOLDOWN_SECONDS']:
        print("⏱️ Cooldown süresi dolmadan yeni sinyal geldi. İşlem atlandı.")
        return

    last_close_price = ut_bot_strategy.klines[-1]['close'] if ut_bot_strategy.klines else 0
    
    if bot_current_position != 'none':
        try:
            ut_bot_strategy.close_position(last_close_price)
            total_net_profit = sum(t['pnl'] for t in ut_bot_strategy.trades if t['action'] == 'exit')
            
            if not is_simulation_mode:
                account_info = await client.futures_account_balance()
                position_info = await client.futures_position_information()
                symbol_pos = next((p for p in position_info if p['symbol'] == CFG['SYMBOL']), None)
                if symbol_pos and float(symbol_pos['positionAmt']) != 0:
                    quantity = abs(float(symbol_pos['positionAmt']))
                    closing_side = 'SELL' if float(symbol_pos['positionAmt']) > 0 else 'BUY'
                    await client.futures_create_order(symbol=CFG['SYMBOL'], side=closing_side, type='MARKET', quantity=quantity)
                    print(f"✅ Gerçek pozisyon ({bot_current_position}) kapatıldı.")
            else:
                print(f"✅ SİMÜLASYON: Mevcut pozisyon ({bot_current_position}) kapatıldı.")

            profit = ut_bot_strategy.trades[-1]['pnl']
            profit_message = f"+{profit:.2f} USDT" if profit >= 0 else f"{profit:.2f} USDT"
            position_close_message = f"📉 Pozisyon kapatıldı! {bot_current_position.upper()}\n\nSon Kapanış Fiyatı: {last_close_price}\nBu İşlemden Kâr/Zarar: {profit_message}\n**Toplam Net Kâr: {total_net_profit:.2f} USDT**"
            await send_telegram_message(position_close_message)
            bot_current_position = 'none'
        except Exception as e:
            print(f"Mevcut pozisyonu kapatırken hata oluştu: {e}")
            return
    
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

async def process_message(msg, client):
    global bot_current_position
    
    if 'k' in msg and 'x' in msg['k'] and msg['k']['x']:
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
            if signal['type'] == 'BUY' and bot_current_position != 'long':
                await place_order(client, 'BUY', signal['message'])
            elif signal['type'] == 'SELL' and bot_current_position != 'short':
                await place_order(client, 'SELL', signal['message'])

def create_mock_files():
    """Mock dosyalarını oluştur"""
    try:
        if not os.path.exists('mock_klines.json'):
            with open('mock_klines.json', 'w') as f:
                json.dump([
                    [1678886400000, "42000", "42500", "41500", "42300"],
                    [1678886460000, "42300", "42800", "42200", "42700"],
                    [1678886520000, "42700", "42900", "42600", "42850"]
                ], f)
        
        if not os.path.exists('mock_kline_stream.json'):
            with open('mock_kline_stream.json', 'w') as f:
                json.dump([
                    {"e":"kline","k":{"t":1678886580000,"T":1678886639999,"s":"ETHUSDT","i":"1m","f":100,"L":200,"o":"42850","c":"43000","h":"43050","l":"42800","v":"1000","n":100,"x":True,"q":"100000","V":"500","Q":"50000","B":"0"}},
                    {"e":"kline","k":{"t":1678886640000,"T":1678886699999,"s":"ETHUSDT","i":"1m","f":201,"L":301,"o":"43000","c":"42950","h":"43020","l":"42900","v":"1200","n":150,"x":True,"q":"120000","V":"600","Q":"60000","B":"0"}}
                ], f)
    except Exception as e:
        print(f"Mock dosyalar oluşturulurken hata: {e}")

async def main():
    # Mock dosyalarını oluştur
    create_mock_files()
    
    if not is_simulation_mode:
        client = await AsyncClient.create(os.getenv('BINANCE_API_KEY'), os.getenv('BINANCE_SECRET_KEY'), testnet=CFG['IS_TESTNET'])
        bm = BinanceSocketManager(client)
        # Doğru WebSocket kullanımı
        symbol_lower = CFG['SYMBOL'].lower()
        stream_name = f'{symbol_lower}@kline_{CFG["INTERVAL"]}'
        ts = bm.multiplex_socket([stream_name])
    else:
        client = MockAsyncClient()
        bm = MockBinanceSocketManager(client)
        ts = bm.kline_socket(symbol=CFG['SYMBOL'], interval=CFG['INTERVAL'])

    # Zaman aşımı ekleme
    try:
        await asyncio.wait_for(fetch_initial_data(client, CFG['SYMBOL'], CFG['INTERVAL']), timeout=30.0)
    except asyncio.TimeoutError:
        print("❌ Hata: İlk verileri alma zaman aşımına uğradı. Ağ bağlantınızı veya API anahtarlarınızı kontrol edin.")
        return
    except Exception as e:
        print(f"❌ Hata: İlk verileri alırken beklenmeyen bir sorun oluştu: {e}")
        return
    
    async with ts as kline_socket:
        while True:
            res = await kline_socket.recv()
            if isinstance(res, dict):
                # Multiplex socket için stream data'yı kontrol et
                if 'stream' in res and 'data' in res:
                    await process_message(res['data'], client)
                else:
                    await process_message(res, client)

if __name__ == "__main__":
    # Yeni asyncio kullanımı
    asyncio.run(main())
