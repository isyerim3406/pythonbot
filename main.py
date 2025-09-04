import asyncio
import os
import time
from datetime import datetime, timezone
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
        
        # Veri saklama
        self.klines = []
        self.heikin_ashi_candles = []
        self.true_ranges = []
        self.atr_values = []
        
        # Pozisyon takibi
        self.xATRTrailingStop = None
        self.pos = 0
        self.prev_pos = 0  # Önceki pozisyonu takip et
        
        # Sermaye ve işlemler
        self.capital = self.initial_capital
        self.trades = []
        self.position_size = 0
        self.entry_price = 0
        self.total_pnl = 0

    def calculate_atr(self, period):
        if len(self.true_ranges) < period:
            return None
        return sum(self.true_ranges[-period:]) / period

    def calculate_sma(self, values, period):
        if len(values) < period:
            return None
        return sum(values[-period:]) / period

    def calculate_heikin_ashi(self, open_price, high, low, close_price):
        """PineScript'teki Heikin Ashi hesaplaması"""
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
        # Kline verilerini sakla
        self.klines.append({
            'timestamp': timestamp, 
            'open': open_price, 
            'high': high, 
            'low': low, 
            'close': close_price
        })
        if len(self.klines) > 500:
            self.klines.pop(0)

        # Önceki kapanış fiyatı
        prev_close = self.klines[-2]['close'] if len(self.klines) > 1 else close_price
        
        # Heikin Ashi hesapla (eğer aktifse)
        if self.h:
            src_candle = self.calculate_heikin_ashi(open_price, high, low, close_price)
            self.heikin_ashi_candles.append(src_candle)
            if len(self.heikin_ashi_candles) > 500:
                self.heikin_ashi_candles.pop(0)
            src = src_candle['close']
            src_high = src_candle['high']
            src_low = src_candle['low']
        else:
            src = close_price
            src_high = high
            src_low = low

        # True Range hesapla
        true_range = max(
            src_high - src_low,
            abs(src_high - prev_close),
            abs(src_low - prev_close)
        )
        self.true_ranges.append(true_range)
        if len(self.true_ranges) > 500:
            self.true_ranges.pop(0)

        # ATR hesapla
        xATR = self.calculate_atr(self.c)
        if xATR is None:
            return {'signal': None}

        nLoss = self.a * xATR

        # ATR Trailing Stop hesaplaması (PineScript'e uygun)
        prev_xATRTrailingStop = self.xATRTrailingStop if self.xATRTrailingStop is not None else src - nLoss

        if src > prev_xATRTrailingStop and prev_close > prev_xATRTrailingStop:
            self.xATRTrailingStop = max(prev_xATRTrailingStop, src - nLoss)
        elif src < prev_xATRTrailingStop and prev_close < prev_xATRTrailingStop:
            self.xATRTrailingStop = min(prev_xATRTrailingStop, src + nLoss)
        elif src > prev_xATRTrailingStop:
            self.xATRTrailingStop = src - nLoss
        else:
            self.xATRTrailingStop = src + nLoss

        # Pozisyon mantığı (PineScript'e uygun)
        self.prev_pos = self.pos

        if prev_close < prev_xATRTrailingStop and src > prev_xATRTrailingStop:
            self.pos = 1
        elif prev_close > prev_xATRTrailingStop and src < prev_xATRTrailingStop:
            self.pos = -1
        # Else durumunda pos değişmez (PineScript'teki gibi)

        # ATR değerlerini sakla
        current_atr = self.calculate_atr(self.c)
        if current_atr is not None:
            self.atr_values.append(current_atr)
            if len(self.atr_values) > 500:
                self.atr_values.pop(0)

        # Sideways filter
        long_term_atr_ma = self.calculate_sma(self.atr_values, self.atr_ma_period)
        is_sideways = (
            self.use_filter and long_term_atr_ma is not None and 
            (current_atr < long_term_atr_ma * self.atr_threshold)
        )

        # Sinyal üretimi (PineScript'e uygun)
        signal = None
        long_condition = self.pos == 1 and self.prev_pos == -1
        short_condition = self.pos == -1 and self.prev_pos == 1

        if long_condition and not is_sideways:
            signal = {'type': 'BUY', 'message': 'UT Bot: AL sinyali', 'price': src}
        elif short_condition and not is_sideways:
            signal = {'type': 'SELL', 'message': 'UT Bot: SAT sinyali', 'price': src}

        return {'signal': signal, 'pos': self.pos, 'prev_pos': self.prev_pos}

    def open_position(self, side, price):
        """Pozisyon aç"""
        # Önceki pozisyon varsa kapat
        if self.position_size != 0:
            self.close_position(price)
        
        # Yeni pozisyon hesapla
        equity_to_use = self.capital * (self.qty_percent / 100)
        qty = equity_to_use / price
        self.position_size = qty if side == 'BUY' else -qty
        self.entry_price = price
        
        self.trades.append({
            'action': 'entry', 
            'type': side, 
            'price': price, 
            'quantity': qty,
            'timestamp': time.time()
        })

    def close_position(self, price):
        """Pozisyon kapat ve PnL hesapla"""
        if self.position_size == 0:
            return {'pnl': 0, 'side': 'none'}
        
        # Pozisyon tarafını belirle
        side = 'LONG' if self.position_size > 0 else 'SHORT'
        
        # PnL hesapla
        if self.position_size > 0:  # LONG pozisyon
            pnl = self.position_size * (price - self.entry_price)
        else:  # SHORT pozisyon
            pnl = abs(self.position_size) * (self.entry_price - price)
        
        # Sermaye güncelle
        self.capital += pnl
        self.total_pnl += pnl
        
        # İşlem kaydı
        self.trades.append({
            'action': 'exit',
            'price': price,
            'quantity': abs(self.position_size),
            'pnl': pnl,
            'timestamp': time.time()
        })
        
        # Pozisyonu sıfırla
        self.position_size = 0
        self.entry_price = 0
        
        return {'pnl': pnl, 'side': side}

    def get_current_position_side(self):
        """Mevcut pozisyon tarafını döndür"""
        if self.position_size > 0:
            return 'LONG'
        elif self.position_size < 0:
            return 'SHORT'
        return 'none'

    def get_total_pnl(self):
        """Toplam PnL döndür"""
        return self.total_pnl

# =========================================================================================
# BOT AYARLARI
# =========================================================================================
CFG = {
    'a': 1,        # PineScript default değerleri
    'c': 10,
    'h': False,
    'use_filter': True,
    'atr_ma_period': 100,
    'atr_threshold': 0.7,
    'TRADE_SIZE_PERCENT': 100,
    'SYMBOL': os.getenv('SYMBOL', 'ETHUSDT'),
    'INTERVAL': os.getenv('INTERVAL', '1h'),
    'INITIAL_CAPITAL': 10000,  # PineScript default
    'COOLDOWN_SECONDS': 60*60,
    'BOT_NAME': "UT Bot Strategy Python",
    'MODE': "Simülasyon"
}

telegram_bot = None
if os.getenv('TG_TOKEN') and os.getenv('TG_CHAT_ID'):
    telegram_bot = telegram.Bot(token=os.getenv('TG_TOKEN'))

ut_bot_strategy = UTBotStrategy(options=CFG)

async def send_telegram_message(text):
    if not telegram_bot or not os.getenv('TG_CHAT_ID'):
        print("Telegram mesajı:", text)
        return
    try:
        await telegram_bot.send_message(
            chat_id=os.getenv('TG_CHAT_ID'),
            text=text,
            parse_mode=constants.ParseMode.MARKDOWN
        )
    except Exception as e:
        print(f"Telegram mesaj hatası: {e}")

# =========================================================================================
# BOT ANA DÖNGÜSÜ
# =========================================================================================
async def run_bot():
    print("🤖 Bot başlatılıyor...")

    client = await AsyncClient.create()
    bm = BinanceSocketManager(client)

    try:
        # Başlangıçta geçmiş 500 mum yükle
        candles = await client.get_klines(symbol=CFG['SYMBOL'], interval=CFG['INTERVAL'], limit=500)
        last_signal = None
        
        for c in candles:
            ts, o, h, l, cl = c[0], float(c[1]), float(c[2]), float(c[3]), float(c[4])
            result = ut_bot_strategy.process_candle(ts, o, h, l, cl)
            if result['signal']:
                last_signal = result['signal']

        print(f"✅ İlk {len(ut_bot_strategy.klines)} mum yüklendi")
        print(f"Son pos: {ut_bot_strategy.pos}, prev_pos: {ut_bot_strategy.prev_pos}")

        # Bot başlama mesajı
        last_signal_msg = last_signal['message'] if last_signal else "Henüz sinyal yok"
        current_pos = ut_bot_strategy.get_current_position_side()
        
        msg = (
            f"✅ **{CFG['BOT_NAME']} Başlatıldı!**\n\n"
            f"**Mod:** {CFG['MODE']}\n"
            f"**Sembol:** {CFG['SYMBOL']}\n"
            f"**Zaman Aralığı:** {CFG['INTERVAL']}\n"
            f"**Başlangıç Sermayesi:** {CFG['INITIAL_CAPITAL']} USDT\n"
            f"**Mevcut Pozisyon:** {current_pos}\n"
            f"**Son Sinyal:** {last_signal_msg}"
        )
        await send_telegram_message(msg)

        # WebSocket başlat
        ts = bm.kline_socket(symbol=CFG['SYMBOL'], interval=CFG['INTERVAL'])
        async with ts as stream:
            while True:
                try:
                    kmsg = await stream.recv()
                    if kmsg.get('e') != 'kline':
                        continue
                        
                    k = kmsg['k']
                    if k['x']:  # Mum kapandığında
                        timestamp = k['t']
                        open_price = float(k['o'])
                        high_price = float(k['h'])
                        low_price = float(k['l'])
                        close_price = float(k['c'])

                        # Bar kapanış logu
                        ts_str = datetime.fromtimestamp(timestamp / 1000, timezone.utc).strftime('%d.%m.%Y %H:%M:%S')
                        print(f"📊 Yeni bar: {CFG['SYMBOL']} {CFG['INTERVAL']} | close={close_price:.2f} | {ts_str} | pos={ut_bot_strategy.pos} | prev_pos={ut_bot_strategy.prev_pos}")

                        # Sinyal işleme
                        result = ut_bot_strategy.process_candle(timestamp, open_price, high_price, low_price, close_price)
                        
                        if result['signal']:
                            signal = result['signal']
                            side = signal['type']
                            
                            # Mevcut pozisyonu kapat (varsa)
                            close_result = ut_bot_strategy.close_position(close_price)
                            
                            if close_result['side'] != 'none':
                                # Kapatma mesajı gönder
                                pnl_text = f"+{close_result['pnl']:.2f}" if close_result['pnl'] >= 0 else f"{close_result['pnl']:.2f}"
                                pnl_percent = (close_result['pnl'] / CFG['INITIAL_CAPITAL']) * 100
                                total_percent = (ut_bot_strategy.get_total_pnl() / CFG['INITIAL_CAPITAL']) * 100
                                
                                ts_str = datetime.fromtimestamp(timestamp/1000, timezone.utc).strftime("%d.%m.%Y - %H:%M")
                                
                                close_msg = (
                                    f"📉 **{close_result['side']} Pozisyon Kapatıldı!**\n\n"
                                    f"**Bot Adı:** {CFG['BOT_NAME']}\n"
                                    f"**Sembol:** {CFG['SYMBOL']}\n"
                                    f"**Zaman Aralığı:** {CFG['INTERVAL']}\n"
                                    f"**Kapanış Fiyatı:** {close_price}\n"
                                    f"**Zaman:** {ts_str}\n"
                                    f"**Bu İşlemden Kar/Zarar:** %{pnl_percent:.2f} ({pnl_text} USDT)\n"
                                    f"**Toplam Net Kar/Zarar:** %{total_percent:.2f} ({ut_bot_strategy.get_total_pnl():.2f} USDT)"
                                )
                                await send_telegram_message(close_msg)
                            
                            # Yeni pozisyon aç
                            ut_bot_strategy.open_position(side, close_price)
                            
                            # Açma mesajı gönder
                            position_name = "LONG" if side == 'BUY' else "SHORT"
                            total_percent = (ut_bot_strategy.get_total_pnl() / CFG['INITIAL_CAPITAL']) * 100
                            ts_str = datetime.fromtimestamp(timestamp/1000, timezone.utc).strftime("%d.%m.%Y - %H:%M")
                            
                            open_msg = (
                                f"🟢 **{position_name} Pozisyon Açıldı!**\n\n" if side == 'BUY' else
                                f"🔴 **{position_name} Pozisyon Açıldı!**\n\n"
                            ) + (
                                f"**Bot Adı:** {CFG['BOT_NAME']}\n"
                                f"**Sembol:** {CFG['SYMBOL']}\n"
                                f"**Zaman Aralığı:** {CFG['INTERVAL']}\n"
                                f"**Sinyal:** {signal['message']}\n"
                                f"**Giriş Fiyatı:** {close_price}\n"
                                f"**Zaman:** {ts_str}\n"
                                f"**Toplam Net Kar/Zarar:** %{total_percent:.2f} ({ut_bot_strategy.get_total_pnl():.2f} USDT)"
                            )
                            await send_telegram_message(open_msg)

                except Exception as e:
                    print(f"Stream işleme hatası: {e}")
                    continue

    except Exception as e:
        print(f"Bot çalıştırma hatası: {e}")
    finally:
        await client.close_connection()

# =========================================================================================
# HTTP SERVER
# =========================================================================================
async def start_http_server():
    async def handle_root(request):
        status = {
            'status': 'Bot çalışıyor 🚀',
            'current_position': ut_bot_strategy.get_current_position_side(),
            'total_pnl': round(ut_bot_strategy.get_total_pnl(), 2),
            'pos': ut_bot_strategy.pos,
            'prev_pos': ut_bot_strategy.prev_pos,
            'total_trades': len(ut_bot_strategy.trades),
            'symbol': CFG['SYMBOL'],
            'interval': CFG['INTERVAL']
        }
        return web.json_response(status)
        
    async def handle_health(request):
        return web.json_response({'status': 'healthy'})

    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/healthz", handle_health)

    port = int(os.environ.get("PORT", 8000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"🌐 HTTP Server başlatıldı: port {port}")

# =========================================================================================
# MAIN
# =========================================================================================
async def main():
    await asyncio.gather(start_http_server(), run_bot())

if __name__ == "__main__":
    asyncio.run(main())
